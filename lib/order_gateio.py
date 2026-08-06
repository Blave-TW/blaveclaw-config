"""
Gate.io order execution library — USDT perpetual futures + spot.

Companion to lib/account_gateio.py (equity/positions). Credentials:
GATEIO_API_KEY / GATEIO_SECRET_KEY in .env (web 下單設定 names;
GATE_API_KEY / GATE_SECRET_KEY accepted as a fallback).

Broker attribution (Blave) is the header `X-Gate-Channel-Id: blave` on EVERY
request — injected by the transport here, callers never handle it.

Design rules (same contract as order_binance / order_bingx / order_okx):
FAIL LOUD / CONFIRMED-NOT-SENT / HALTABLE+AUDITED via lib/guard /
ATTRIBUTION on every request. Idempotency caveat: the `text` custom id is a
LABEL — Gate.io does NOT reject duplicate ids AT ALL (measured live
2026-08-05: two orders with the identical text both filled; there is not
even Binance/OKX's un-filled-window dedup), so on OrderNotConfirmed always
re-query (get_open_orders / order list), never blindly resubmit.

Gate.io quirks handled here (SDK-cross-checked AND measured live 2026-08-05
on a real account, single and dual mode, futures + spot):
- futures `size` is signed CONTRACTS (positive=buy/long, negative=sell/
  short) — base qty is converted via the contract's quanto_multiplier
  (the OKX ctVal pitfall; callers always pass BASE units, conversion lives
  only in this file, Decimal end-to-end)
- market order = price "0" + tif "ioc"; whole-position close = size 0 +
  close true (single mode) / auto_size close_long|close_short (dual mode)
- dual (hedge) mode: reduce_only + order sign uniquely addresses the side
  (a reduce-only sell can only reduce the long side)
- conditional (SL/TP) orders live on /futures/usdt/price_orders with their
  own id namespace + open/cancel endpoints — regular order queries never
  see them
- spot market BUYs size in QUOTE currency (amount=USDT), SELLs in base —
  Gate.io's native semantics, exactly the cross-venue spot contract
- spot BUY fees are deducted from the RECEIVED BASE asset (measured: bought
  0.000093 BTC, fee 9.3e-8 BTC) — executed_qty is NOT what landed in the
  wallet; to sell inventory down, size from get_spot_balances, never from a
  buy's executed_qty (selling executed_qty raises BALANCE_NOT_ENOUGH)
- a futures account does not EXIST until the first transfer into it
  (USER_NOT_FOUND — handled as a state in lib/account_gateio, not a broken
  key); an untouched contract has no position record (POSITION_NOT_FOUND —
  get_leverage returns 0)
- errors are {"label", "message"} with 4xx/5xx — label is the machine-
  readable part and is surfaced first
- attribution lands verifiably: fills carry biz_info "ch:blave", price
  orders carry broker "blave" (measured on live orders)
"""

import hashlib
import hmac
import json
import time
from decimal import Decimal, ROUND_DOWN

import requests

from lib import guard

HOST = "https://api.gateio.ws"
PREFIX = "/api/v4"

_rules_cache = {}      # contract/pair -> rules (per-process)
_pos_mode_cache = {}   # api_key -> "single" | "dual"


class GateioError(Exception):
    """Raised on any Gate.io error response or unexpected shape. code carries
    the response's machine-readable `label` (HTTP status as fallback)."""

    def __init__(self, code, msg, path=""):
        self.code = code
        self.msg = msg
        super().__init__(f"Gate.io error {code}: {msg or '(empty msg)'} | {path}")


class OrderNotConfirmed(Exception):
    """Order accepted but not terminal within timeout. It may still fill —
    query again, never blindly resubmit (text ids do NOT dedup here)."""


class ProtectionFailed(Exception):
    """Position is OPEN but a protective (SL/TP) order could not be placed or
    verified. The position is NAKED — alert the user immediately."""


# ── transport ────────────────────────────────────────────────────────────────

def _creds(env):
    api_key = env.get("GATEIO_API_KEY") or env.get("GATE_API_KEY")
    secret = env.get("GATEIO_SECRET_KEY") or env.get("GATE_SECRET_KEY")
    if not api_key or not secret:
        raise ValueError("GATEIO_API_KEY / GATEIO_SECRET_KEY missing from .env")
    return api_key, secret


def _reduce_semantics(p):
    return (str(p.get("reduce_only", "")).lower() == "true"
            or p.get("reduce_only") is True
            or p.get("close") is True
            or str(p.get("close", "")).lower() == "true"
            or p.get("auto_size") in ("close_long", "close_short"))


def _order_intent(method, path, body):
    """'entry' | 'reduce' | 'protective' | 'cancel' | None. Same S1 rule as
    the other libs: a conditional TYPE alone is not protective — it must
    carry reduce semantics, otherwise it is an entry and HALT blocks it."""
    if method == "DELETE" and ("/orders" in path or "/price_orders" in path):
        return "cancel"
    if method != "POST":
        return None
    p = body or {}
    if path == "/futures/usdt/orders":
        return "reduce" if _reduce_semantics(p) else "entry"
    if path == "/futures/usdt/price_orders":
        return "protective" if _reduce_semantics(p.get("initial") or {}) else "entry"
    if path == "/spot/orders":
        # spot has no positions: BUY builds exposure, SELL liquidates inventory
        return "reduce" if p.get("side") == "sell" else "entry"
    return None


_AUDIT_PARAM_KEYS = ("contract", "currency_pair", "side", "size", "price",
                     "tif", "reduce_only", "close", "auto_size", "amount",
                     "type", "text")


def _audit_fields(body):
    p = body or {}
    fields = {k: p[k] for k in _AUDIT_PARAM_KEYS if k in p}
    init = p.get("initial") or {}
    fields.update({k: init[k] for k in _AUDIT_PARAM_KEYS if k in init})
    trig = p.get("trigger") or {}
    if "price" in trig:
        fields["trigger_price"] = trig["price"]
    return fields


def _request(method, path, env, body=None, query="", retries=3):
    """Signed request with guard gate + audit on order-mutating calls."""
    intent = _order_intent(method, path, body)
    if intent is not None:
        fields = _audit_fields(body)
        fields["intent"] = intent
        if intent == "entry" and guard.halted():
            guard.audit("order_denied_halt", **fields)
            raise guard.Halted(
                f"state/HALT is set ({guard.halt_info()}) — entry order for "
                f"{fields.get('contract') or fields.get('currency_pair')} refused "
                f"before reaching the exchange. Closes, SL/TP and cancels still "
                f"work. Only the user may clear the halt (guard.clear_halt)."
            )
        guard.audit("order_attempt", **fields)
        try:
            data = _send(method, path, env, body, query, retries)
        except Exception as e:
            guard.audit("order_error", error=str(e), **fields)
            raise
        oid = data.get("id") if isinstance(data, dict) else ""
        guard.audit("order_ok", order_id=str(oid or ""), **fields)
        return data
    return _send(method, path, env, body, query, retries)


def _send(method, path, env, body=None, query="", retries=3):
    api_key, secret = _creds(env)
    body_str = "" if body is None else json.dumps(body)
    last_err = None
    for attempt in range(retries):
        ts = str(int(time.time()))
        payload_hash = hashlib.sha512(body_str.encode()).hexdigest()
        sign_str = "\n".join([method.upper(), PREFIX + path, query, payload_hash, ts])
        sig = hmac.new(secret.encode(), sign_str.encode(), hashlib.sha512).hexdigest()
        headers = {
            "KEY": api_key,
            "Timestamp": ts,
            "SIGN": sig,
            "Content-Type": "application/json",
            "X-Gate-Channel-Id": "blave",  # broker attribution — MANDATORY
        }
        url = HOST + PREFIX + path + ("?" + query if query else "")
        try:
            r = requests.request(method, url, headers=headers,
                                 data=body_str or None, timeout=10)
        except requests.exceptions.ConnectionError as e:
            # POST only fails loud: a reset AFTER the request went out means the
            # order may have landed even though the response died, and Gate.io
            # `text` ids have ZERO dedup (measured: two identical-text orders
            # both fill) — a blind resend can double a position. The reconciler
            # re-derives the diff from live positions next round, which is the
            # only reliable "did it land" check. GET/DELETE stay retryable.
            if method.upper() == "POST":
                raise
            last_err = e
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)
            continue
        if not r.ok:
            try:
                err = r.json()
            except ValueError:
                err = {}
            raise GateioError(err.get("label", r.status_code),
                              err.get("message", r.text[:120]), path)
        if not r.text:
            return {}  # DELETE endpoints return empty bodies
        try:
            return r.json()
        except ValueError:
            raise GateioError(r.status_code, f"non-JSON response ({r.text[:120]})", path)
    raise last_err


# ── symbols / rules ─────────────────────────────────────────────────────────

def _contract(sym):
    """Canonical dashless (BTCUSDT) → Gate.io contract (BTC_USDT)."""
    sym = (sym or "").replace("-", "").replace("_", "").upper()
    for quote in ("USDT", "USDC"):
        if sym.endswith(quote) and len(sym) > len(quote):
            return f"{sym[:-len(quote)]}_{quote}"
    raise ValueError(f"cannot derive Gate.io contract from {sym!r}")


def _futures_rules(env, symbol):
    c = _contract(symbol)
    key = f"fut:{c}"
    if key not in _rules_cache:
        r = _send("GET", f"/futures/usdt/contracts/{c}", env)
        _rules_cache[key] = {
            "multiplier": str(r.get("quanto_multiplier") or "1"),  # base per contract
            "min_ct": int(r.get("order_size_min") or 1),           # contracts
            "tick": str(r.get("order_price_round") or "0.01"),
            "active": not r.get("in_delisting", False),
        }
    return _rules_cache[key]


def get_contract_rules(env, symbol):
    """Cross-venue contract: {'step','min_qty','min_notional','contract_value'}
    with min_qty in BASE units (min contracts × multiplier). step is the sz
    step in CONTRACTS (always 1 on Gate.io) — use format_qty, never convert
    by hand."""
    r = _futures_rules(env, symbol)
    return {
        "step": "1",
        "min_qty": r["min_ct"] * float(r["multiplier"]),
        "min_notional": 0.0,  # Gate.io futures gate on min contracts, not notional
        "contract_value": float(r["multiplier"]),
        "price_tick": r["tick"],
        "active": r["active"],
    }


def format_qty(env, symbol, qty, price=None):
    """BASE qty → CONTRACTS string (integer) floored. Raises ValueError below
    the minimum or on a delisting contract. Callers use this ONLY as the
    min-size gate — never feed the return into place_market_order (it would
    re-convert contracts→contracts and oversize N× — the manager.md pitfall)."""
    r = _futures_rules(env, symbol)
    if not r["active"]:
        raise ValueError(f"{symbol} is not open for trading")
    # Decimal all the way: float division under-sizes by a whole contract at
    # exact multiples (the OKX 0.01/0.1 lesson)
    contracts = int(Decimal(str(qty)) / Decimal(r["multiplier"]))
    if contracts < r["min_ct"] or contracts <= 0:
        raise ValueError(
            f"{symbol} qty {qty} = {contracts} contracts, below minimum {r['min_ct']}"
        )
    return str(contracts)


def format_price(env, symbol, price):
    r = _futures_rules(env, symbol)
    tick = Decimal(r["tick"]).normalize()
    return format(Decimal(str(price)).quantize(tick, rounding=ROUND_DOWN), "f")


def _cid():
    """Gate.io custom id: `t-` prefix, ≤28 bytes after it, [0-9A-Za-z_.-]."""
    return "t-rc" + time.strftime("%y%m%d%H%M%S") + f"{int(time.time() * 1000) % 1000:03d}"


def _text(client_order_id=None):
    if not client_order_id:
        return _cid()
    cid = str(client_order_id)
    out = cid if cid.startswith("t-") else f"t-{cid}"
    # over-long raises instead of truncating (order_binance.py convention):
    # a silently shortened id desyncs the caller's records from the audit log
    if len(out.encode()) > 30:  # "t-" + ≤28 bytes
        raise ValueError(f"client_order_id too long for Gate.io ({cid[:40]}...)")
    return out


# ── position mode ───────────────────────────────────────────────────────────

def get_position_mode(env):
    """'single' | 'dual' (hedge). Cached per api key per process."""
    key, _ = _creds(env)
    if key not in _pos_mode_cache:
        acct = _send("GET", "/futures/usdt/accounts", env)
        _pos_mode_cache[key] = "dual" if acct.get("in_dual_mode") else "single"
    return _pos_mode_cache[key]


# ── orders (futures) ────────────────────────────────────────────────────────

def get_order(env, symbol, order_id):
    """Futures order by id, normalized. executed_qty is BASE units (size/left
    are contracts — converted here). Order responses carry no fee; commission
    is fetched best-effort from my_trades."""
    o = _send("GET", f"/futures/usdt/orders/{order_id}", env)
    if "status" not in o:
        raise GateioError("N/A", f"order query missing status: {list(o)}", "orders")
    mult = float(_futures_rules(env, symbol)["multiplier"])
    size = float(o.get("size", 0) or 0)
    left = float(o.get("left", 0) or 0)
    filled_ct = abs(size - left)
    status = o.get("status")          # open | finished
    finish_as = o.get("finish_as")    # filled | cancelled | ioc | ...
    if status == "finished":
        norm_status = "filled" if (finish_as == "filled" or filled_ct > 0) else "canceled"
    else:
        norm_status = "open"
    commission, commission_asset = 0.0, ""
    if status == "finished" and filled_ct > 0:
        try:  # best-effort — fee lives on trades, not the order
            trades = _send("GET", "/futures/usdt/my_trades", env,
                           query=f"contract={_contract(symbol)}&order={order_id}")
            commission = sum(abs(float(t.get("fee", 0) or 0)) for t in trades)
            commission_asset = "USDT" if commission else ""
        except Exception:
            pass
    return {
        "order_id": str(o.get("id", "")),
        "status": norm_status,
        "avg_price": float(o.get("fill_price", 0) or 0),
        "executed_qty": filled_ct * mult,
        "commission": commission,
        "commission_asset": commission_asset,
        "client_order_id": o.get("text") or "",
        "raw": o,
    }


def _confirm(env, symbol, order_id, getter, timeout=15):
    deadline = time.time() + timeout
    order = None
    while time.time() < deadline:
        order = getter(env, symbol, order_id)
        if order["status"] == "filled":
            return order
        if order["status"] == "canceled":
            raise GateioError("N/A", f"order {order_id} ended canceled with zero fill",
                              "confirm")
        time.sleep(1)
    raise OrderNotConfirmed(
        f"order {order_id} still {order['status'] if order else 'UNKNOWN'} after "
        f"{timeout}s — re-query before any resubmit (text ids do not dedup)"
    )


def place_market_order(env, symbol, direction, qty, client_order_id=None,
                       reduce_only=False, confirm_timeout=15):
    """Confirmed futures market order (price "0" + tif ioc). qty is BASE units
    (converted to signed contracts internally). direction: 'long'|'short' =
    the position this order builds (when reduce_only, the position it
    reduces). Below min contracts → False (intentional skip). Dual (hedge)
    mode needs no posSide field — reduce_only + the order's sign uniquely
    addresses the side (a reduce-only sell can only reduce the long side)."""
    c = _contract(symbol)
    try:
        ct = int(format_qty(env, symbol, qty))
    except ValueError as e:
        if "not open for trading" in str(e):
            raise
        return False
    if reduce_only:
        signed = -ct if direction == "long" else ct   # sell closes long
    else:
        signed = ct if direction == "long" else -ct
    body = {
        "contract": c,
        "size": signed,
        "price": "0",
        "tif": "ioc",
        "text": _text(client_order_id),
    }
    if reduce_only:
        body["reduce_only"] = True
    placed = _request("POST", "/futures/usdt/orders", env, body)
    order_id = placed.get("id")
    if not order_id:
        raise GateioError("N/A", f"order response missing id: {placed}", "orders")
    return _confirm(env, symbol, order_id, get_order, timeout=confirm_timeout)


def get_mark_price(env, symbol):
    """Live mark price (public futures ticker) — the cross-venue wiring
    contract for USD→qty conversion (lib/venue_wiring.py)."""
    rows = _send("GET", "/futures/usdt/tickers", env,
                 query=f"contract={_contract(symbol)}")
    t = rows[0] if rows else {}
    return float(t.get("mark_price") or t.get("last"))


def close_position(env, symbol, direction, qty, client_order_id=None):
    """Market-close (part of) an existing position, confirmed. direction is
    the POSITION being closed ('long'|'short')."""
    return place_market_order(env, symbol, direction, qty,
                              client_order_id=client_order_id, reduce_only=True)


def close_position_partial(env, symbol, direction, qty, client_order_id=None):
    """Cross-venue reconciler contract name — reduce the position by a BASE
    qty, reduce-only semantics, works under HALT."""
    return close_position(env, symbol, direction, qty,
                          client_order_id=client_order_id)


def get_open_orders(env, symbol=None):
    query = "status=open"
    if symbol:
        query += f"&contract={_contract(symbol)}"
    return _send("GET", "/futures/usdt/orders", env, query=query)


def cancel_order(env, symbol, order_id):
    return _request("DELETE", f"/futures/usdt/orders/{order_id}", env)


def cancel_all_orders(env, symbol):
    """Cancel all open orders AND all open conditional (price-triggered)
    orders on the contract."""
    c = _contract(symbol)
    orders = _request("DELETE", "/futures/usdt/orders", env, query=f"contract={c}")
    algos = _request("DELETE", "/futures/usdt/price_orders", env, query=f"contract={c}")
    return {"orders": orders, "algo_orders": algos}


# ── protective orders (futures) ─────────────────────────────────────────────

def get_open_algo_orders(env, symbol=None):
    query = "status=open"
    if symbol:
        query += f"&contract={_contract(symbol)}"
    return _send("GET", "/futures/usdt/price_orders", env, query=query)


def cancel_algo_order(env, symbol, algo_id):
    return _request("DELETE", f"/futures/usdt/price_orders/{algo_id}", env)


def place_protective_orders(env, symbol, direction, qty=None, sl_price=None,
                            tp_price=None, verify=True):
    """Reduce-only conditional SL/TP for an EXISTING futures position via
    /futures/usdt/price_orders (market execution: initial price "0" + tif
    ioc; trigger on MARK price). direction is the POSITION's direction.
    qty=None protects the whole position (close=true / dual-mode auto_size —
    survives size changes). Verifies the orders exist in open price orders;
    raises ProtectionFailed otherwise — the caller MUST alert the user
    (naked position). Returned dicts carry id (str) — cancel via
    cancel_algo_order, not cancel_order."""
    c = _contract(symbol)
    mode = get_position_mode(env)
    placed = {}
    try:
        for kind, trigger in (("sl", sl_price), ("tp", tp_price)):
            if trigger is None:
                continue
            initial = {"contract": c, "price": "0", "tif": "ioc", "text": _cid()}
            if qty is None:
                initial["size"] = 0
                if mode == "dual":
                    initial["auto_size"] = ("close_long" if direction == "long"
                                            else "close_short")
                    initial["reduce_only"] = True
                else:
                    initial["close"] = True
            else:
                ct = int(format_qty(env, symbol, qty))
                initial["size"] = -ct if direction == "long" else ct
                initial["reduce_only"] = True
            # rule 1 = trigger when price >= , 2 = when price <=
            if direction == "long":
                rule = 2 if kind == "sl" else 1
            else:
                rule = 1 if kind == "sl" else 2
            body = {
                "initial": initial,
                "trigger": {
                    "strategy_type": 0,               # price-triggered
                    "price_type": 1,                  # mark price
                    "price": format_price(env, symbol, trigger),
                    "rule": rule,
                },
            }
            order = _request("POST", "/futures/usdt/price_orders", env, body)
            if not order.get("id"):
                raise GateioError("N/A", f"{kind} response missing id: {order}",
                                  "price_orders")
            placed[kind] = {"algoId": str(order["id"]), "raw": order}
    except Exception as e:
        raise ProtectionFailed(
            f"{symbol} {direction} position may be NAKED — placing protective orders "
            f"failed after {list(placed)} succeeded: {e}"
        ) from e

    if verify and placed:
        open_ids = {str(o.get("id")) for o in get_open_algo_orders(env, symbol)}
        missing = [k for k, o in placed.items() if o["algoId"] not in open_ids]
        if missing:
            raise ProtectionFailed(
                f"{symbol} {direction}: protective order(s) {missing} accepted but "
                f"NOT in open price orders — verify manually, position may be naked"
            )
    return placed


def open_position(env, symbol, direction, qty, sl_price=None, tp_price=None,
                  client_order_id=None, confirm_timeout=15):
    """Confirmed market entry, then protective orders (two-step — same naked-
    window contract as order_binance/order_okx: as small as one round-trip,
    ProtectionFailed if protection isn't verified on the exchange)."""
    entry = place_market_order(env, symbol, direction, qty,
                               client_order_id=client_order_id,
                               confirm_timeout=confirm_timeout)
    if entry is False:
        return False
    protection = {}
    if sl_price is not None or tp_price is not None:
        protection = place_protective_orders(env, symbol, direction, qty=None,
                                             sl_price=sl_price, tp_price=tp_price)
    return {"entry": entry, "protection": protection}


# ── leverage ─────────────────────────────────────────────────────────────────

def set_leverage(env, symbol, leverage):
    """Set ISOLATED leverage. Cross mode (leverage 0 + cross_leverage_limit
    query param) is NOT wired here — unverified against a live account; add the
    param and verify before using. Never silently inherit: query first, set
    only on user instruction (references/manager.md confirmation rules)."""
    return _send("POST", f"/futures/usdt/positions/{_contract(symbol)}/leverage",
                 env, query=f"leverage={int(leverage)}")


def get_leverage(env, symbol):
    try:
        p = _send("GET", f"/futures/usdt/positions/{_contract(symbol)}", env)
    except GateioError as e:
        # a contract never touched has no position record, and a futures
        # account not yet funded doesn't exist at all — both mean "no leverage
        # set", 0 = venue default (USER_NOT_FOUND-as-state matches account_gateio)
        if e.code in ("POSITION_NOT_FOUND", "USER_NOT_FOUND"):
            return 0
        raise
    return int(float(p.get("leverage", 0) or 0))


# ── spot execution (MARKET="spot" strategies) ───────────────────────────────

def _spot_pair(sym):
    """Canonical dashless (BTCUSDT) → Gate.io spot pair (BTC_USDT)."""
    return _contract(sym)


def _spot_rules(env, symbol):
    pair = _spot_pair(symbol)
    key = f"spot:{pair}"
    if key not in _rules_cache:
        r = _send("GET", f"/spot/currency_pairs/{pair}", env)
        _rules_cache[key] = {
            "amount_step": str(Decimal(1).scaleb(-int(r.get("amount_precision") or 0))
                               .normalize()),
            "min_base": float(r.get("min_base_amount") or 0),
            "min_quote": float(r.get("min_quote_amount") or 0),
            "active": r.get("trade_status") == "tradable",
        }
    return _rules_cache[key]


def get_spot_rules(env, symbol):
    """{'step','min_qty','min_notional','active'} — spot amounts are BASE
    units on sells; min_notional is Gate.io's min_quote_amount (market buys
    are quote-sized, checked against it directly)."""
    r = _spot_rules(env, symbol)
    return {"step": r["amount_step"], "min_qty": r["min_base"],
            "min_notional": r["min_quote"], "active": r["active"]}


def format_spot_qty(env, symbol, qty):
    r = _spot_rules(env, symbol)
    if not r["active"]:
        raise ValueError(f"{symbol} is not open for spot trading")
    q = Decimal(str(qty)).quantize(Decimal(r["amount_step"]).normalize(),
                                   rounding=ROUND_DOWN)
    if float(q) < r["min_base"] or float(q) <= 0:
        raise ValueError(
            f"{symbol} spot qty {qty} floors to {q}, below minimum {r['min_base']}"
        )
    return format(q, "f")


def get_spot_order(env, symbol, order_id):
    """Spot order by id — fee rides the order itself here (no trades call).
    executed_qty is BASE units (filled quote ÷ avg price)."""
    o = _send("GET", f"/spot/orders/{order_id}", env,
              query=f"currency_pair={_spot_pair(symbol)}")
    if "status" not in o:
        raise GateioError("N/A", f"spot order query missing status: {list(o)}", "orders")
    avg = float(o.get("avg_deal_price", 0) or 0)
    quote_filled = float(o.get("filled_total", 0) or 0)
    executed = quote_filled / avg if avg > 0 else 0.0
    status = o.get("status")  # open | closed | cancelled
    if status == "closed":
        norm_status = "filled"
    elif status == "cancelled":
        # ioc market orders park the un-filled remainder as cancelled —
        # a partial fill is still a fill
        norm_status = "filled" if executed > 0 else "canceled"
    else:
        norm_status = "open"
    return {
        "order_id": str(o.get("id", "")),
        "status": norm_status,
        "avg_price": avg,
        "executed_qty": executed,
        "quote_qty": quote_filled,
        "commission": abs(float(o.get("fee", 0) or 0)),
        "commission_asset": o.get("fee_currency") or "",
        "client_order_id": o.get("text") or "",
        "raw": o,
    }


def place_spot_market_order(env, symbol, side, base_qty=None, quote_qty=None,
                            client_order_id=None, confirm_timeout=15):
    """Confirmed spot market order. side: 'buy'|'sell'. BUYs size in QUOTE
    currency (Gate.io native: market-buy amount IS the quote spend); SELLs
    in base qty floored to the pair's amount step. Below min → False.
    Guard: buy=entry (halt blocks), sell=reduce (never trapped)."""
    pair = _spot_pair(symbol)
    side = side.lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy|sell, got {side!r}")
    r = _spot_rules(env, symbol)
    if not r["active"]:
        raise ValueError(f"{symbol} is not open for spot trading")
    body = {
        "currency_pair": pair,
        "side": side,
        "type": "market",
        "time_in_force": "ioc",
        "account": "spot",
        "text": _text(client_order_id),
    }
    if side == "buy":
        if quote_qty is None:
            raise ValueError("spot buys are sized in quote currency — pass quote_qty")
        if float(quote_qty) < r["min_quote"]:
            return False
        body["amount"] = f"{float(quote_qty):.2f}"
    else:
        if base_qty is None:
            raise ValueError("spot sells are sized in base currency — pass base_qty")
        try:
            body["amount"] = format_spot_qty(env, symbol, base_qty)
        except ValueError as e:
            if "not open for spot trading" in str(e):
                raise
            return False
    placed = _request("POST", "/spot/orders", env, body)
    order_id = placed.get("id")
    if not order_id:
        raise GateioError("N/A", f"spot order response missing id: {placed}", "orders")
    return _confirm(env, symbol, order_id, get_spot_order, timeout=confirm_timeout)


def get_spot_balances(env):
    """{asset: available+locked} in the SPOT account."""
    rows = _send("GET", "/spot/accounts", env)
    out = {}
    for r in rows:
        amt = float(r.get("available", 0) or 0) + float(r.get("locked", 0) or 0)
        if amt > 0:
            out[r.get("currency")] = amt
    return out


def get_spot_price(env, symbol):
    rows = _send("GET", "/spot/tickers", env,
                 query=f"currency_pair={_spot_pair(symbol)}")
    return float((rows[0] if rows else {})["last"])
