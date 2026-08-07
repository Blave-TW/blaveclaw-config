"""
Binance USDⓈ-M futures order execution library.

Companion to lib/account_binance.py (equity/positions). Credentials:
BINANCE_API_KEY, BINANCE_SECRET_KEY in .env. Set BINANCE_DEMO=true to run the
same code against the futures testnet (demo-fapi.binance.com — needs testnet
keys, unlike BingX's VST which reuses live keys).

Broker attribution (Blave) is PER ORDER on Binance, not a header: every order
placement carries newClientOrderId starting with x-52DDFAFN (USDS-M futures
broker ID). This lib builds the id itself — callers pass a plain
client_order_id suffix and never handle the prefix.

Design rules (same contract as lib/order_bingx.py — non-negotiable):

1. FAIL LOUD. Every unexpected response shape raises BinanceError with the
   exchange's own code and message.
2. CONFIRMED, NOT SENT. Orders are polled to a terminal state; callers get
   what the EXCHANGE says (avgPrice, executedQty, commission) — never intent.
3. NO NAKED POSITIONS (best effort). Binance futures has no atomic SL/TP
   attach on the entry order (unlike BingX) — open_position places the entry,
   then protective orders, and VERIFIES them on the exchange; a protection
   failure raises ProtectionFailed, which callers must surface immediately.
4. IDEMPOTENT (bounded). newClientOrderId is unique per intent; Binance
   rejects a duplicate against an OPEN order (-2022/-4116 family) but reuses
   ids freely once the order is terminal — so on OrderNotConfirmed, re-query;
   never blindly resubmit.
5. HALTABLE + AUDITED. Every order-mutating request passes through lib/guard:
   state/HALT blocks entry orders BEFORE any network call (reduce-only closes,
   SL/TP and cancels still pass — flattening must never be trapped), and every
   attempt/outcome/denial lands in state/audit.jsonl.

Verified against ccxt's production binance parser + official docs + live
orders on the real account (2026-08-04). Binance quirks handled here:
quantity is BASE units already (no contract conversion anywhere — ctVal=1);
hedge mode REJECTS the reduceOnly param (-1106; closing is expressed by
side-opposite-positionSide instead); order query returns no commission
(fetched best-effort from userTrades); dualSidePosition is a real JSON
boolean (BingX returns the string "true"); orderId is a large integer (kept
as str); CONDITIONAL orders (SL/TP/trailing) live on a SEPARATE Algo Order
API — /fapi/v1/order rejects STOP_MARKET with -4120 (measured live) — with
its own vocabulary: triggerPrice not stopPrice, clientAlgoId not
newClientOrderId (broker prefix still applies), algoId/algoStatus in
responses, and their own open/cancel endpoints (openAlgoOrders /
algoOpenOrders) — /fapi/v1/openOrders does NOT list them.
"""

import hashlib
import hmac
import os
import time
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

import requests

from lib import guard

LIVE_URL = "https://fapi.binance.com"
DEMO_URL = "https://demo-fapi.binance.com"  # futures testnet (testnet keys)
SPOT_LIVE_URL = "https://api.binance.com"
SPOT_DEMO_URL = "https://testnet.binance.vision"  # spot testnet (own keys)

BROKER_PREFIX = "x-52DDFAFN"  # Blave USDS-M futures broker ID — every order
CID_MAX = 36                  # Binance newClientOrderId hard limit

RETRYABLE_CODES = {-1001}  # internal error / disconnected. Rate limits
                           # (-1003 / HTTP 429) are deliberately NOT retried:
                           # hammering through them escalates to an IP ban.
RECV_WINDOW = "5000"

_rules_cache = {}          # symbol -> contract rules (per-process)
_position_mode_cache = {}  # api_key -> "hedge" | "oneway"
_time_offset = {"ms": 0}   # server-clock correction, set on -1021


class BinanceError(Exception):
    """Raised on any Binance error response or unexpected shape."""

    def __init__(self, code, msg, path=""):
        self.code = code
        self.msg = msg
        super().__init__(f"Binance error {code}: {msg or '(empty msg)'} | {path}")


class OrderNotConfirmed(Exception):
    """Order was accepted but did not reach a terminal state within timeout.
    The order may still fill — query it again before assuming anything."""


class ProtectionFailed(Exception):
    """Position is OPEN but a protective (SL/TP) order could not be placed or
    verified. The position is NAKED — alert the user immediately."""


# ── transport ────────────────────────────────────────────────────────────────

def _base(env, spot=False):
    demo = str(env.get("BINANCE_DEMO", os.environ.get("BINANCE_DEMO", ""))).lower() == "true"
    if spot:
        return SPOT_DEMO_URL if demo else SPOT_LIVE_URL
    return DEMO_URL if demo else LIVE_URL


def _canonical(sym):
    """Canonical == Binance futures format already (dashless uppercase).
    Strip separators defensively so 'BTC-USDT' from a stray caller still works."""
    return (sym or "").replace("-", "").replace("_", "").upper()


def _sync_time(env):
    """-1021 = our clock drifted outside recvWindow. Correct against the
    venue's clock instead of failing every order on machine drift."""
    server = requests.get(f"{_base(env)}/fapi/v1/time", timeout=10).json()["serverTime"]
    _time_offset["ms"] = int(server) - int(time.time() * 1000)


# Order-mutating endpoints and how to classify their intent for the guard.
_MUTATING_PATHS = {
    ("POST", "/api/v3/order"): "place",
    ("DELETE", "/api/v3/order"): "cancel",
    ("DELETE", "/api/v3/openOrders"): "cancel_all",
    ("POST", "/fapi/v1/order"): "place",
    ("POST", "/fapi/v1/algoOrder"): "place",
    ("DELETE", "/fapi/v1/order"): "cancel",
    ("DELETE", "/fapi/v1/algoOrder"): "cancel",
    ("DELETE", "/fapi/v1/allOpenOrders"): "cancel_all",
    ("DELETE", "/fapi/v1/algoOpenOrders"): "cancel_all",
    ("POST", "/fapi/v1/leverage"): "leverage",
}

_PROTECTIVE_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT",
                     "TRAILING_STOP_MARKET"}

# Params worth keeping in the audit line (no secrets live in params — the
# signature and timestamp are added later, inside _send's own copy).
_AUDIT_PARAM_KEYS = ("symbol", "side", "positionSide", "type", "quantity",
                     "price", "stopPrice", "triggerPrice", "newClientOrderId",
                     "clientAlgoId", "reduceOnly", "closePosition", "leverage",
                     "orderId", "algoId")


def _order_intent(method, path, params):
    """'entry' | 'reduce' | 'protective' | 'cancel' | 'cancel_all' |
    'leverage' | None (not order-mutating). Hedge mode has no reduceOnly
    flag — there, closing = side opposite to positionSide.

    A conditional TYPE alone does not make an order protective (audited
    2026-08-04): a STOP_MARKET BUY with no reduce semantics is a breakout
    ENTRY, and classifying it protective would let it sail through HALT.
    Every legitimate protective order this lib produces carries reduce
    semantics (closePosition / reduceOnly / hedge opposite-side), so require
    them — conditional without reduce semantics is an entry and gets halted."""
    kind = _MUTATING_PATHS.get((method, path))
    if kind != "place":
        return kind
    p = params or {}
    if path == "/api/v3/order":
        # spot has no positions: BUY builds exposure (halt blocks it), SELL
        # liquidates inventory (never trapped — same rule as reduce-only)
        return "reduce" if p.get("side") == "SELL" else "entry"
    is_reduce = (str(p.get("reduceOnly", "")).lower() == "true" or
                 str(p.get("closePosition", "")).lower() == "true")
    ps, side = p.get("positionSide"), p.get("side")
    if (ps == "LONG" and side == "SELL") or (ps == "SHORT" and side == "BUY"):
        is_reduce = True
    if p.get("type") in _PROTECTIVE_TYPES:
        return "protective" if is_reduce else "entry"
    return "reduce" if is_reduce else "entry"


def _request(method, path, env, params=None, signed=True, retries=3, spot=False):
    """Gate + audit wrapper around _send. Reads pass straight through;
    order-mutating requests are halt-checked and audited (design rule 5)."""
    intent = _order_intent(method, path, params)
    if intent is None:
        return _send(method, path, env, params, signed, retries, spot)

    fields = {k: params[k] for k in _AUDIT_PARAM_KEYS if k in (params or {})}
    fields["intent"] = intent
    fields["demo"] = _base(env) == DEMO_URL

    if intent == "entry" and guard.halted():
        guard.audit("order_denied_halt", **fields)
        raise guard.Halted(
            f"state/HALT is set ({guard.halt_info()}) — entry order for "
            f"{fields.get('symbol')} refused before reaching the exchange. "
            f"Closes, SL/TP and cancels still work. Only the user may clear "
            f"the halt (guard.clear_halt)."
        )

    guard.audit("order_attempt", **fields)
    try:
        data = _send(method, path, env, params, signed, retries, spot)
    except Exception as e:
        guard.audit("order_error", error=str(e), **fields)
        raise
    order_id = (data or {}).get("orderId", "") if isinstance(data, dict) else ""
    guard.audit("order_ok", order_id=str(order_id), **fields)
    return data


def _send(method, path, env, params=None, signed=True, retries=3, spot=False):
    api_key = env.get("BINANCE_API_KEY")
    secret_key = env.get("BINANCE_SECRET_KEY")
    if signed and (not api_key or not secret_key):
        raise ValueError("BINANCE_API_KEY / BINANCE_SECRET_KEY missing from .env")

    headers = {"X-MBX-APIKEY": api_key} if signed else {}
    last_err = None
    for attempt in range(retries):
        if signed:
            p = dict(params or {})
            p["timestamp"] = int(time.time() * 1000) + _time_offset["ms"]
            p.setdefault("recvWindow", RECV_WINDOW)
            qs = urlencode(p)
            sig = hmac.new(secret_key.encode(), qs.encode(), hashlib.sha256).hexdigest()
            payload = f"{qs}&signature={sig}"  # signature must be last
        else:
            payload = urlencode(params or {})

        try:
            if method == "POST":
                r = requests.post(
                    f"{_base(env, spot)}{path}", data=payload,
                    headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10,
                )
            else:
                url = f"{_base(env, spot)}{path}" + (f"?{payload}" if payload else "")
                r = requests.request(method, url, headers=headers, timeout=10)
        except requests.exceptions.ConnectionError as e:
            last_err = e
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)
            continue

        try:
            data = r.json()
        except ValueError:
            raise BinanceError(r.status_code, f"non-JSON response ({r.text[:120]})", path)
        # Errors are {"code": <negative int>, "msg": "..."} with a 4xx status.
        if isinstance(data, dict) and isinstance(data.get("code"), int) \
                and data["code"] < 0 and "msg" in data:
            code = data["code"]
            if code == -1021:
                _sync_time(env)
                last_err = BinanceError(code, data.get("msg"), path)
                continue  # resigned with corrected clock next attempt
            if code in RETRYABLE_CODES and attempt < retries - 1:
                last_err = BinanceError(code, data.get("msg"), path)
                time.sleep(1 + attempt)
                continue
            raise BinanceError(code, data.get("msg"), path)
        return data
    raise last_err


# ── broker client-order-id ───────────────────────────────────────────────────

def _cid(client_order_id=None):
    """Build the attributed newClientOrderId: x-52DDFAFN + caller suffix (or a
    time-based one). Raises instead of truncating an over-long caller id —
    silent truncation could collide two intents and break idempotency."""
    suffix = client_order_id or f"{int(time.time() * 1000):x}"
    cid = f"{BROKER_PREFIX}{suffix}"
    if len(cid) > CID_MAX:
        raise ValueError(
            f"client_order_id too long: {len(suffix)} chars, max "
            f"{CID_MAX - len(BROKER_PREFIX)} after the broker prefix"
        )
    return cid


# ── contract rules / quantity formatting ────────────────────────────────────

def get_contract_rules(env, symbol):
    """Rules for one symbol: {'step': str, 'min_qty': float, 'min_notional':
    float, 'contract_value': 1.0, 'price_tick': str, 'active': bool}.
    Binance futures quantity IS base units — contract_value is always 1.0
    (kept for the cross-venue contract). Cached per process (exchangeInfo is
    one big call for every symbol)."""
    symbol = _canonical(symbol)
    if not _rules_cache:
        info = _send("GET", "/fapi/v1/exchangeInfo", env, signed=False)
        for s in info.get("symbols", []):
            filters = {f.get("filterType"): f for f in s.get("filters", [])}
            lot = filters.get("LOT_SIZE", {})
            mkt = filters.get("MARKET_LOT_SIZE", {})
            _rules_cache[s["symbol"]] = {
                "step": mkt.get("stepSize") or lot.get("stepSize") or "1",
                # market orders must satisfy BOTH filters — take the stricter
                "min_qty": max(float(lot.get("minQty", 0)), float(mkt.get("minQty", 0))),
                "max_qty": float(mkt.get("maxQty") or lot.get("maxQty") or 0) or None,
                "min_notional": float(filters.get("MIN_NOTIONAL", {}).get("notional", 0)),
                "contract_value": 1.0,
                "price_tick": filters.get("PRICE_FILTER", {}).get("tickSize", "0.01"),
                "active": s.get("status") == "TRADING",
            }
    if symbol not in _rules_cache:
        raise BinanceError("N/A", f"symbol {symbol} not found in /fapi/v1/exchangeInfo",
                           "exchangeInfo")
    return _rules_cache[symbol]


def _floor_to_step(value, step_str):
    return Decimal(str(value)).quantize(Decimal(step_str).normalize(), rounding=ROUND_DOWN)


def format_qty(env, symbol, qty, price=None):
    """Floor a BASE-currency qty to the symbol's step and validate minimums.
    Returns a plain decimal string (never scientific notation). Raises
    ValueError below min_qty, or below min_notional when price is given.
    Callers use this as the min-size gate ONLY — never feed the result into
    place_market_order (units pitfall, references/manager.md)."""
    rules = get_contract_rules(env, symbol)
    if not rules["active"]:
        raise ValueError(f"{symbol} is not open for trading")
    q = _floor_to_step(qty, rules["step"])
    if float(q) < rules["min_qty"] or float(q) <= 0:
        raise ValueError(
            f"{symbol} qty {qty} floors to {q}, below exchange minimum {rules['min_qty']}"
        )
    if price is not None and float(q) * float(price) < rules["min_notional"]:
        raise ValueError(
            f"{symbol} notional {float(q) * float(price):.4f} below minimum "
            f"{rules['min_notional']} USDT"
        )
    return format(q, "f")


def format_price(env, symbol, price):
    """Floor price to the symbol's tick size. Returns a plain decimal string."""
    rules = get_contract_rules(env, symbol)
    return format(_floor_to_step(price, rules["price_tick"]), "f")


# ── position mode ────────────────────────────────────────────────────────────

def get_position_mode(env):
    """'hedge' or 'oneway'. Binance returns dualSidePosition as a real JSON
    boolean (not BingX's string). Cached per api key per process."""
    key = env.get("BINANCE_API_KEY", "")
    if key not in _position_mode_cache:
        data = _request("GET", "/fapi/v1/positionSide/dual", env) or {}
        _position_mode_cache[key] = "hedge" if data.get("dualSidePosition") else "oneway"
    return _position_mode_cache[key]


def _position_side(env, direction):
    """direction: 'long'|'short' (of the POSITION). One-way mode sends BOTH;
    hedge mode sends LONG/SHORT regardless of open/close."""
    if get_position_mode(env) == "oneway":
        return "BOTH"
    return direction.upper()


# ── orders ───────────────────────────────────────────────────────────────────

def _norm_ids(o):
    """orderId/algoId are huge integers — keep them as str, never float math."""
    if isinstance(o, dict):
        for k in ("orderId", "algoId"):
            if k in o:
                o[k] = str(o[k])
    return o


def place_market_order(env, symbol, direction, qty, client_order_id=None,
                       reduce_only=False, confirm_timeout=15):
    """Market order, CONFIRMED. qty is BASE currency (= Binance's own unit).
    direction: 'long'|'short' = the position this order builds (when
    reduce_only, the position it reduces). Returns confirm_order()'s dict —
    exchange-reported avg_price / executed_qty / commission, never intent.
    Below min qty/notional → returns False (intentional skip, the reconciler
    convention — no phantom-trade notification).

    client_order_id: unique-per-intent suffix ("mystrat20260709120500"),
    ≤26 chars — the broker prefix x-52DDFAFN is prepended here. Duplicate ids
    are rejected by Binance only while the original order is OPEN; a terminal
    order's id is reusable, so treat this as a retry-window guard, not a
    permanent ledger (see OrderNotConfirmed)."""
    symbol = _canonical(symbol)
    mode = get_position_mode(env)
    try:
        qty_str = format_qty(env, symbol, qty)
    except ValueError as e:
        # a suspended symbol is a FAULT, not an intentional min-size skip —
        # swallowing it would leave a close silently unexecuted (audit S4)
        if "not open for trading" in str(e):
            raise
        return False
    if reduce_only:
        side = "SELL" if direction == "long" else "BUY"
    else:
        side = "BUY" if direction == "long" else "SELL"
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": _position_side(env, direction),
        "type": "MARKET",
        "quantity": qty_str,
        "newClientOrderId": _cid(client_order_id),
    }
    # hedge mode REJECTS reduceOnly (-1106) — there, closing is already fully
    # expressed by side being opposite to positionSide
    if reduce_only and mode == "oneway":
        params["reduceOnly"] = "true"

    try:
        placed = _norm_ids(_request("POST", "/fapi/v1/order", env, params))
    except BinanceError as e:
        # -4164 = below MIN_NOTIONAL. format_qty can't pre-check it here (no
        # price arg on market orders), and Binance EXEMPTS reduce-only orders
        # from the check — so let the exchange decide and map its rejection to
        # the contract's intentional-skip False (audit S3).
        if e.code == -4164:
            return False
        raise
    if not placed.get("orderId"):
        raise BinanceError("N/A", f"order response missing orderId: {placed}", "order")
    return confirm_order(env, symbol, placed["orderId"], timeout=confirm_timeout)


def place_limit_order(env, symbol, direction, qty, price, client_order_id=None,
                      reduce_only=False, time_in_force="GTC", post_only=False):
    """Limit order, UNCONFIRMED (it may rest) — use confirm_order / get_order
    to track it. Returns the raw order dict plus a normalized 'order_id' key
    (the chase-limit contract: every later cancel/status call is by order_id).
    Below min qty/notional → returns False (intentional skip, same convention
    as place_market_order — the chase toolkit sizes legs in USD and small
    residuals are expected). price is floored to the symbol's tick here —
    callers pass a raw float.

    post_only=True sends timeInForce=GTX (Binance futures' post-only dialect,
    overriding time_in_force); a GTX order that would cross is refused with
    -5022 GTX_ORDER_REJECT and RETURNS {'status': 'post_only_rejected'}
    instead of raising — the chase executor re-reads the book and re-posts."""
    symbol = _canonical(symbol)
    mode = get_position_mode(env)
    if reduce_only:
        side = "SELL" if direction == "long" else "BUY"
    else:
        side = "BUY" if direction == "long" else "SELL"
    price_str = format_price(env, symbol, price)
    try:
        # Binance EXEMPTS reduce-only orders from MIN_NOTIONAL (-4164, same
        # exemption place_market_order relies on) — skip the notional pre-check
        # there so small residual closes ($10-100) aren't blocked by our own
        # gate; step/min_qty checks still apply either way.
        qty_str = format_qty(env, symbol, qty,
                             price=None if reduce_only else price_str)
    except ValueError as e:
        # a suspended symbol is a FAULT, not an intentional min-size skip
        if "not open for trading" in str(e):
            raise
        return False
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": _position_side(env, direction),
        "type": "LIMIT",
        "quantity": qty_str,
        "price": price_str,
        "timeInForce": "GTX" if post_only else time_in_force,
        "newClientOrderId": _cid(client_order_id),
    }
    if reduce_only and mode == "oneway":
        params["reduceOnly"] = "true"
    try:
        placed = _norm_ids(_request("POST", "/fapi/v1/order", env, params))
    except BinanceError as e:
        if post_only and e.code == -5022:
            return {"status": "post_only_rejected"}
        raise
    if not placed.get("orderId"):
        raise BinanceError("N/A", f"order response missing orderId: {placed}", "order")
    placed["order_id"] = placed["orderId"]
    return placed


def get_order(env, symbol, order_id):
    """Order details by orderId. Normalized: status, avg_price, executed_qty,
    orig_qty, client_order_id, plus the raw dict under 'raw'. Commission is
    NOT in Binance's order query — confirm_order adds it from userTrades."""
    o = _norm_ids(_request("GET", "/fapi/v1/order", env,
                           {"symbol": _canonical(symbol), "orderId": str(order_id)}))
    if "status" not in o:
        raise BinanceError("N/A", f"order query missing status: {o}", "order")
    return {
        "order_id": o["orderId"],
        "status": o["status"],
        "avg_price": float(o.get("avgPrice") or 0),
        "executed_qty": float(o.get("executedQty") or 0),
        "orig_qty": float(o.get("origQty") or 0),
        "client_order_id": o.get("clientOrderId") or "",
        "raw": o,
    }


TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}


def confirm_order(env, symbol, order_id, timeout=15):
    """Poll until the order reaches a terminal state. Returns get_order()'s
    dict plus 'commission' (summed from userTrades, best-effort — a stats
    lookup failing must not turn a FILLED order into an exception). Raises
    BinanceError if the exchange reports CANCELED/REJECTED/EXPIRED,
    OrderNotConfirmed if still pending after timeout — the order MAY STILL
    FILL; re-query, do not blindly resubmit."""
    symbol = _canonical(symbol)
    deadline = time.time() + timeout
    order = None
    while time.time() < deadline:
        order = get_order(env, symbol, order_id)
        if order["status"] == "FILLED":
            order["commission"], order["commission_asset"] = \
                _order_commission(env, symbol, order_id)
            return order
        if order["status"] in TERMINAL_STATUSES:
            raise BinanceError("N/A", f"order {order_id} ended {order['status']}", "confirm")
        time.sleep(1)
    raise OrderNotConfirmed(
        f"order {order_id} still {order['status'] if order else 'UNKNOWN'} after {timeout}s"
    )


def _order_commission(env, symbol, order_id):
    """(amount, asset) summed across the order's fills. NOT always USDT:
    fee-discount accounts pay in BNB (commissionAsset) — measured live, so the
    asset is returned rather than a number that would be silently mis-summed.
    userTrades lags the fill by a moment — retried once (measured: immediate
    query returned no fills, 1s later they were there). Best-effort: (0.0,
    'USDT') if the lookup fails — a stats hiccup must not fail a FILLED order.
    Mixed-asset fills (rare): sums the first asset seen and ignores others."""
    try:
        for attempt in range(2):
            fills = _request("GET", "/fapi/v1/userTrades", env,
                             {"symbol": symbol, "orderId": str(order_id)}) or []
            if fills:
                asset = fills[0].get("commissionAsset") or "USDT"
                return (sum(abs(float(f.get("commission") or 0)) for f in fills
                            if (f.get("commissionAsset") or "USDT") == asset), asset)
            if attempt == 0:
                time.sleep(1)
    except Exception:
        pass
    return (0.0, "USDT")


def _norm_open_row(o):
    """Open-order row contract (venue_wiring.sweep_orphan_orders): every row
    carries canonical 'symbol' + 'order_id' + 'client_order_id' alongside the
    venue's raw fields — the reconciler sweeps orphaned rc*-prefixed orders by
    these keys after a restart."""
    o = _norm_ids(o)
    o["symbol"] = _canonical(o.get("symbol"))
    o["order_id"] = o.get("orderId", "")
    o["client_order_id"] = o.get("clientOrderId") or ""
    return o


def get_open_orders(env, symbol=None):
    """All open REGULAR orders (market/limit), orderId normalized to str and
    each row normalized per _norm_open_row (symbol/order_id/client_order_id).
    Conditional SL/TP orders are NOT here — see get_open_algo_orders()."""
    params = {"symbol": _canonical(symbol)} if symbol else {}
    return [_norm_open_row(o) for o in
            (_request("GET", "/fapi/v1/openOrders", env, params) or [])]


def get_open_algo_orders(env, symbol=None):
    """All open CONDITIONAL (algo) orders — the Algo API keeps them separate
    from /fapi/v1/openOrders. algoId normalized to str. Response may arrive
    as a bare list or nested under 'orders' depending on doc version — both
    are handled."""
    params = {"symbol": _canonical(symbol)} if symbol else {}
    data = _request("GET", "/fapi/v1/openAlgoOrders", env, params) or []
    rows = data.get("orders", []) if isinstance(data, dict) else data
    return [_norm_ids(o) for o in rows]


def cancel_order(env, symbol, order_id=None, client_order_id=None):
    """Cancel one REGULAR order by orderId or the FULL newClientOrderId
    (prefix included — pass what get_order returned). Conditional orders go
    through cancel_algo_order.

    Idempotent-safe (chase-limit contract): an order that is already filled
    or gone rejects the cancel with -2011 CANCEL_REJECTED ("Unknown order
    sent") — that RETURNS {'status': 'already_gone', ...} instead of raising;
    the caller always re-reads fills afterwards. Other refusals raise."""
    params = {"symbol": _canonical(symbol)}
    if order_id:
        params["orderId"] = str(order_id)
    elif client_order_id:
        params["origClientOrderId"] = client_order_id
    else:
        raise ValueError("order_id or client_order_id required")
    try:
        return _norm_ids(_request("DELETE", "/fapi/v1/order", env, params))
    except BinanceError as e:
        if e.code == -2011:
            return {"status": "already_gone", "code": e.code, "msg": e.msg}
        raise


def cancel_algo_order(env, algo_id=None, client_algo_id=None):
    """Cancel one CONDITIONAL order by algoId or the FULL clientAlgoId."""
    params = {}
    if algo_id:
        params["algoId"] = str(algo_id)
    elif client_algo_id:
        params["clientAlgoId"] = client_algo_id
    else:
        raise ValueError("algo_id or client_algo_id required")
    return _norm_ids(_request("DELETE", "/fapi/v1/algoOrder", env, params))


def cancel_all_orders(env, symbol):
    """Cancel ALL open orders for a symbol — regular AND conditional (the two
    live on separate endpoints; cancelling only one side leaves protective
    orders behind). Only do this when also closing the position."""
    symbol = _canonical(symbol)
    out = {"orders": _request("DELETE", "/fapi/v1/allOpenOrders", env,
                              {"symbol": symbol})}
    out["algo_orders"] = _request("DELETE", "/fapi/v1/algoOpenOrders", env,
                                  {"symbol": symbol})
    return out


# ── protective orders ────────────────────────────────────────────────────────

def place_protective_orders(env, symbol, direction, qty=None, sl_price=None,
                            tp_price=None, verify=True):
    """Reduce-only STOP_MARKET / TAKE_PROFIT_MARKET for an EXISTING position,
    via the Algo Order API (/fapi/v1/order rejects conditional types with
    -4120 since the migration — measured live 2026-08-04).
    direction is the POSITION's direction; the orders take the opposite side.
    qty=None protects the WHOLE position via closePosition=true (survives
    size changes — prefer it unless doing partial TPs). Verifies the orders
    exist in open ALGO orders; raises ProtectionFailed otherwise — the caller
    MUST alert the user (naked position). Returned dicts carry algoId (str) —
    cancel via cancel_algo_order, not cancel_order."""
    symbol = _canonical(symbol)
    mode = get_position_mode(env)
    close_side = "SELL" if direction == "long" else "BUY"
    placed = {}
    try:
        for kind, otype, trigger in (
            ("sl", "STOP_MARKET", sl_price),
            ("tp", "TAKE_PROFIT_MARKET", tp_price),
        ):
            if trigger is None:
                continue
            params = {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": close_side,
                "positionSide": _position_side(env, direction),
                "type": otype,
                # Algo API vocabulary: triggerPrice (the classic endpoint's
                # stopPrice name is not accepted here)
                "triggerPrice": format_price(env, symbol, trigger),
                "workingType": "MARK_PRICE",
                "clientAlgoId": _cid(),  # broker attribution rides this field
            }
            if qty is None:
                # closePosition carries its own reduce-only semantics and must
                # NOT be combined with quantity or reduceOnly
                params["closePosition"] = "true"
            else:
                params["quantity"] = format_qty(env, symbol, qty)
                if mode == "oneway":
                    params["reduceOnly"] = "true"
            order = _norm_ids(_request("POST", "/fapi/v1/algoOrder", env, params))
            if not order.get("algoId"):
                raise BinanceError("N/A", f"{otype} response missing algoId", "algoOrder")
            placed[kind] = order
    except Exception as e:
        raise ProtectionFailed(
            f"{symbol} {direction} position may be NAKED — placing protective orders "
            f"failed after {list(placed)} succeeded: {e}"
        ) from e

    if verify and placed:
        open_ids = {o.get("algoId") for o in get_open_algo_orders(env, symbol)}
        missing = [k for k, o in placed.items() if o["algoId"] not in open_ids]
        if missing:
            raise ProtectionFailed(
                f"{symbol} {direction}: protective order(s) {missing} were accepted but "
                f"are NOT in open algo orders — verify manually, position may be naked"
            )
    return placed


# ── high-level entry / close ────────────────────────────────────────────────

def open_position(env, symbol, direction, qty, sl_price=None, tp_price=None,
                  client_order_id=None, confirm_timeout=15):
    """Open a position: confirmed market entry, then protective orders.
    Binance futures cannot attach SL/TP to the entry order itself (no atomic
    request like BingX's) — there IS a short naked window between the fill and
    the protective orders; this function makes it as small as one round-trip
    and raises ProtectionFailed if protection isn't verified on the exchange.

    Returns {'entry': confirmed order dict, 'protection': {'sl'/'tp': order}}.
    Returns False if qty is below the exchange minimum (skip semantics)."""
    symbol = _canonical(symbol)
    entry = place_market_order(
        env, symbol, direction, qty,
        client_order_id=client_order_id, confirm_timeout=confirm_timeout,
    )
    if entry is False:
        return False
    protection = {}
    if sl_price is not None or tp_price is not None:
        protection = place_protective_orders(
            env, symbol, direction, qty=None, sl_price=sl_price, tp_price=tp_price,
        )
    return {"entry": entry, "protection": protection}


def close_position(env, symbol, direction, qty, client_order_id=None):
    """Market-close (part of) an existing position, confirmed. direction is
    the POSITION being closed ('long'|'short')."""
    return place_market_order(
        env, symbol, direction, qty,
        client_order_id=client_order_id, reduce_only=True,
    )


def close_position_partial(env, symbol, direction, qty, client_order_id=None):
    """Cross-venue reconciler contract name — close_position already takes a
    qty (partial-capable); this alias is the FIXED name the reconciler and
    manager/flatten.py call."""
    return close_position(env, symbol, direction, qty, client_order_id=client_order_id)


# ── leverage ─────────────────────────────────────────────────────────────────

def set_leverage(env, symbol, leverage):
    """Set leverage (applies to both sides on Binance). Never silently
    inherit: query first, set only on user instruction (references/manager.md
    confirmation rules)."""
    return _request("POST", "/fapi/v1/leverage", env, {
        "symbol": _canonical(symbol), "leverage": str(int(leverage)),
    })


def get_mark_price(env, symbol):
    """Live mark price (public premiumIndex) — the cross-venue wiring contract
    for USD→qty conversion (lib/venue_wiring.py)."""
    data = _send("GET", "/fapi/v1/premiumIndex", env,
                 {"symbol": _canonical(symbol)}, signed=False)
    return float(data["markPrice"])


def get_bbo(env, symbol):
    """Top-of-book {'bid': float, 'ask': float} from the public bookTicker —
    real best bid/ask, never mark/last price (chase-limit contract)."""
    data = _send("GET", "/fapi/v1/ticker/bookTicker", env,
                 {"symbol": _canonical(symbol)}, signed=False)
    return {"bid": float(data["bidPrice"]), "ask": float(data["askPrice"])}


def get_leverage(env, symbol):
    """Current leverage for the symbol, from the position-risk row (present
    even when flat)."""
    rows = _request("GET", "/fapi/v2/positionRisk", env,
                    {"symbol": _canonical(symbol)}) or []
    lev = int(float(rows[0].get("leverage", 0))) if rows else 0
    return {"long": lev, "short": lev}


# ── spot execution (MARKET="spot" strategies) ────────────────────────────────
# Same guard/audit/attribution discipline as the futures layer, different
# vocabulary: spot broker prefix is x-GBN6HWR2, quantities obey the SPOT
# exchangeInfo filters (separate from futures rules), market BUYs can be
# placed in QUOTE currency (quoteOrderQty — no price conversion, no step
# math), and the FULL response returns fills inline: no confirm polling.
# There are no positions and no leverage — inventory is the position.

SPOT_BROKER_PREFIX = "x-GBN6HWR2"  # Blave spot broker ID — every spot order

_spot_rules_cache = {}  # symbol -> spot trading rules (per-process)


def _spot_cid(client_order_id=None):
    suffix = client_order_id or f"{int(time.time() * 1000):x}"
    cid = f"{SPOT_BROKER_PREFIX}{suffix}"
    if len(cid) > CID_MAX:
        raise ValueError(
            f"client_order_id too long: {len(suffix)} chars, max "
            f"{CID_MAX - len(SPOT_BROKER_PREFIX)} after the spot broker prefix"
        )
    return cid


def get_spot_rules(env, symbol):
    """Spot trading rules for one symbol: {'step': str, 'min_qty': float,
    'min_notional': float, 'price_tick': str, 'active': bool}. Spot's
    notional filter is named NOTIONAL on current symbols but MIN_NOTIONAL on
    older doc examples — accept both. Cached per process; queried per symbol
    (spot exchangeInfo for ALL symbols is a multi-MB payload, unlike fapi's)."""
    symbol = _canonical(symbol)
    if symbol not in _spot_rules_cache:
        info = _send("GET", "/api/v3/exchangeInfo", env,
                     {"symbol": symbol}, signed=False, spot=True)
        symbols = info.get("symbols") or []
        if not symbols:
            raise BinanceError("N/A", f"symbol {symbol} not in spot exchangeInfo",
                               "exchangeInfo")
        s = symbols[0]
        filters = {f.get("filterType"): f for f in s.get("filters", [])}
        lot = filters.get("LOT_SIZE", {})
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        _spot_rules_cache[symbol] = {
            "step": lot.get("stepSize") or "1",
            "min_qty": float(lot.get("minQty", 0)),
            "min_notional": float(notional.get("minNotional", 0)),
            "price_tick": filters.get("PRICE_FILTER", {}).get("tickSize", "0.01"),
            "active": s.get("status") == "TRADING",
        }
    return _spot_rules_cache[symbol]


def format_spot_qty(env, symbol, qty):
    """Floor a BASE qty to the spot step. Raises ValueError below min_qty or
    on a suspended symbol. Plain decimal string."""
    rules = get_spot_rules(env, symbol)
    if not rules["active"]:
        raise ValueError(f"{symbol} is not open for trading")
    q = _floor_to_step(qty, rules["step"])
    if float(q) < rules["min_qty"] or float(q) <= 0:
        raise ValueError(
            f"{symbol} spot qty {qty} floors to {q}, below minimum {rules['min_qty']}"
        )
    return format(q, "f")


def format_spot_price(env, symbol, price):
    """Floor price to the spot symbol's tick size. Plain decimal string."""
    rules = get_spot_rules(env, symbol)
    return format(_floor_to_step(price, rules["price_tick"]), "f")


def place_spot_market_order(env, symbol, side, base_qty=None, quote_qty=None,
                            client_order_id=None):
    """Confirmed spot market order. side: 'buy'|'sell'.

    BUYs pass quote_qty (USDT amount — Binance's quoteOrderQty sizes the order
    in quote currency, no price conversion or step math on our side); SELLs
    pass base_qty (coin amount, floored to the spot step — you can only sell
    inventory you hold). Below min qty/notional → returns False (intentional
    skip, reconciler convention). Guard: buy=entry (halt blocks), sell=reduce
    (never trapped).

    newOrderRespType=FULL makes the placement response itself carry the fills
    (spot fills synchronously) — status/avgPrice/executedQty/commission come
    from that response, no polling. Returns {'order_id', 'status',
    'avg_price', 'executed_qty', 'quote_qty', 'commission',
    'commission_asset', 'client_order_id', 'raw'}."""
    symbol = _canonical(symbol)
    side = side.lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy|sell, got {side!r}")
    rules = get_spot_rules(env, symbol)
    params = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET",
        "newOrderRespType": "FULL",
        "newClientOrderId": _spot_cid(client_order_id),
    }
    if side == "buy":
        if quote_qty is None:
            raise ValueError("spot buys are sized in quote currency — pass quote_qty")
        if float(quote_qty) < rules["min_notional"]:
            return False  # below min notional — intentional skip
        params["quoteOrderQty"] = f"{float(quote_qty):.2f}"
    else:
        if base_qty is None:
            raise ValueError("spot sells are sized in base currency — pass base_qty")
        try:
            params["quantity"] = format_spot_qty(env, symbol, base_qty)
        except ValueError as e:
            if "not open for trading" in str(e):
                raise
            return False  # below min qty — intentional skip

    try:
        o = _norm_ids(_request("POST", "/api/v3/order", env, params, spot=True))
    except BinanceError as e:
        # -1013 = filter failure (NOTIONAL/LOT_SIZE) the pre-checks above
        # could not see (price moved) — the contract's intentional skip
        if e.code == -1013:
            return False
        raise
    status = o.get("status")
    executed = float(o.get("executedQty") or 0)
    quote = float(o.get("cummulativeQuoteQty") or 0)
    if status != "FILLED":
        raise BinanceError("N/A", f"spot order {o.get('orderId')} ended {status} "
                           f"(executed {executed})", "order")
    fills = o.get("fills") or []
    commission = 0.0
    commission_asset = None
    if fills:
        commission_asset = fills[0].get("commissionAsset")
        commission = sum(abs(float(f.get("commission") or 0)) for f in fills
                         if f.get("commissionAsset") == commission_asset)
    return {
        "order_id": o.get("orderId", ""),
        "status": status,
        "avg_price": (quote / executed) if executed else 0.0,
        "executed_qty": executed,
        "quote_qty": quote,
        "commission": commission,
        "commission_asset": commission_asset or "",
        "client_order_id": o.get("clientOrderId") or "",
        "raw": o,
    }


def get_spot_balances(env):
    """{asset: free+locked} for every non-zero spot balance — the spot
    inventory read the reconciler wiring keys `actual` on."""
    data = _send("GET", "/api/v3/account", env,
                 {"omitZeroBalances": "true"}, spot=True)
    out = {}
    for b in data.get("balances", []):
        amt = float(b.get("free") or 0) + float(b.get("locked") or 0)
        if amt > 0:
            out[b.get("asset")] = amt
    return out


def get_spot_price(env, symbol):
    """Last spot price (public)."""
    data = _send("GET", "/api/v3/ticker/price", env,
                 {"symbol": _canonical(symbol)}, signed=False, spot=True)
    return float(data["price"])


# ── spot limit layer (chase-limit contract, references/exchange-connect.md) ──

def place_spot_limit_order(env, symbol, side, base_qty, price,
                           client_order_id=None, post_only=False):
    """Spot limit order, UNCONFIRMED (it may rest) — track via get_spot_order.
    side: 'buy'|'sell'. base_qty is BASE currency on BOTH sides (unlike market
    buys, a limit has a price to convert with — no quoteOrderQty lane); price
    is floored to the spot tick here. Below min qty/notional → returns False
    (intentional skip). Returns a normalized dict with 'order_id' (+ status /
    orig_qty / executed_qty / avg_price / raw).

    post_only=True sends type LIMIT_MAKER — spot's post-only is an order TYPE,
    not a timeInForce, and LIMIT_MAKER takes quantity+price only. A maker
    order that would cross is refused with -2010 "Order would immediately
    match and take." and RETURNS {'status': 'post_only_rejected'} instead of
    raising; other -2010 messages (insufficient balance, …) stay fatal.
    Guard: buy=entry (halt blocks), sell=reduce (never trapped)."""
    symbol = _canonical(symbol)
    side = side.lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy|sell, got {side!r}")
    rules = get_spot_rules(env, symbol)
    try:
        qty_str = format_spot_qty(env, symbol, base_qty)
    except ValueError as e:
        # a suspended symbol is a FAULT, not an intentional min-size skip
        if "not open for trading" in str(e):
            raise
        return False
    price_str = format_spot_price(env, symbol, price)
    if float(qty_str) * float(price_str) < rules["min_notional"]:
        return False  # below min notional — intentional skip
    params = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "LIMIT_MAKER" if post_only else "LIMIT",
        "quantity": qty_str,
        "price": price_str,
        "newClientOrderId": _spot_cid(client_order_id),
    }
    if not post_only:
        params["timeInForce"] = "GTC"  # LIMIT_MAKER rejects timeInForce
    try:
        o = _norm_ids(_request("POST", "/api/v3/order", env, params, spot=True))
    except BinanceError as e:
        if post_only and e.code == -2010 and "immediately match" in (e.msg or ""):
            return {"status": "post_only_rejected"}
        raise
    if not o.get("orderId"):
        raise BinanceError("N/A", f"spot order response missing orderId: {o}", "order")
    executed = float(o.get("executedQty") or 0)
    quote = float(o.get("cummulativeQuoteQty") or 0)
    return {
        "order_id": o["orderId"],
        "status": o.get("status") or "",
        "avg_price": (quote / executed) if executed else 0.0,
        "executed_qty": executed,
        "orig_qty": float(o.get("origQty") or 0),
        "client_order_id": o.get("clientOrderId") or "",
        "raw": o,
    }


def get_spot_order(env, symbol, order_id):
    """Spot order details by orderId. Normalized: status, avg_price,
    executed_qty, orig_qty, client_order_id, plus the raw dict under 'raw'.
    avg_price is derived from cummulativeQuoteQty/executedQty — the spot
    order query has no avgPrice field."""
    o = _norm_ids(_request("GET", "/api/v3/order", env,
                           {"symbol": _canonical(symbol), "orderId": str(order_id)},
                           spot=True))
    if "status" not in o:
        raise BinanceError("N/A", f"spot order query missing status: {o}", "order")
    executed = float(o.get("executedQty") or 0)
    quote = float(o.get("cummulativeQuoteQty") or 0)
    return {
        "order_id": o["orderId"],
        "status": o["status"],
        "avg_price": (quote / executed) if executed else 0.0,
        "executed_qty": executed,
        "orig_qty": float(o.get("origQty") or 0),
        "client_order_id": o.get("clientOrderId") or "",
        "raw": o,
    }


def cancel_spot_order(env, symbol, order_id=None, client_order_id=None):
    """Cancel one spot order by orderId or the FULL newClientOrderId.
    Idempotent-safe like cancel_order: already filled / gone rejects with
    -2011 CANCEL_REJECTED and RETURNS {'status': 'already_gone', ...} instead
    of raising — the caller re-reads fills afterwards."""
    params = {"symbol": _canonical(symbol)}
    if order_id:
        params["orderId"] = str(order_id)
    elif client_order_id:
        params["origClientOrderId"] = client_order_id
    else:
        raise ValueError("order_id or client_order_id required")
    try:
        return _norm_ids(_request("DELETE", "/api/v3/order", env, params, spot=True))
    except BinanceError as e:
        if e.code == -2011:
            return {"status": "already_gone", "code": e.code, "msg": e.msg}
        raise


def get_spot_bbo(env, symbol):
    """Spot top-of-book {'bid': float, 'ask': float} from the public
    bookTicker — real best bid/ask, never last price (chase-limit contract)."""
    data = _send("GET", "/api/v3/ticker/bookTicker", env,
                 {"symbol": _canonical(symbol)}, signed=False, spot=True)
    return {"bid": float(data["bidPrice"]), "ask": float(data["askPrice"])}


def get_spot_open_orders(env, symbol=None):
    """All open spot orders, each row normalized per _norm_open_row
    (canonical symbol / order_id / client_order_id — the reconciler's orphan
    sweep keys on these). Symbol omitted = all symbols (heavier call weight;
    used once at reconciler startup)."""
    params = {"symbol": _canonical(symbol)} if symbol else {}
    return [_norm_open_row(o) for o in
            (_request("GET", "/api/v3/openOrders", env, params, spot=True) or [])]
