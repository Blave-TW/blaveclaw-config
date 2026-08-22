"""
Paper venue — simulated trading, no exchange, no keys.

Binding "paper" writes PAPER_API_KEY/PAPER_SECRET_KEY (fixed markers, not
secrets) + PAPER_BOUND_TS; this lib pair (order_paper + account_paper) then
makes the machine behave like any official venue — the reconciler, dispatch,
flatten, the web portfolio page and the equity curve run unchanged, only the
fills are simulated. Same surface as the other order libs (four reconciler
names + limit/spot/protective helpers, lib/guard built in).

PRICE (the whole model, Wei 2026-08-21):
  Fills use the price the STRATEGY sees, from the STRATEGY's own data source —
  resolved via lib/paper_data.current_price (see that module for why: adjusted
  vs raw prices, per-strategy endpoints). NOT an independent public feed.
  * one price per symbol (latest bar Close) — NO bid/ask, NO spread, NO
    slippage. Beginner-tier fidelity (Webull/TradingView do the same); fees are
    the one realism kept.
  * fills are IMMEDIATE at that price — no wait for the next bar (the live-paper
    convention, e.g. Freqtrade dry-run). Paper therefore diverges a little from
    the next-bar-open backtest; that is normal and a useful look-ahead check.
  * ONE USD account, any asset, NO FX: the reconciler sizes qty = USD ÷ price,
    so PnL = qty × (mark − entry) = USD_notional × (mark/entry − 1), a unitless
    return — currency and contract unit cancel (product decision B). A TXF or
    TW-stock position needs no point value / lot / FX here.

Contract rules are permissive (fetch_data gives OHLC, not an instrument spec):
no min-qty / min-notional / step gating — the reconciler's own slice threshold
is the floor. Positions are tracked as base-qty + entry (qty is USD÷price, a
notional-derived number; never floored to a crypto step).

Ledger: state/paper_ledger.json (atomic writes, flock on POSIX / msvcrt on
Windows). Seeded with PAPER_INITIAL_EQUITY (.env, default 10,000 USDT); a ledger
older than PAPER_BOUND_TS is re-seeded, so unbind→rebind is a fresh start.
reset_account(env) wipes and re-seeds — agent runs it on explicit user request
only. Reduce/close legs are NEVER refused by the equity/leverage checks.
"""
import json
import logging
import os
import threading
import time

from lib import guard
from lib.paper_data import current_price, PaperNoPrice

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
DEFAULT_CASH = 10_000.0
TAKER_FEE = 0.0005   # market / crossing-limit / protective-trigger
MAKER_FEE = 0.0002   # resting-limit fill
SPOT_FEE = 0.001     # spot both sides
MAX_LEVERAGE = 10.0
MAX_FILLS = 500
MAX_CLOSED_ORDERS = 1000

log = logging.getLogger("order_paper")


class PaperError(Exception):
    """Simulated-venue refusal with a readable reason."""


# ── price (from the strategy's own data source) ──────────────────────────────

def _price(env, symbol, cache=None):
    """Current price for symbol via lib/paper_data (the strategy's fetch_data).
    Optional per-operation cache dict avoids re-fetching one symbol twice in a
    single fill/mark. Raises PaperError (not a silent 0) when unpriceable."""
    if cache is not None and symbol in cache:
        return cache[symbol]
    try:
        px, _ = current_price(symbol, env)
    except PaperNoPrice as e:
        raise PaperError(str(e))
    px = float(px)
    if not (px > 0):
        raise PaperError(f"non-positive price for {symbol}: {px}")
    if cache is not None:
        cache[symbol] = px
    return px


def get_mark_price(env, symbol):
    """Cross-venue wiring contract: current price for USD→qty conversion."""
    return _price(env, symbol)


def get_spot_price(env, symbol):
    return _price(env, symbol)


def get_bbo(env, symbol):
    """No real book — bid == ask == mark (paper has no spread). Kept so the
    chase/limit layer has a uniform interface."""
    p = _price(env, symbol)
    return {"bid": p, "ask": p}


def get_spot_bbo(env, symbol):
    p = _price(env, symbol)
    return {"bid": p, "ask": p}


# Permissive rules — fetch_data is OHLC only, there is no instrument spec, and
# a notional-derived qty must never be floored to a crypto step. The
# reconciler's own USD slice threshold is the real floor.
_OPEN_RULES = {"step": "0.00000001", "min_qty": 0.0, "min_notional": 0.0,
               "contract_value": 1.0, "price_tick": "0.00000001", "active": True}


def get_contract_rules(env, symbol):
    return dict(_OPEN_RULES)


def get_spot_rules(env, symbol):
    return dict(_OPEN_RULES)


def format_qty(env, symbol, qty, price=None):
    """No min gate (permissive rules) — return the qty string. Non-positive →
    empty (the caller's min-size skip contract)."""
    q = float(qty)
    return repr(q) if q > 0 else ""


def format_spot_qty(env, symbol, qty):
    q = float(qty)
    return repr(q) if q > 0 else ""


def _spot_base(sym):
    for q in ("USDT", "USDC"):
        if sym.endswith(q) and len(sym) > len(q):
            return sym[: -len(q)]
    raise PaperError(f"paper spot supports USDT/USDC quotes only, got {sym}")


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
    return {"version": 2, "created_ts": int(time.time()), "initial_cash": cash,
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
        raise PaperError(f"paper ledger unreadable ({type(e).__name__}: {e})")
    fresh = _new_ledger(env)
    fresh["created_ts"] = max(fresh["created_ts"], bound_ts)  # clock-skew guard
    return fresh


def _save(led):
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(led, f, indent=1)
    os.replace(tmp, LEDGER_PATH)


class _txn:
    """Exclusive read-modify-write on the ledger; nothing persists if the block
    raises (a refused order leaves no trace beyond the audit log)."""

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
                else:
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


def _prune(led):
    closed = [o for o in led["orders"].values() if o["status"] != "open"]
    if len(closed) > MAX_CLOSED_ORDERS:
        closed.sort(key=lambda o: o.get("ts", 0))
        for o in closed[: len(closed) - MAX_CLOSED_ORDERS]:
            led["orders"].pop(o["order_id"], None)


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


# ── position math (base qty + entry; PnL = qty × Δprice = notional × return) ──

def _cap_reduce(led, sym, signed):
    q0 = float(led["positions"].get(sym, {}).get("qty", 0))
    if q0 == 0 or (q0 > 0) == (signed > 0):
        return 0.0
    return -q0 if abs(signed) > abs(q0) else signed


def _pos_mark(env, led, sym, marks):
    """Mark price for a HELD position, tolerant of a symbol that lost its price
    source (e.g. its strategy was deleted → resolver can't price it). Falls
    back to the last fill price (entry) so one un-priceable position can never
    take the whole account read down (audit P1-2) — nor block flattening it.
    Returns (mark, priced) where priced=False means the entry fallback was used
    (unrealized PnL for that leg is then 0)."""
    try:
        return _price(env, sym, marks), True
    except PaperError:
        return float(led["positions"].get(sym, {}).get("entry") or 0.0), False


def _equity(env, led, marks):
    """cash + unrealized swap PnL + spot inventory at mark. marks caches
    per-symbol prices for this operation."""
    upnl = 0.0
    for sym, p in led["positions"].items():
        mark, _ = _pos_mark(env, led, sym, marks)
        upnl += (mark - p["entry"]) * p["qty"]
    spot_value = 0.0
    prices = {}
    for asset, amt in led["spot"].items():
        if amt <= 0:
            continue
        try:
            px = _price(env, asset + "USDT", marks)
        except PaperError:
            px = None  # unpriceable — listed by holdings, valued None
        prices[asset] = px
        if px:
            spot_value += amt * px
    return led["cash"] + upnl + spot_value, upnl, spot_value, prices


def _apply_swap_fill(env, led, sym, signed_qty, price, fee, marks):
    """Net one-way position update; returns realized PnL. Only exposure-adding
    fills are checked against equity/leverage (computed on the would-be state,
    committed only if it passes). Reduce/close never refused — no liquidation
    model, so a blown account must still be flatten-able."""
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
        marks.setdefault(sym, price)
        trial = dict(led, cash=new_cash, positions=new_positions)
        equity, _, _, _ = _equity(env, trial, marks)
        if equity <= 0:
            raise PaperError("paper account equity would be <= 0 — refused "
                             "(reset_account to start over)")
        gross = sum(abs(p["qty"]) * _pos_mark(env, dict(led, positions=new_positions), s, marks)[0]
                    for s, p in new_positions.items())
        if gross > MAX_LEVERAGE * equity + 1e-9:
            raise PaperError(f"gross notional {gross:.0f} exceeds {MAX_LEVERAGE:g}× "
                             f"paper equity {equity:.0f} — refused")
    led["cash"] = new_cash
    led["positions"] = new_positions
    if sym not in new_positions:
        led["protective"].pop(sym, None)
    return realized


def _fill_spot(led, o, base_qty, price, fee_rate):
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


# ── lazy settlement (resting limits + protective triggers) ───────────────────

_TERMINAL_FILLED = {"filled"}
_TERMINAL_GONE = {"canceled", "cancelled", "rejected", "expired"}


def _norm_status(raw):
    s = str(raw or "").lower()
    if s in _TERMINAL_FILLED:
        return "filled"
    if s in _TERMINAL_GONE:
        return "canceled"
    if s == "post_only_rejected":
        return "post_only_rejected"
    return "open"


def _settle(env, led):
    """Fill resting limits the mark has reached; fire protective triggers the
    mark has crossed. Called at the top of every ledger transaction, so a read
    is the matching cadence."""
    _prune(led)
    marks = {}
    for o in list(led["orders"].values()):
        if o["status"] != "open":
            continue
        sym = o["symbol"]
        try:
            mark = _price(env, sym, marks)
        except PaperError:
            continue  # can't price this tick — leave the order resting
        px = float(o["price"])
        touched = (mark <= px) if o["side"] == "buy" else (mark >= px)
        if not touched:
            continue
        qty = float(o["orig_qty"])
        # A RESTING (maker) order fills at its own limit price, not the mark:
        # the mark has just crossed through px, so filling at the more-favourable
        # mark would hand the trader a better price than they asked for (audit
        # P2). You get the price you posted.
        fill_px = px
        if o["market"] == "spot":
            try:
                _fill_spot(led, o, qty, fill_px, SPOT_FEE)
            except PaperError as e:
                o["status"] = "canceled"
                o["reason"] = str(e)
            continue
        signed = qty if o["side"] == "buy" else -qty
        if o.get("reduce_only"):
            signed = _cap_reduce(led, sym, signed)
            if signed == 0:
                o["status"] = "canceled"
                o["reason"] = "nothing left to reduce"
                continue
        fee = abs(signed) * fill_px * MAKER_FEE
        try:
            realized = _apply_swap_fill(env, led, sym, signed, fill_px, fee, marks)
        except PaperError as e:
            o["status"] = "canceled"
            o["reason"] = str(e)
            continue
        _record_fill(led, o, abs(signed), fill_px, fee, realized)
    for sym, trig in list(led["protective"].items()):
        pos = led["positions"].get(sym)
        if not pos:
            led["protective"].pop(sym, None)
            continue
        try:
            mark = _price(env, sym, marks)
        except PaperError:
            continue
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
                       abs(signed), None, reduce_only=True,
                       direction="long" if long else "short")
        realized = _apply_swap_fill(env, led, sym, signed, mark, fee, marks)
        _record_fill(led, o, abs(signed), mark, fee, realized, kind=hit)
        led["protective"].pop(sym, None)
        guard.audit("order_ok", venue=VENUE, symbol=sym, intent="protective",
                    kind=hit, qty=abs(signed), price=mark, order_id=o["order_id"])


# ── guard gate ───────────────────────────────────────────────────────────────

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


# ── swap layer (reconciler contract) ─────────────────────────────────────────

def place_market_order(env, symbol, direction, qty, client_order_id=None,
                       reduce_only=False):
    """Confirmed simulated market order: whole qty at the current price (no
    spread). Same client_order_id → the earlier fill (idempotent)."""
    sym = str(symbol).upper()
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be long|short, got {direction!r}")
    if float(qty) <= 0:
        return False
    buy = (direction == "long") != bool(reduce_only)
    side = "buy" if buy else "sell"
    fields = _gate("reduce" if reduce_only else "entry", symbol=sym, side=side,
                   qty=float(qty), client_order_id=client_order_id or "")
    try:
        with _txn(env) as led:
            _settle(env, led)
            prev = _find_order(led, client_order_id=client_order_id) if client_order_id else None
            if prev is not None:
                return _fill_view(prev) if prev["status"] == "filled" else False
            marks = {}
            # An entry must fail loud if the symbol can't be priced (never open
            # at a made-up price); a reduce/close must ALWAYS go through — even
            # if the price source vanished (deleted strategy) — so it falls back
            # to the position's entry (audit P1-2: a position must never become
            # un-flattenable).
            if reduce_only:
                price = _pos_mark(env, led, sym, marks)[0]
                if not price:
                    price = _price(env, sym, marks)  # no position/entry → surface the real error
            else:
                price = _price(env, sym, marks)
            signed = float(qty) if buy else -float(qty)
            if reduce_only:
                signed = _cap_reduce(led, sym, signed)
                if signed == 0:
                    guard.audit("order_ok", order_id="", note="nothing to reduce", **fields)
                    return False
            fee = abs(signed) * price * TAKER_FEE
            o = _new_order(led, sym, "swap", side, "market", price, abs(signed),
                           client_order_id, reduce_only=reduce_only, direction=direction)
            realized = _apply_swap_fill(env, led, sym, signed, price, fee, marks)
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
    """Resting simulated limit (chase contract). With a single price and no
    book, 'crossing' means the limit is already through the mark (buy px≥mark /
    sell px≤mark): post_only rejects that, otherwise it fills now as a taker;
    a resting order fills the first tick the mark reaches its price."""
    sym = str(symbol).upper()
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be long|short, got {direction!r}")
    if float(qty) <= 0:
        return False
    px = float(price)
    buy = (direction == "long") != bool(reduce_only)
    side = "buy" if buy else "sell"
    fields = _gate("reduce" if reduce_only else "entry", symbol=sym, side=side,
                   qty=float(qty), price=px, client_order_id=client_order_id or "",
                   post_only=post_only)
    try:
        with _txn(env) as led:
            _settle(env, led)
            prev = _find_order(led, client_order_id=client_order_id) if client_order_id else None
            if prev is not None:
                return {"order_id": prev["order_id"], "status": prev["status"],
                        "client_order_id": prev.get("client_order_id") or ""}
            marks = {}
            mark = _price(env, sym, marks)
            crosses = (px >= mark) if buy else (px <= mark)
            o = _new_order(led, sym, "swap", side, "limit", px, float(qty), client_order_id,
                           reduce_only=reduce_only, direction=direction, post_only=post_only)
            if crosses and post_only:
                o["status"] = "canceled"
                o["reason"] = "post_only would take liquidity"
                guard.audit("order_ok", order_id=o["order_id"], note="post_only_rejected", **fields)
                return {"status": "post_only_rejected", "order_id": o["order_id"]}
            if crosses:
                signed = float(qty) if buy else -float(qty)
                if reduce_only:
                    signed = _cap_reduce(led, sym, signed)
                    if signed == 0:
                        o["status"] = "canceled"
                        o["reason"] = "nothing left to reduce"
                        return {"order_id": o["order_id"], "status": "canceled"}
                fee = abs(signed) * mark * TAKER_FEE
                realized = _apply_swap_fill(env, led, sym, signed, mark, fee, marks)
                _record_fill(led, o, abs(signed), mark, fee, realized)
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
    """Idempotent-safe: open→canceled; filled/canceled/unknown→already_gone."""
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
    sym = str(symbol).upper() if symbol else None
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


# ── protective (SL/TP) ───────────────────────────────────────────────────────

def place_protective_orders(env, symbol, direction, qty=None, sl_price=None,
                            tp_price=None, verify=True):
    """Arm SL and/or TP on the whole position (qty accepted for parity,
    ignored). Replaces any previous triggers on the symbol."""
    sym = str(symbol).upper()
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
    sym = str(symbol).upper()
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

def place_spot_market_order(env, symbol, side, base_qty=None, quote_qty=None,
                            client_order_id=None):
    """Confirmed simulated spot market — BUY sized in quote (USDT), SELL in
    base, at the current price. Below zero → False."""
    sym = str(symbol).upper()
    side = str(side).lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy|sell, got {side!r}")
    fields = _gate("entry" if side == "buy" else "reduce", symbol=sym, side=side,
                   market="spot", client_order_id=client_order_id or "")
    try:
        with _txn(env) as led:
            _settle(env, led)
            prev = _find_order(led, client_order_id=client_order_id) if client_order_id else None
            if prev is not None:
                return _fill_view(prev) if prev["status"] == "filled" else False
            price = _price(env, sym, {})
            base = _spot_base(sym)
            if side == "buy":
                if quote_qty is None:
                    raise ValueError("spot buys are sized in quote currency — pass quote_qty")
                q = float(quote_qty) / price
            else:
                if base_qty is None:
                    raise ValueError("spot sells are sized in base currency — pass base_qty")
                q = min(float(base_qty), led["spot"].get(base, 0.0))
            if q <= 0:
                guard.audit("order_ok", order_id="", note="zero/again", **fields)
                return False
            o = _new_order(led, sym, "spot", side, "market", price, q, client_order_id)
            _fill_spot(led, o, q, price, SPOT_FEE)
            guard.audit("order_ok", order_id=o["order_id"], price=price, **fields)
            return _fill_view(o)
    except guard.Halted:
        raise
    except Exception as e:
        guard.audit("order_error", error=str(e), **fields)
        raise


def place_spot_limit_order(env, symbol, side, base_qty, price, client_order_id=None,
                           post_only=False):
    sym = str(symbol).upper()
    side = str(side).lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy|sell, got {side!r}")
    if float(base_qty) <= 0:
        return False
    px = float(price)
    fields = _gate("entry" if side == "buy" else "reduce", symbol=sym, side=side,
                   price=px, market="spot", client_order_id=client_order_id or "",
                   post_only=post_only)
    try:
        with _txn(env) as led:
            _settle(env, led)
            prev = _find_order(led, client_order_id=client_order_id) if client_order_id else None
            if prev is not None:
                return {"order_id": prev["order_id"], "status": prev["status"],
                        "client_order_id": prev.get("client_order_id") or ""}
            mark = _price(env, sym, {})
            base = _spot_base(sym)
            if side == "buy" and led["cash"] < float(base_qty) * px * (1 + SPOT_FEE):
                raise PaperError(f"insufficient paper cash to rest a {sym} buy")
            if side == "sell" and led["spot"].get(base, 0.0) + 1e-12 < float(base_qty):
                raise PaperError(f"insufficient {base} to rest a sell")
            crosses = (px >= mark) if side == "buy" else (px <= mark)
            o = _new_order(led, sym, "spot", side, "limit", px, float(base_qty),
                           client_order_id, post_only=post_only)
            if crosses and post_only:
                o["status"] = "canceled"
                o["reason"] = "post_only would take liquidity"
                guard.audit("order_ok", order_id=o["order_id"], note="post_only_rejected", **fields)
                return {"status": "post_only_rejected", "order_id": o["order_id"]}
            if crosses:
                _fill_spot(led, o, float(base_qty), mark, SPOT_FEE)
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
    """Settled, marked-to-market view for account_paper."""
    with _txn(env) as led:
        _settle(env, led)
        marks = {}
        equity, upnl, spot_value, prices = _equity(env, led, marks)
        positions = []
        for sym, p in led["positions"].items():
            # _pos_mark falls back to entry for an unpriceable symbol, so
            # mark_price is ALWAYS a real number (never None) — account_paper /
            # venue_wiring float() it and size×mark_price it (audit P1-2 follow-up).
            mk, _ = _pos_mark(env, led, sym, marks)
            positions.append({"symbol": sym, "side": "long" if p["qty"] > 0 else "short",
                              "size": abs(p["qty"]), "entry_price": p["entry"],
                              "mark_price": mk})
        return {"equity": equity, "cash": led["cash"], "unrealized": upnl,
                "spot_value": spot_value, "positions": positions,
                "spot": {a: {"amount": v, "price": prices.get(a)}
                         for a, v in led["spot"].items() if v > 0},
                "created_ts": led["created_ts"], "initial_cash": led["initial_cash"],
                "fills": list(led["fills"][-50:])}


def reset_account(env, cash=None):
    """Wipe positions/orders/fills and re-seed cash. Explicit user request only."""
    fields = _gate("reduce", symbol="*", note="reset_account")
    with _txn(env) as led:
        fresh = _new_ledger(env)
        # created_ts must not trail PAPER_BOUND_TS, or a machine clock behind the
        # (browser-set) bind stamp makes the next _load judge this fresh ledger
        # "older than the bind" and re-seed again — silently undoing the reset
        # (audit P2; same clock-skew guard _load uses).
        fresh["created_ts"] = max(fresh["created_ts"], _bound_ts(env))
        if cash:
            fresh["cash"] = fresh["initial_cash"] = float(cash)
        led.clear()
        led.update(fresh)
        guard.audit("order_ok", order_id="reset", **fields)
        return {"equity": led["cash"], "currency": "USDT"}
