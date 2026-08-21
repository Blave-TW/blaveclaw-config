"""
Paper venue — simulated trading against LIVE public prices, no exchange keys.

Why it exists: a user who wants to run a strategy before risking money had two
bad options — an exchange demo account (separate keys, not every venue has one)
or nothing. Binding "paper" on the web writes PAPER_API_KEY/PAPER_SECRET_KEY
(fixed values, no user input) and this lib pair (account_paper + order_paper)
makes the machine behave like any other official venue: the reconciler,
dispatch (market / TWAP / chase-limit), flatten, the web portfolio page and the
equity curve all run unchanged — only the fills are simulated.

Surface: the same function names and return shapes as lib/order_binance.py /
lib/order_okx.py (four reconciler names, limit layer, spot layer, protective
helpers, lib/guard built in). Read those docstrings for the contract; this
file documents only what is SIMULATED and how.

Fidelity — honest list of what is and is not modelled:
  prices      Binance public endpoints, no key: USDⓈ-M perps (fapi) for the
              swap layer, api.binance.com for spot. mark = premiumIndex
              markPrice, BBO = bookTicker, rules = exchangeInfo (step / min
              qty / min notional / tick), so below-minimum behaviour matches a
              real venue. A symbol Binance does not list cannot be paper-traded.
  market      fills the WHOLE order immediately at the far side of the book
              (buy @ ask, sell @ bid); taker fee 0.05% swap / 0.10% spot.
  limit       post-only chase contract: crossing the book at placement →
              {'status': 'post_only_rejected'}; otherwise rests and fills
              ALL-OR-NOTHING the first time the book touches the price
              (buy: ask <= px, sell: bid >= px), maker fee 0.02% swap /
              0.10% spot. Resting orders are settled LAZILY — on every call
              into this lib (get_order / get_open_orders / any account read),
              not tick by tick: a price that touched and left between two
              reads does not fill. A non-post-only limit that crosses fills
              at once as a taker.
  protective  SL/TP are stored and checked against the mark on every call
              into the lib (same lazy cadence); trigger = close the whole
              position at the mark, taker fee. No OCO race, no trigger gap.
  positions   one-way net per symbol, base units, average entry; reduce-only
              caps at the position and never flips. No funding, no
              liquidation, no partial fills, no latency, no slippage beyond
              the spread. One USDT cash pool funds swap margin AND spot buys;
              gross swap notional is capped at MAX_LEVERAGE × equity and an
              order that would take equity to <= 0 is refused (fail loud).
  flows       none — the initial cash is a seed, not a deposit.
Ledger: state/paper_ledger.json (atomic writes, flock on POSIX; Windows
msvcrt on Windows). Seeded lazily with PAPER_INITIAL_EQUITY (.env, default
10,000 USDT). The web writes PAPER_BOUND_TS on every bind: a ledger older than
the current bind is re-seeded, so unbind → rebind is a fresh start (and the
"starts with 10,000 USDT" copy stays true). reset_account(env) wipes
positions/orders and re-seeds — the agent runs it on explicit user request only.
Reduce/close legs are NEVER refused by the equity/leverage checks — only
exposure-adding fills are; a blown-up paper account can always be flattened.
"""
import json
import logging
import os
import threading
import time
from decimal import Decimal, ROUND_DOWN

import requests

from lib import guard

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

VENUE = "paper"
LEDGER_PATH = "state/paper_ledger.json"
LOCK_PATH = "state/paper_ledger.lock"
FAPI_URL = "https://fapi.binance.com"
SPOT_URL = "https://api.binance.com"
DEFAULT_CASH = 10_000.0
TAKER_FEE = 0.0005
MAKER_FEE = 0.0002
SPOT_FEE = 0.001
MAX_LEVERAGE = 10.0
MAX_FILLS = 500  # rolling fill history kept in the ledger (display / audit)

log = logging.getLogger("order_paper")


class PaperError(Exception):
    """Simulated-venue refusal with a readable reason (the exchange-error twin)."""


class UnknownSymbol(PaperError):
    """The reference market does not list this symbol (HTTP 400) — the only
    price-source failure that may be treated as 'unpriceable' rather than an
    outage."""


# ── reference market (Binance public, no key) ────────────────────────────────

def _canonical(symbol):
    s = str(symbol).upper().replace("-SWAP", "").replace("_", "").replace("-", "")
    return s.replace("/", "").replace(":USDT", "")


def _get(base, path, params=None, retries=3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(base + path, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code == 400:
                raise UnknownSymbol(f"reference market rejected {params}: {last}")
            if 400 <= r.status_code < 500 and r.status_code != 429:
                break  # bad request — retrying cannot help
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(0.5 * (i + 1))
    raise PaperError(f"reference price unavailable ({path}): {last}")


_rules_cache = {}
_spot_rules_cache = {}


def get_contract_rules(env, symbol):
    """{'step','min_qty','min_notional','contract_value','price_tick','active'}
    — Binance USDⓈ-M rules, base units (contract_value 1). Cached per process."""
    symbol = _canonical(symbol)
    if not _rules_cache:
        info = _get(FAPI_URL, "/fapi/v1/exchangeInfo")
        for s in info.get("symbols", []):
            f = {x.get("filterType"): x for x in s.get("filters", [])}
            lot, mkt = f.get("LOT_SIZE", {}), f.get("MARKET_LOT_SIZE", {})
            _rules_cache[s["symbol"]] = {
                "step": mkt.get("stepSize") or lot.get("stepSize") or "1",
                "min_qty": max(float(lot.get("minQty", 0)), float(mkt.get("minQty", 0))),
                "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 0)),
                "contract_value": 1.0,
                "price_tick": f.get("PRICE_FILTER", {}).get("tickSize", "0.01"),
                "active": s.get("status") == "TRADING",
            }
    if symbol not in _rules_cache:
        raise PaperError(f"{symbol} is not a USDⓈ-M perp on the reference market "
                         f"(Binance exchangeInfo) — paper can only trade listed symbols")
    return _rules_cache[symbol]


def get_spot_rules(env, symbol):
    """{'step','min_qty','min_notional','price_tick','active'} — Binance spot."""
    symbol = _canonical(symbol)
    if symbol not in _spot_rules_cache:
        info = _get(SPOT_URL, "/api/v3/exchangeInfo", {"symbol": symbol})
        s = (info.get("symbols") or [{}])[0]
        f = {x.get("filterType"): x for x in s.get("filters", [])}
        lot = f.get("LOT_SIZE", {})
        notional = f.get("NOTIONAL", {}) or f.get("MIN_NOTIONAL", {})
        _spot_rules_cache[symbol] = {
            "step": lot.get("stepSize") or "1",
            "min_qty": float(lot.get("minQty", 0)),
            "min_notional": float(notional.get("minNotional", 0) or 0),
            "price_tick": f.get("PRICE_FILTER", {}).get("tickSize", "0.01"),
            "active": s.get("status") == "TRADING",
        }
    return _spot_rules_cache[symbol]


def _floor(value, step):
    return Decimal(str(value)).quantize(Decimal(str(step)).normalize(), rounding=ROUND_DOWN)


def format_qty(env, symbol, qty, price=None):
    """Base qty floored to the step, as a string. Raises ValueError below the
    minimum (the min-size gate; callers turn that into a False skip)."""
    r = get_contract_rules(env, symbol)
    if not r["active"]:
        raise ValueError(f"{symbol} is not open for trading")
    q = _floor(qty, r["step"])
    if float(q) <= 0 or float(q) < r["min_qty"]:
        raise ValueError(f"{symbol} qty {qty} floors to {q}, below minimum {r['min_qty']}")
    if price and r["min_notional"] and float(q) * float(price) < r["min_notional"]:
        raise ValueError(f"{symbol} notional {float(q) * float(price):.2f} below "
                         f"minimum {r['min_notional']}")
    return format(q, "f")


def format_spot_qty(env, symbol, qty):
    r = get_spot_rules(env, symbol)
    if not r["active"]:
        raise ValueError(f"{symbol} is not open for spot trading")
    q = _floor(qty, r["step"])
    if float(q) <= 0 or float(q) < r["min_qty"]:
        raise ValueError(f"{symbol} spot qty {qty} floors to {q}, below minimum {r['min_qty']}")
    return format(q, "f")


def format_price(env, symbol, price):
    return float(_floor(price, get_contract_rules(env, symbol)["price_tick"]))


def format_spot_price(env, symbol, price):
    return float(_floor(price, get_spot_rules(env, symbol)["price_tick"]))


def get_mark_price(env, symbol):
    d = _get(FAPI_URL, "/fapi/v1/premiumIndex", {"symbol": _canonical(symbol)})
    return float(d["markPrice"])


def get_bbo(env, symbol):
    d = _get(FAPI_URL, "/fapi/v1/ticker/bookTicker", {"symbol": _canonical(symbol)})
    bid, ask = float(d.get("bidPrice") or 0), float(d.get("askPrice") or 0)
    if not bid or not ask:
        raise PaperError(f"{symbol} reference book is empty (bid={bid} ask={ask})")
    return {"bid": bid, "ask": ask}


def get_spot_price(env, symbol):
    return float(_get(SPOT_URL, "/api/v3/ticker/price", {"symbol": _canonical(symbol)})["price"])


def get_spot_bbo(env, symbol):
    d = _get(SPOT_URL, "/api/v3/ticker/bookTicker", {"symbol": _canonical(symbol)})
    bid, ask = float(d.get("bidPrice") or 0), float(d.get("askPrice") or 0)
    if not bid or not ask:
        raise PaperError(f"{symbol} reference spot book is empty (bid={bid} ask={ask})")
    return {"bid": bid, "ask": ask}


# ── ledger ───────────────────────────────────────────────────────────────────

_plock = threading.RLock()


def _initial_cash(env):
    try:
        v = float((env or {}).get("PAPER_INITIAL_EQUITY") or DEFAULT_CASH)
    except (TypeError, ValueError):
        v = DEFAULT_CASH
    return v if v > 0 else DEFAULT_CASH


def _new_ledger(env):
    cash = _initial_cash(env)
    return {"version": 1, "created_ts": int(time.time()), "initial_cash": cash,
            "cash": cash, "positions": {}, "spot": {}, "orders": {},
            "protective": {}, "fills": [], "seq": 0}


def _bound_ts(env):
    try:
        return int(float((env or {}).get("PAPER_BOUND_TS") or 0))
    except (TypeError, ValueError):
        return 0


def _load(env):
    bound_ts = _bound_ts(env)
    try:
        with open(LEDGER_PATH) as f:
            led = json.load(f)
        if isinstance(led, dict) and "cash" in led:
            for k in ("positions", "spot", "orders", "protective"):
                led.setdefault(k, {})
            led.setdefault("fills", [])
            led.setdefault("seq", 0)
            if bound_ts <= int(led.get("created_ts") or 0):
                return led
            log.info("paper ledger predates the current bind — re-seeding")
            guard.audit("paper_reseed", venue=VENUE, reason="rebind",
                        old_created_ts=led.get("created_ts"), bound_ts=bound_ts)
        else:
            log.error("paper ledger malformed — re-seeding")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        # a torn read must not silently re-seed a live paper account
        raise PaperError(f"paper ledger unreadable ({type(e).__name__}: {e})")
    fresh = _new_ledger(env)
    # created_ts never trails the bind stamp: the stamp comes from the user's
    # browser clock, and a clock ahead of the machine would otherwise re-seed
    # on every read until it catches up (wiping trades meanwhile)
    fresh["created_ts"] = max(fresh["created_ts"], bound_ts)
    return fresh


def _save(led):
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(led, f, indent=1)
    os.replace(tmp, LEDGER_PATH)


class _txn:
    """Exclusive read-modify-write on the ledger. Nothing is persisted if the
    block raises (refused orders leave no trace beyond the audit log)."""

    def __init__(self, env):
        self.env = env
        self.fh = None

    def __enter__(self):
        _plock.acquire()
        try:
            if fcntl is not None or msvcrt is not None:
                os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
                self.fh = open(LOCK_PATH, "a+")
                if fcntl is not None:
                    fcntl.flock(self.fh, fcntl.LOCK_EX)
                else:  # msvcrt: LK_LOCK retries ~10 s then raises — loop it
                    deadline = time.time() + 60
                    while True:
                        try:
                            msvcrt.locking(self.fh.fileno(), msvcrt.LK_LOCK, 1)
                            break
                        except OSError:
                            if time.time() > deadline:
                                raise PaperError("paper ledger lock timeout (60s)")
            self.led = _load(self.env)
            return self.led
        except Exception:
            self._release()
            raise

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                _save(self.led)
        finally:
            self._release()

    def _release(self):
        try:
            if self.fh is not None:
                if fcntl is not None:
                    fcntl.flock(self.fh, fcntl.LOCK_UN)
                else:
                    try:
                        msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                self.fh.close()
                self.fh = None
        finally:
            _plock.release()


def _next_id(led):
    led["seq"] = int(led.get("seq", 0)) + 1
    return f"P{led['seq']:08d}"


def _find_order(led, order_id=None, client_order_id=None):
    if order_id and str(order_id) in led["orders"]:
        return led["orders"][str(order_id)]
    if client_order_id:
        for o in led["orders"].values():
            if o.get("client_order_id") == client_order_id:
                return o
    return None


def _record_fill(led, o, qty, price, fee, realized=0.0, kind=None):
    o["executed_qty"] = float(o.get("executed_qty") or 0) + qty
    o["avg_price"] = price
    o["commission"] = float(o.get("commission") or 0) + fee
    o["status"] = "filled"
    o["fill_ts"] = int(time.time())
    led["fills"].append({"ts": o["fill_ts"], "order_id": o["order_id"],
                         "symbol": o["symbol"], "market": o["market"],
                         "side": o["side"], "qty": qty, "price": price, "fee": fee,
                         "realized": realized, "kind": kind or o["type"]})
    del led["fills"][:-MAX_FILLS]


def _mark_for(env, led, sym, cache):
    if sym not in cache:
        cache[sym] = get_mark_price(env, sym)
    return cache[sym]


def _equity_of(env, led, marks):
    upnl = 0.0
    for sym, p in led["positions"].items():
        upnl += (_mark_for(env, led, sym, marks) - p["entry"]) * p["qty"]
    spot_value = 0.0
    spot_prices = {}
    for asset, amt in led["spot"].items():
        if amt <= 0:
            continue
        try:
            px = get_spot_price(env, asset + "USDT")
        except UnknownSymbol:
            px = None  # delisted / no USDT pair — listed by get_holdings, valued None
        # any other PaperError (outage, 429) propagates: a silent None would
        # drop the whole spot value out of equity for one read and the curve
        # would record a fake drawdown
        spot_prices[asset] = px
        if px:
            spot_value += amt * px
    return led["cash"] + upnl + spot_value, upnl, spot_value, spot_prices


def _apply_swap_fill(env, led, sym, signed_qty, price, fee, marks):
    """Net one-way position math. Returns realized PnL. Refuses (raises) a
    fill that would blow the leverage cap or zero the account — an exchange
    would reject on margin; silence here would produce impossible curves."""
    pos = led["positions"].get(sym, {"qty": 0.0, "entry": 0.0})
    q0, e0 = float(pos["qty"]), float(pos["entry"])
    q1 = q0 + signed_qty
    realized = 0.0
    if q0 == 0 or (q0 > 0) == (signed_qty > 0):
        entry = (abs(q0) * e0 + abs(signed_qty) * price) / abs(q1)
    else:
        closed = min(abs(q0), abs(signed_qty))
        realized = (price - e0) * closed * (1 if q0 > 0 else -1)
        entry = price if abs(signed_qty) > abs(q0) else e0
    new_cash = led["cash"] + realized - fee
    new_positions = dict(led["positions"])
    if abs(q1) < 1e-12:
        new_positions.pop(sym, None)
    else:
        new_positions[sym] = {"qty": q1, "entry": entry}
    if abs(q1) > abs(q0) + 1e-12:
        # exposure-adding fill: check on the WOULD-BE state, nothing committed
        # yet. Reduce/close legs skip this — a blown-up account must still be
        # able to flatten (an exchange liquidates; we have no liquidation).
        marks.setdefault(sym, price)
        trial = dict(led, cash=new_cash, positions=new_positions)
        equity, _, _, _ = _equity_of(env, trial, marks)
        if equity <= 0:
            raise PaperError("paper account equity would be <= 0 — refused "
                             "(reset_account to start over)")
        gross = sum(abs(p["qty"]) * _mark_for(env, trial, s, marks)
                    for s, p in new_positions.items())
        if gross > MAX_LEVERAGE * equity + 1e-9:
            raise PaperError(f"gross notional {gross:.0f} exceeds {MAX_LEVERAGE:g}× paper "
                             f"equity {equity:.0f} — refused")
    led["cash"] = new_cash
    led["positions"] = new_positions
    if sym not in new_positions:
        led["protective"].pop(sym, None)
    return realized


MAX_CLOSED_ORDERS = 1000  # closed orders kept for get_order / idempotency lookups


def _prune(led):
    closed = [o for o in led["orders"].values() if o["status"] != "open"]
    if len(closed) > MAX_CLOSED_ORDERS:
        closed.sort(key=lambda o: o.get("ts", 0))
        for o in closed[: len(closed) - MAX_CLOSED_ORDERS]:
            led["orders"].pop(o["order_id"], None)


def _settle(env, led):
    """Lazy matching: fill resting limits the book has touched, fire
    protective triggers the mark has crossed. Called at the top of every
    ledger transaction."""
    _prune(led)
    bbo_cache, marks = {}, {}
    for o in list(led["orders"].values()):
        if o["status"] != "open":
            continue
        sym, market = o["symbol"], o["market"]
        key = (market, sym)
        if key not in bbo_cache:
            bbo_cache[key] = get_spot_bbo(env, sym) if market == "spot" else get_bbo(env, sym)
        bid, ask = bbo_cache[key]["bid"], bbo_cache[key]["ask"]
        px = float(o["price"])
        touched = (ask <= px) if o["side"] == "buy" else (bid >= px)
        if not touched:
            continue
        qty = float(o["orig_qty"])
        if market == "spot":
            try:
                _fill_spot(env, led, o, qty, px, SPOT_FEE)
            except PaperError as e:
                o["status"] = "canceled"
                o["reason"] = str(e)
            continue
        else:
            signed = qty if o["side"] == "buy" else -qty
            if o.get("reduce_only"):
                signed = _cap_reduce(led, sym, signed)
                if signed == 0:
                    o["status"] = "canceled"
                    o["reason"] = "nothing left to reduce"
                    continue
            fee = abs(signed) * px * MAKER_FEE
            try:
                realized = _apply_swap_fill(env, led, sym, signed, px, fee, marks)
            except PaperError as e:
                o["status"] = "canceled"
                o["reason"] = str(e)
                continue
            _record_fill(led, o, abs(signed), px, fee, realized)
    for sym, trig in list(led["protective"].items()):
        pos = led["positions"].get(sym)
        if not pos:
            led["protective"].pop(sym, None)
            continue
        mark = _mark_for(env, led, sym, marks)
        long = pos["qty"] > 0
        sl, tp = trig.get("sl"), trig.get("tp")
        hit = None
        if sl and ((long and mark <= sl) or (not long and mark >= sl)):
            hit = "sl"
        elif tp and ((long and mark >= tp) or (not long and mark <= tp)):
            hit = "tp"
        if not hit:
            continue
        signed = -pos["qty"]
        fee = abs(signed) * mark * TAKER_FEE
        o = _new_order(led, sym, "swap", "sell" if long else "buy", "market", mark,
                       abs(signed), None, reduce_only=True, direction="long" if long else "short")
        realized = _apply_swap_fill(env, led, sym, signed, mark, fee, marks)
        _record_fill(led, o, abs(signed), mark, fee, realized, kind=hit)
        led["protective"].pop(sym, None)
        guard.audit("order_ok", venue=VENUE, symbol=sym, intent="protective",
                    kind=hit, qty=abs(signed), price=mark, order_id=o["order_id"])


def _cap_reduce(led, sym, signed):
    q0 = float(led["positions"].get(sym, {}).get("qty", 0))
    if q0 == 0 or (q0 > 0) == (signed > 0):
        return 0.0
    return -q0 if abs(signed) > abs(q0) else signed


def _new_order(led, sym, market, side, typ, price, qty, cid, reduce_only=False,
               direction=None, post_only=False):
    oid = _next_id(led)
    o = {"order_id": oid, "client_order_id": cid or "", "symbol": sym, "market": market,
         "side": side, "direction": direction, "type": typ, "price": price,
         "orig_qty": qty, "executed_qty": 0.0, "avg_price": 0.0, "commission": 0.0,
         "status": "open", "reduce_only": bool(reduce_only), "post_only": bool(post_only),
         "ts": int(time.time())}
    led["orders"][oid] = o
    return o


def _fill_spot(env, led, o, base_qty, price, fee_rate):
    base = _spot_base(o["symbol"])
    if o["side"] == "buy":
        cost = base_qty * price
        fee = cost * fee_rate
        if led["cash"] < cost + fee:
            raise PaperError(f"insufficient paper cash for {o['symbol']} buy "
                             f"({led['cash']:.2f} < {cost + fee:.2f})")
        led["cash"] -= cost + fee
        led["spot"][base] = led["spot"].get(base, 0.0) + base_qty
    else:
        held = led["spot"].get(base, 0.0)
        if base_qty > held + 1e-12:
            raise PaperError(f"insufficient {base} to sell ({held} < {base_qty})")
        proceeds = base_qty * price
        fee = proceeds * fee_rate
        led["cash"] += proceeds - fee
        left = held - base_qty
        if left <= 1e-12:
            led["spot"].pop(base, None)
        else:
            led["spot"][base] = left
    _record_fill(led, o, base_qty, price, fee)
    return fee


def _order_view(o):
    return {"order_id": o["order_id"], "status": o["status"],
            "avg_price": float(o.get("avg_price") or 0),
            "orig_qty": float(o.get("orig_qty") or 0),
            "executed_qty": float(o.get("executed_qty") or 0),
            "quote_qty": float(o.get("executed_qty") or 0) * float(o.get("avg_price") or 0),
            "commission": float(o.get("commission") or 0), "commission_asset": "USDT",
            "client_order_id": o.get("client_order_id") or "", "raw": dict(o)}


def _fill_view(o):
    v = _order_view(o)
    v["exchange"] = VENUE
    return v


def _gate(intent, **fields):
    fields["venue"] = VENUE
    fields["intent"] = intent
    if intent == "entry" and guard.halted():
        guard.audit("order_denied_halt", **fields)
        raise guard.Halted(
            f"state/HALT is set ({guard.halt_info()}) — paper entry order for "
            f"{fields.get('symbol')} refused. Closes, SL/TP and cancels still work. "
            f"Only the user may clear the halt (guard.clear_halt).")
    guard.audit("order_attempt", **fields)
    return fields


# ── swap layer (reconciler contract) ─────────────────────────────────────────

def place_market_order(env, symbol, direction, qty, client_order_id=None,
                       reduce_only=False):
    """Confirmed simulated market order: whole qty at the far side of the live
    book. Below min → False. Same client_order_id → the earlier fill (idempotent)."""
    sym = _canonical(symbol)
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be long|short, got {direction!r}")
    get_contract_rules(env, sym)  # unknown symbol fails here with the readable reason
    bbo = get_bbo(env, sym)
    try:
        # reduce-only is exempt from MIN_NOTIONAL (Binance semantics, and the
        # reason flatten can always clear a residual) — only the step/min-qty gate
        q = float(format_qty(env, sym, qty,
                             price=None if reduce_only else (bbo["ask"] + bbo["bid"]) / 2))
    except ValueError as e:
        if "not open for trading" in str(e):
            raise
        return False
    buy = (direction == "long") != bool(reduce_only)
    side = "buy" if buy else "sell"
    price = bbo["ask"] if buy else bbo["bid"]
    fields = _gate("reduce" if reduce_only else "entry", symbol=sym, side=side,
                   qty=q, client_order_id=client_order_id or "")
    try:
        with _txn(env) as led:
            _settle(env, led)
            prev = _find_order(led, client_order_id=client_order_id) if client_order_id else None
            if prev is not None:
                return _fill_view(prev) if prev["status"] == "filled" else False
            signed = q if buy else -q
            if reduce_only:
                signed = _cap_reduce(led, sym, signed)
                if signed == 0:
                    guard.audit("order_ok", order_id="", note="nothing to reduce", **fields)
                    return False
            fee = abs(signed) * price * TAKER_FEE
            o = _new_order(led, sym, "swap", side, "market", price, abs(signed),
                           client_order_id, reduce_only=reduce_only, direction=direction)
            realized = _apply_swap_fill(env, led, sym, signed, price, fee, {})
            _record_fill(led, o, abs(signed), price, fee, realized)
            guard.audit("order_ok", order_id=o["order_id"], price=price, **fields)
            return _fill_view(o)
    except guard.Halted:
        raise
    except Exception as e:
        guard.audit("order_error", error=str(e), **fields)
        raise


def close_position_partial(env, symbol, direction, qty, client_order_id=None):
    return place_market_order(env, symbol, direction, qty,
                              client_order_id=client_order_id, reduce_only=True)


def place_limit_order(env, symbol, direction, qty, price, client_order_id=None,
                      reduce_only=False, time_in_force="GTC", post_only=False):
    """Resting simulated limit (chase contract). post_only crossing the live
    book → {'status': 'post_only_rejected'}; non-post-only crossing fills now
    as a taker. Below min → False."""
    sym = _canonical(symbol)
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be long|short, got {direction!r}")
    px = format_price(env, sym, price)
    try:
        q = float(format_qty(env, sym, qty, price=None if reduce_only else px))
    except ValueError as e:
        if "not open for trading" in str(e):
            raise
        return False
    buy = (direction == "long") != bool(reduce_only)
    side = "buy" if buy else "sell"
    bbo = get_bbo(env, sym)
    fields = _gate("reduce" if reduce_only else "entry", symbol=sym, side=side, qty=q,
                   price=px, client_order_id=client_order_id or "", post_only=post_only)
    try:
        with _txn(env) as led:
            _settle(env, led)
            prev = _find_order(led, client_order_id=client_order_id) if client_order_id else None
            if prev is not None:
                return {"order_id": prev["order_id"], "status": prev["status"],
                        "client_order_id": prev.get("client_order_id") or ""}
            crosses = (px >= bbo["ask"]) if buy else (px <= bbo["bid"])
            if crosses and post_only:
                o = _new_order(led, sym, "swap", side, "limit", px, q, client_order_id,
                               reduce_only=reduce_only, direction=direction, post_only=True)
                o["status"] = "canceled"
                o["reason"] = "post_only would take liquidity"
                guard.audit("order_ok", order_id=o["order_id"], note="post_only_rejected", **fields)
                return {"status": "post_only_rejected", "order_id": o["order_id"]}
            o = _new_order(led, sym, "swap", side, "limit", px, q, client_order_id,
                           reduce_only=reduce_only, direction=direction, post_only=post_only)
            if crosses:
                fill_px = bbo["ask"] if buy else bbo["bid"]
                signed = q if buy else -q
                if reduce_only:
                    signed = _cap_reduce(led, sym, signed)
                    if signed == 0:
                        o["status"] = "canceled"
                        o["reason"] = "nothing left to reduce"
                        return {"order_id": o["order_id"], "status": "canceled"}
                fee = abs(signed) * fill_px * TAKER_FEE
                realized = _apply_swap_fill(env, led, sym, signed, fill_px, fee, {})
                _record_fill(led, o, abs(signed), fill_px, fee, realized)
            guard.audit("order_ok", order_id=o["order_id"], **fields)
            return {"order_id": o["order_id"], "status": o["status"],
                    "client_order_id": client_order_id or ""}
    except guard.Halted:
        raise
    except Exception as e:
        guard.audit("order_error", error=str(e), **fields)
        raise


def get_order(env, symbol, order_id):
    with _txn(env) as led:
        _settle(env, led)
        o = _find_order(led, order_id=order_id)
        if o is None:
            raise PaperError(f"order {order_id} unknown to the paper ledger")
        return _order_view(o)


def cancel_order(env, symbol, order_id=None, client_order_id=None):
    """Idempotent-safe: open → canceled; filled / canceled / unknown →
    {'status': 'already_gone'} (never raises for a race)."""
    with _txn(env) as led:
        _settle(env, led)
        o = _find_order(led, order_id=order_id, client_order_id=client_order_id)
        if o is None or o["status"] != "open":
            return {"status": "already_gone", "order_id": str(order_id or client_order_id or "")}
        o["status"] = "canceled"
        o["reason"] = "canceled by caller"
        guard.audit("order_ok", venue=VENUE, intent="cancel", symbol=o["symbol"],
                    order_id=o["order_id"])
        return {"status": "canceled", "order_id": o["order_id"]}


def _open_rows(led, market, symbol=None):
    sym = _canonical(symbol) if symbol else None
    return [{"symbol": o["symbol"], "order_id": o["order_id"],
             "client_order_id": o.get("client_order_id") or "", "side": o["side"],
             "price": o["price"], "orig_qty": o["orig_qty"], "reduce_only": o.get("reduce_only"),
             "ts": o["ts"]}
            for o in led["orders"].values()
            if o["status"] == "open" and o["market"] == market and (not sym or o["symbol"] == sym)]


def get_open_orders(env, symbol=None):
    with _txn(env) as led:
        _settle(env, led)
        return _open_rows(led, "swap", symbol)


# ── protective (SL/TP) — lazy triggers, see module docstring ─────────────────

def place_protective_orders(env, symbol, direction, qty=None, sl_price=None,
                            tp_price=None, verify=True):
    """Arm SL and/or TP on the position (whole position — qty is accepted for
    contract parity and ignored). Replaces any previous triggers on the symbol."""
    sym = _canonical(symbol)
    if not sl_price and not tp_price:
        raise ValueError("sl_price or tp_price required")
    fields = _gate("protective", symbol=sym, sl=sl_price, tp=tp_price)
    with _txn(env) as led:
        _settle(env, led)
        if sym not in led["positions"]:
            raise PaperError(f"no open paper position on {sym} to protect")
        led["protective"][sym] = {"sl": float(sl_price) if sl_price else None,
                                  "tp": float(tp_price) if tp_price else None,
                                  "ts": int(time.time())}
        guard.audit("order_ok", order_id=f"prot-{sym}", **fields)
        return {"symbol": sym, "sl_price": led["protective"][sym]["sl"],
                "tp_price": led["protective"][sym]["tp"], "status": "armed"}


def cancel_protective_orders(env, symbol):
    sym = _canonical(symbol)
    with _txn(env) as led:
        return {"removed": led["protective"].pop(sym, None) is not None}


def open_position(env, symbol, direction, qty, sl_price=None, tp_price=None,
                  client_order_id=None):
    fill = place_market_order(env, symbol, direction, qty, client_order_id=client_order_id)
    if fill is False:
        return False
    if sl_price or tp_price:
        fill["protective"] = place_protective_orders(env, symbol, direction,
                                                     sl_price=sl_price, tp_price=tp_price)
    return fill


# ── spot layer ───────────────────────────────────────────────────────────────

def _spot_base(sym):
    for q in ("USDT", "USDC"):
        if sym.endswith(q) and len(sym) > len(q):
            return sym[: -len(q)]
    raise PaperError(f"paper spot supports USDT/USDC quotes only, got {sym}")


def place_spot_market_order(env, symbol, side, base_qty=None, quote_qty=None,
                            client_order_id=None):
    """Confirmed simulated spot market order — BUY sized in quote (USDT), SELL in
    base, at the live far side of the spot book. Below min → False."""
    sym = _canonical(symbol)
    side = str(side).lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy|sell, got {side!r}")
    base = _spot_base(sym)
    bbo = get_spot_bbo(env, sym)
    rules = get_spot_rules(env, sym)
    if side == "buy":
        if quote_qty is None:
            raise ValueError("spot buys are sized in quote currency — pass quote_qty")
        if rules["min_notional"] and float(quote_qty) < rules["min_notional"]:
            return False
        price = bbo["ask"]
        try:
            q = float(format_spot_qty(env, sym, float(quote_qty) / price))
        except ValueError as e:
            if "not open" in str(e):
                raise
            return False
    else:
        if base_qty is None:
            raise ValueError("spot sells are sized in base currency — pass base_qty")
        price = bbo["bid"]
        try:
            q = float(format_spot_qty(env, sym, base_qty))
        except ValueError as e:
            if "not open" in str(e):
                raise
            return False
        if rules["min_notional"] and q * price < rules["min_notional"]:
            return False
    fields = _gate("entry" if side == "buy" else "reduce", symbol=sym, side=side, qty=q,
                   market="spot", client_order_id=client_order_id or "")
    try:
        with _txn(env) as led:
            _settle(env, led)
            prev = _find_order(led, client_order_id=client_order_id) if client_order_id else None
            if prev is not None:
                return _fill_view(prev) if prev["status"] == "filled" else False
            if side == "sell":
                q = min(q, led["spot"].get(base, 0.0))
                if q <= 0:
                    guard.audit("order_ok", order_id="", note="no inventory", **fields)
                    return False
            o = _new_order(led, sym, "spot", side, "market", price, q, client_order_id)
            _fill_spot(env, led, o, q, price, SPOT_FEE)
            guard.audit("order_ok", order_id=o["order_id"], price=price, **fields)
            return _fill_view(o)
    except guard.Halted:
        raise
    except Exception as e:
        guard.audit("order_error", error=str(e), **fields)
        raise


def place_spot_limit_order(env, symbol, side, base_qty, price, client_order_id=None,
                           post_only=False):
    sym = _canonical(symbol)
    side = str(side).lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy|sell, got {side!r}")
    px = format_spot_price(env, sym, price)
    try:
        q = float(format_spot_qty(env, sym, base_qty))
    except ValueError as e:
        if "not open" in str(e):
            raise
        return False
    bbo = get_spot_bbo(env, sym)
    fields = _gate("entry" if side == "buy" else "reduce", symbol=sym, side=side, qty=q,
                   price=px, market="spot", client_order_id=client_order_id or "",
                   post_only=post_only)
    try:
        with _txn(env) as led:
            _settle(env, led)
            prev = _find_order(led, client_order_id=client_order_id) if client_order_id else None
            if prev is not None:
                return {"order_id": prev["order_id"], "status": prev["status"],
                        "client_order_id": prev.get("client_order_id") or ""}
            base = _spot_base(sym)
            if side == "buy" and led["cash"] < q * px * (1 + SPOT_FEE):
                raise PaperError(f"insufficient paper cash to rest a {sym} buy of "
                                 f"{q} @ {px} ({led['cash']:.2f} available)")
            if side == "sell" and led["spot"].get(base, 0.0) + 1e-12 < q:
                raise PaperError(f"insufficient {base} to rest a sell of {q} "
                                 f"({led['spot'].get(base, 0.0)} held)")
            crosses = (px >= bbo["ask"]) if side == "buy" else (px <= bbo["bid"])
            o = _new_order(led, sym, "spot", side, "limit", px, q, client_order_id,
                           post_only=post_only)
            if crosses and post_only:
                o["status"] = "canceled"
                o["reason"] = "post_only would take liquidity"
                guard.audit("order_ok", order_id=o["order_id"], note="post_only_rejected", **fields)
                return {"status": "post_only_rejected", "order_id": o["order_id"]}
            if crosses:
                _fill_spot(env, led, o, q, bbo["ask"] if side == "buy" else bbo["bid"], SPOT_FEE)
            guard.audit("order_ok", order_id=o["order_id"], **fields)
            return {"order_id": o["order_id"], "status": o["status"],
                    "client_order_id": client_order_id or ""}
    except guard.Halted:
        raise
    except Exception as e:
        guard.audit("order_error", error=str(e), **fields)
        raise


def get_spot_order(env, symbol, order_id):
    return get_order(env, symbol, order_id)


def cancel_spot_order(env, symbol, order_id=None, client_order_id=None):
    return cancel_order(env, symbol, order_id=order_id, client_order_id=client_order_id)


def get_spot_open_orders(env, symbol=None):
    with _txn(env) as led:
        _settle(env, led)
        return _open_rows(led, "spot", symbol)


def get_spot_balances(env):
    """{asset: amount} — simulated spot inventory plus the USDT cash pool."""
    with _txn(env) as led:
        _settle(env, led)
        out = {a: float(v) for a, v in led["spot"].items() if v > 0}
        out["USDT"] = float(led["cash"])
        return out


# ── account view (lib/account_paper.py reads through here) ───────────────────

def snapshot(env):
    """Settled, marked-to-market view of the paper account for account_paper."""
    with _txn(env) as led:
        _settle(env, led)
        marks = {}
        equity, upnl, spot_value, spot_prices = _equity_of(env, led, marks)
        positions = []
        for sym, p in led["positions"].items():
            positions.append({"symbol": sym, "side": "long" if p["qty"] > 0 else "short",
                              "size": abs(p["qty"]), "entry_price": p["entry"],
                              "mark_price": marks[sym]})
        return {"equity": equity, "cash": led["cash"], "unrealized": upnl,
                "spot_value": spot_value, "positions": positions,
                "spot": {a: {"amount": v, "price": spot_prices.get(a)}
                         for a, v in led["spot"].items() if v > 0},
                "created_ts": led["created_ts"], "initial_cash": led["initial_cash"],
                "fills": list(led["fills"][-50:])}


def reset_account(env, cash=None):
    """Wipe positions/orders/fills and re-seed cash (PAPER_INITIAL_EQUITY or
    `cash`). Explicit user request only — the agent never resets on its own."""
    fields = _gate("reduce", symbol="*", note="reset_account")
    with _txn(env) as led:
        fresh = _new_ledger(env)
        if cash:
            fresh["cash"] = fresh["initial_cash"] = float(cash)
        led.clear()
        led.update(fresh)
        guard.audit("order_ok", order_id="reset", **fields)
        return {"equity": led["cash"], "currency": "USDT"}


if __name__ == "__main__":  # offline self-check: python -m lib.order_paper (run from a scratch cwd)
    import tempfile
    _book = {"BTCUSDT": (100000.0, 100001.0)}

    def _fake(base, path, params=None, retries=3):
        sym = (params or {}).get("symbol", "BTCUSDT")
        if path.endswith("exchangeInfo"):
            return {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "50"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"}]}]}
        if path.endswith("premiumIndex"):
            return {"markPrice": str(sum(_book[sym]) / 2)}
        return {"bidPrice": str(_book[sym][0]), "askPrice": str(_book[sym][1])}
    _get = _fake  # noqa: F811
    os.chdir(tempfile.mkdtemp())
    e = {}
    f = place_market_order(e, "BTCUSDT", "long", 0.01, client_order_id="a")
    assert f["avg_price"] == 100001.0 and f["executed_qty"] == 0.01
    assert place_market_order(e, "BTCUSDT", "long", 0.01, client_order_id="a")["order_id"] == f["order_id"]
    assert place_market_order(e, "BTCUSDT", "long", 0.0001) is False
    r = place_limit_order(e, "BTCUSDT", "long", 0.01, 99990.0, post_only=True)
    assert r["status"] == "open" and get_order(e, "BTCUSDT", r["order_id"])["status"] == "open"
    _book["BTCUSDT"] = (99989.0, 99990.0)
    assert get_order(e, "BTCUSDT", r["order_id"])["status"] == "filled"
    c = close_position_partial(e, "BTCUSDT", "long", 1.0)  # capped at 0.02 held, never flips
    assert c["executed_qty"] == 0.02 and snapshot(e)["positions"] == []
    guard.trip_halt("self-check", "order_paper")
    try:
        place_market_order(e, "BTCUSDT", "long", 0.01)
        raise SystemExit("HALT did not block the entry")
    except guard.Halted:
        pass
    guard.clear_halt("order_paper")
    print("order_paper self-check OK")
