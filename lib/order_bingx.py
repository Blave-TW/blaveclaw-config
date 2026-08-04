"""
BingX swap (USDT-M perpetual) order execution library.

Companion to lib/account_bingx.py (equity/positions). Credentials:
BINGX_API_KEY, BINGX_SECRET_KEY in .env. Set BINGX_DEMO=true to trade on the
VST paper environment (open-api-vst.bingx.com — swap only) with the same keys.
X-SOURCE-KEY: BX-AI-SKILL broker attribution is mandatory on every request.

Design rules (why this file exists — hand-rolled per-machine versions of this
layer caused naked positions, duplicate orders, and TP-reported-as-SL bugs):

1. FAIL LOUD. Every unexpected response shape raises BingXError with the code
   and message. Never .get() a critical field into a silent default.
2. CONFIRMED, NOT SENT. The place-order response contains no fill info.
   `place_market_order` / `open_position` poll the order until it reaches a
   terminal state and return what the EXCHANGE says (avgPrice, executedQty,
   commission) — report those numbers, never the intent.
3. NO NAKED POSITIONS. `open_position` attaches stopLoss/takeProfit to the
   entry order itself (single atomic request — the naked window between entry
   and protective orders does not exist). Standalone protective orders are
   verified to exist on the exchange before returning.
4. IDEMPOTENT. Pass `client_order_id` (unique per intent, alphanumeric only,
   e.g. "mystrat20260709120500") — resubmitting the same id is rejected by the
   exchange (error 101481) instead of opening a second position.
5. HALTABLE + AUDITED. Every order-mutating request passes through lib/guard:
   if state/HALT exists, entry orders raise guard.Halted BEFORE any network
   call (reduce-only closes, SL/TP, and cancels still work — flattening must
   never be trapped), and every attempt/outcome/denial is appended to
   state/audit.jsonl. See lib/guard.py for semantics.

Verified against ccxt's production parser + BingX official docs + two
fleet-debugged implementations. Known quirks handled here so callers never
meet them: response nested under data.order; clientOrderID (request) vs
clientOrderId (query response); fills under data.fill_orders with startTs/
endTs and ISO-string filledTime; position mode as string "true"/"false";
orderId as huge integer (kept as str); commission returned negative.
"""

import hashlib
import hmac
import json
import os
import time
from decimal import Decimal, ROUND_DOWN

import requests

from lib import guard

LIVE_URL = "https://open-api.bingx.com"
LIVE_FALLBACK = "https://open-api.bingx.pro"
DEMO_URL = "https://open-api-vst.bingx.com"  # VST paper trading, swap only

RETRYABLE_CODES = {100410, 100500}  # 100410 = rate limited (no Retry-After; backoff), 100500 = internal
RECV_WINDOW = "5000"

_rules_cache = {}  # symbol -> contract rules (per-process)
_position_mode_cache = {}  # api_key -> "hedge" | "oneway"


class BingXError(Exception):
    """Raised on any BingX error response or unexpected shape."""

    def __init__(self, code, msg, path=""):
        self.code = code
        self.msg = msg
        super().__init__(f"BingX error {code}: {msg or '(empty msg)'} | {path}")


class OrderNotConfirmed(Exception):
    """Order was accepted but did not reach a terminal state within timeout.
    The order may still fill — query it again before assuming anything."""


class ProtectionFailed(Exception):
    """Position is OPEN but a protective (SL/TP) order could not be placed or
    verified. The position is NAKED — alert the user immediately."""


# ── transport ────────────────────────────────────────────────────────────────

def _bases(env):
    if str(env.get("BINGX_DEMO", os.environ.get("BINGX_DEMO", ""))).lower() == "true":
        return [DEMO_URL]
    return [LIVE_URL, LIVE_FALLBACK]


def _sign(secret_key, params):
    params = dict(params)
    params["timestamp"] = str(int(time.time() * 1000))
    params.setdefault("recvWindow", RECV_WINDOW)
    canonical = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig = hmac.new(secret_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return canonical + f"&signature={sig}"


# Order-mutating endpoints and how to classify their intent for the guard.
_MUTATING_PATHS = {
    ("POST", "/openApi/swap/v2/trade/order"): "place",
    ("DELETE", "/openApi/swap/v2/trade/order"): "cancel",
    ("DELETE", "/openApi/swap/v2/trade/allOpenOrders"): "cancel_all",
    ("POST", "/openApi/swap/v2/trade/leverage"): "leverage",
}

_PROTECTIVE_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"}

# Params worth keeping in the audit line (no secrets live in params — the
# signature and timestamp are added later, inside _sign's own copy).
_AUDIT_PARAM_KEYS = ("symbol", "side", "positionSide", "type", "quantity",
                     "price", "stopPrice", "clientOrderID", "reduceOnly",
                     "closePosition", "leverage", "orderId")


def _order_intent(method, path, params):
    """'entry' | 'reduce' | 'protective' | 'cancel' | 'cancel_all' |
    'leverage' | None (not order-mutating). Hedge mode has no reduceOnly
    flag — there, closing = side opposite to positionSide."""
    kind = _MUTATING_PATHS.get((method, path))
    if kind != "place":
        return kind
    p = params or {}
    if p.get("type") in _PROTECTIVE_TYPES:
        return "protective"
    if str(p.get("reduceOnly", "")).lower() == "true" or \
       str(p.get("closePosition", "")).lower() == "true":
        return "reduce"
    ps, side = p.get("positionSide"), p.get("side")
    if (ps == "LONG" and side == "SELL") or (ps == "SHORT" and side == "BUY"):
        return "reduce"
    return "entry"


def _request(method, path, env, params=None, signed=True, retries=3):
    """Gate + audit wrapper around _send. Reads pass straight through;
    order-mutating requests are halt-checked and audited (design rule 5)."""
    intent = _order_intent(method, path, params)
    if intent is None:
        return _send(method, path, env, params, signed, retries)

    fields = {k: params[k] for k in _AUDIT_PARAM_KEYS if k in (params or {})}
    fields["intent"] = intent
    fields["demo"] = _bases(env) == [DEMO_URL]

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
        data = _send(method, path, env, params, signed, retries)
    except Exception as e:
        guard.audit("order_error", error=str(e), **fields)
        raise
    order = (data or {}).get("order", {}) if isinstance(data, dict) else {}
    guard.audit("order_ok", order_id=str(order.get("orderId", "")), **fields)
    return data


def _send(method, path, env, params=None, signed=True, retries=3):
    api_key = env.get("BINGX_API_KEY")
    secret_key = env.get("BINGX_SECRET_KEY")
    if signed and (not api_key or not secret_key):
        raise ValueError("BINGX_API_KEY / BINGX_SECRET_KEY missing from .env")

    headers = {"X-SOURCE-KEY": "BX-AI-SKILL"}
    if signed:
        headers["X-BX-APIKEY"] = api_key

    last_err = None
    for attempt in range(retries):
        for base in _bases(env):
            try:
                if signed:
                    qs = _sign(secret_key, params or {})
                else:
                    qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
                url = f"{base}{path}" + (f"?{qs}" if method != "POST" and qs else "")
                if method == "POST":
                    r = requests.post(
                        url, data=qs,
                        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                        timeout=10,
                    )
                else:
                    r = requests.request(method, url, headers=headers, timeout=10)
                data = r.json()
                code = data.get("code")
                if code == 0:
                    return data.get("data")
                if code in RETRYABLE_CODES and attempt < retries - 1:
                    last_err = BingXError(code, data.get("msg"), path)
                    time.sleep(1 + attempt)
                    break  # retry outer loop from primary base
                raise BingXError(code, data.get("msg"), path)
            except requests.exceptions.ConnectionError as e:
                last_err = e
                continue  # next base
        else:
            # every base raised ConnectionError
            if attempt == retries - 1:
                raise last_err
            time.sleep(1 + attempt)
    raise last_err


# ── contract rules / quantity formatting ────────────────────────────────────

def _bingx_symbol(sym):
    """Canonical dashless symbols (BTCUSDT — what strategies/reconciler use)
    → BingX swap format (BTC-USDT). Already-dashed symbols pass through."""
    import re as _re
    if "-" not in sym:
        return _re.sub(r"^([A-Z0-9]+?)(USDT|USDC)$", r"\1-\2", sym.upper())
    return sym


def get_contract_rules(env, symbol):
    """Rules for one symbol. {'qty_precision', 'price_precision', 'min_qty',
    'min_notional', 'active'} — precisions are DECIMAL PLACES (BingX swap gives
    digit counts, not tick sizes). Cached per process."""
    symbol = _bingx_symbol(symbol)
    if symbol not in _rules_cache:
        rows = _request("GET", "/openApi/swap/v2/quote/contracts", env, signed=False) or []
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            _rules_cache[sym] = {
                "qty_precision": int(r["quantityPrecision"]),
                "price_precision": int(r["pricePrecision"]),
                "min_qty": float(r["tradeMinQuantity"]),
                "min_notional": float(r.get("tradeMinUSDT", 2)),
                "active": str(r.get("apiStateOpen", "")).lower() == "true",
            }
    if symbol not in _rules_cache:
        raise BingXError("N/A", f"symbol {symbol} not found in /quote/contracts", "contracts")
    return _rules_cache[symbol]


def _floor_to_precision(value, decimals):
    step = Decimal(1).scaleb(-decimals)  # 10^-decimals
    return Decimal(str(value)).quantize(step, rounding=ROUND_DOWN)


def format_qty(env, symbol, qty, price=None):
    """Floor qty to the symbol's precision and validate minimums. Returns a
    plain decimal string (never scientific notation). Raises ValueError if the
    floored qty is below min_qty, or below min_notional when price is given."""
    rules = get_contract_rules(env, symbol)
    if not rules["active"]:
        raise ValueError(f"{symbol} is not open for API trading")
    q = _floor_to_precision(qty, rules["qty_precision"])
    if float(q) < rules["min_qty"]:
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
    """Floor price to the symbol's price precision. Returns a plain decimal string."""
    rules = get_contract_rules(env, symbol)
    return format(_floor_to_precision(price, rules["price_precision"]), "f")


# ── position mode ────────────────────────────────────────────────────────────

def get_position_mode(env):
    """'hedge' or 'oneway'. BingX returns dualSidePosition as the STRING
    "true"/"false". Cached per api key per process."""
    key = env.get("BINGX_API_KEY", "")
    if key not in _position_mode_cache:
        data = _request("GET", "/openApi/swap/v1/positionSide/dual", env) or {}
        _position_mode_cache[key] = (
            "hedge" if str(data.get("dualSidePosition", "")).lower() == "true" else "oneway"
        )
    return _position_mode_cache[key]


def _position_side(env, direction, closing=False):
    """direction: 'long'|'short' (of the POSITION). One-way mode always sends
    BOTH; hedge mode sends LONG/SHORT regardless of open/close."""
    if get_position_mode(env) == "oneway":
        return "BOTH"
    return direction.upper()


# ── orders ───────────────────────────────────────────────────────────────────

def _unwrap_order(data):
    """BingX nests the order object: {data: {order: {...}}}. orderId is a huge
    integer — return it as str and never let callers do float math on it."""
    order = (data or {}).get("order", data) or {}
    if "orderId" in order:
        order["orderId"] = str(order["orderId"])
    return order


def _attach_protection(params, sl_price=None, tp_price=None, qty=None):
    """Attach stopLoss/takeProfit to an entry order as BingX's stringified-JSON
    fields — one atomic request, no naked-position window."""
    if sl_price is not None:
        params["stopLoss"] = json.dumps({
            "type": "STOP_MARKET", "stopPrice": float(sl_price),
            "workingType": "MARK_PRICE", **({"quantity": float(qty)} if qty else {}),
        })
    if tp_price is not None:
        params["takeProfit"] = json.dumps({
            "type": "TAKE_PROFIT_MARKET", "stopPrice": float(tp_price),
            "workingType": "MARK_PRICE", **({"quantity": float(qty)} if qty else {}),
        })
    return params


def place_market_order(env, symbol, direction, qty, client_order_id=None,
                       reduce_only=False, sl_price=None, tp_price=None,
                       confirm_timeout=15):
    """Market order, CONFIRMED. direction: 'long'|'short' = the position this
    order builds (when reduce_only, the position it reduces). Returns the
    confirmed order dict from `confirm_order` — exchange-reported avg_price /
    executed_qty / commission, not intent.

    client_order_id: pass a unique-per-intent id ("mystrat20260709120500") so
    an accidental resubmit is rejected (error 101481) instead of doubling the
    position. ALPHANUMERIC ONLY, 1-40 chars, lowercased by BingX. Note the
    request param spelling is clientOrderID (official docs mix both spellings;
    capital ID is what ccxt ships in production), while query RESPONSES come
    back as clientOrderId."""
    symbol = _bingx_symbol(symbol)
    mode = get_position_mode(env)
    if reduce_only:
        side = "SELL" if direction == "long" else "BUY"
    else:
        side = "BUY" if direction == "long" else "SELL"
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": _position_side(env, direction),
        "type": "MARKET",
        "quantity": format_qty(env, symbol, qty),
    }
    if reduce_only and mode == "oneway":
        params["reduceOnly"] = "true"  # hedge mode expresses this via positionSide
    if client_order_id:
        params["clientOrderID"] = client_order_id  # capital ID on requests
    _attach_protection(params, sl_price, tp_price)

    placed = _unwrap_order(_request("POST", "/openApi/swap/v2/trade/order", env, params))
    if not placed.get("orderId"):
        raise BingXError("N/A", f"order response missing orderId: {placed}", "trade/order")
    return confirm_order(env, symbol, placed["orderId"], timeout=confirm_timeout)


def place_limit_order(env, symbol, direction, qty, price, client_order_id=None,
                      reduce_only=False, time_in_force="GTC"):
    """Limit order. Returns the raw (unconfirmed) order dict with orderId —
    limit orders may rest; use confirm_order / get_order to track it."""
    symbol = _bingx_symbol(symbol)
    mode = get_position_mode(env)
    if reduce_only:
        side = "SELL" if direction == "long" else "BUY"
    else:
        side = "BUY" if direction == "long" else "SELL"
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": _position_side(env, direction),
        "type": "LIMIT",
        "quantity": format_qty(env, symbol, qty, price=price),
        "price": format_price(env, symbol, price),
        "timeInForce": time_in_force,
    }
    if reduce_only and mode == "oneway":
        params["reduceOnly"] = "true"
    if client_order_id:
        params["clientOrderID"] = client_order_id
    return _unwrap_order(_request("POST", "/openApi/swap/v2/trade/order", env, params))


def get_order(env, symbol, order_id):
    """Order details by orderId. Normalized: status, avg_price, executed_qty,
    orig_qty, commission (abs — BingX returns it negative), client_order_id,
    plus the raw dict under 'raw'."""
    data = _request("GET", "/openApi/swap/v2/trade/order", env,
                    {"symbol": symbol, "orderId": str(order_id)})
    o = _unwrap_order(data)
    if "status" not in o:
        raise BingXError("N/A", f"order query missing status: {o}", "trade/order")
    return {
        "order_id": o["orderId"],
        "status": o["status"],
        "avg_price": float(o.get("avgPrice") or 0),
        "executed_qty": float(o.get("executedQty") or 0),
        "orig_qty": float(o.get("origQty") or 0),
        "commission": abs(float(o.get("commission") or 0)),
        # response uses lowercase-d clientOrderId (request uses clientOrderID)
        "client_order_id": o.get("clientOrderId") or o.get("clientOrderID") or "",
        "raw": o,
    }


# BingX emits BOTH enum families: docs-v3 says NEW/PENDING/PARTIALLYFILLED/
# CANCELLED/FAILED, the skills repo says PARTIALLY_FILLED/CANCELED/EXPIRED —
# accept the union; anything not terminal here is treated as still pending.
TERMINAL_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "FAILED", "EXPIRED"}


def confirm_order(env, symbol, order_id, timeout=15):
    """Poll until the order reaches a terminal state. Returns get_order()'s
    normalized dict. Raises BingXError if the exchange reports CANCELED/FAILED,
    OrderNotConfirmed if still pending after timeout — in that case the order
    MAY STILL FILL; do not blindly resubmit (use client_order_id anyway)."""
    deadline = time.time() + timeout
    order = None
    while time.time() < deadline:
        order = get_order(env, symbol, order_id)
        if order["status"] == "FILLED":
            return order
        if order["status"] in TERMINAL_STATUSES:
            raise BingXError("N/A", f"order {order_id} ended {order['status']}", "confirm")
        time.sleep(1)
    raise OrderNotConfirmed(
        f"order {order_id} still {order['status'] if order else 'UNKNOWN'} after {timeout}s"
    )


def get_open_orders(env, symbol=None):
    """All open orders (entry + conditional). Returns a list of raw order dicts
    with orderId normalized to str."""
    symbol = _bingx_symbol(symbol) if symbol else symbol
    params = {"symbol": symbol} if symbol else {}
    data = _request("GET", "/openApi/swap/v2/trade/openOrders", env, params) or {}
    orders = data.get("orders", []) if isinstance(data, dict) else data
    for o in orders:
        if "orderId" in o:
            o["orderId"] = str(o["orderId"])
    return orders


def cancel_order(env, symbol, order_id=None, client_order_id=None):
    """Cancel one order by orderId or clientOrderID. Returns the canceled
    order's raw dict. Raises BingXError if the exchange refuses."""
    symbol = _bingx_symbol(symbol)
    params = {"symbol": symbol}
    if order_id:
        params["orderId"] = str(order_id)
    elif client_order_id:
        params["clientOrderID"] = client_order_id
    else:
        raise ValueError("order_id or client_order_id required")
    return _unwrap_order(_request("DELETE", "/openApi/swap/v2/trade/order", env, params))


def cancel_all_orders(env, symbol):
    """Cancel ALL open orders for a symbol (including protective orders —
    only do this when also closing the position)."""
    return _request("DELETE", "/openApi/swap/v2/trade/allOpenOrders", env,
                    {"symbol": _bingx_symbol(symbol)})


# ── protective orders (standalone) ───────────────────────────────────────────

def place_protective_orders(env, symbol, direction, qty=None, sl_price=None,
                            tp_price=None, verify=True):
    """Standalone reduce-only STOP_MARKET / TAKE_PROFIT_MARKET orders for an
    EXISTING position (use open_position's attached protection for new entries;
    use this for multi-level TPs or repairing protection).

    direction is the POSITION's direction ('long'|'short'); the protective
    orders take the opposite side automatically. qty=None protects the WHOLE
    position via closePosition=true (triggers a full close, survives later
    position-size changes — prefer it unless doing partial TPs). Verifies the
    orders exist on the exchange before returning. Raises ProtectionFailed if
    placement or verification fails — the caller MUST alert the user (naked
    position).

    VST quirk: the demo environment rejects closePosition=true with 109400
    ("parameter quantity or stopPrice is must") — on BINGX_DEMO=true, pass an
    explicit qty instead."""
    symbol = _bingx_symbol(symbol)
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
                "symbol": symbol,
                "side": close_side,
                "positionSide": _position_side(env, direction),
                "type": otype,
                "stopPrice": format_price(env, symbol, trigger),
                "workingType": "MARK_PRICE",
            }
            if qty is None:
                # closePosition carries its own reduce-only semantics and
                # must NOT be combined with quantity or reduceOnly
                params["closePosition"] = "true"
            else:
                params["quantity"] = format_qty(env, symbol, qty)
                if mode == "oneway":
                    params["reduceOnly"] = "true"
            order = _unwrap_order(_request("POST", "/openApi/swap/v2/trade/order", env, params))
            if not order.get("orderId"):
                raise BingXError("N/A", f"{otype} response missing orderId", "trade/order")
            placed[kind] = order
    except Exception as e:
        raise ProtectionFailed(
            f"{symbol} {direction} position may be NAKED — placing protective orders "
            f"failed after {list(placed)} succeeded: {e}"
        ) from e

    if verify and placed:
        open_ids = {o.get("orderId") for o in get_open_orders(env, symbol)}
        missing = [k for k, o in placed.items() if o["orderId"] not in open_ids]
        if missing:
            raise ProtectionFailed(
                f"{symbol} {direction}: protective order(s) {missing} were accepted but "
                f"are NOT in open orders — verify manually, position may be naked"
            )
    return placed


# ── high-level: the recommended entry flow ───────────────────────────────────

def open_position(env, symbol, direction, qty, sl_price=None, tp_price=None,
                  client_order_id=None, confirm_timeout=15):
    """Open a position the safe way: ONE atomic market order with stopLoss/
    takeProfit attached (no naked window), confirmed against the exchange,
    protection verified in open orders.

    Returns {'entry': confirmed order dict, 'protection': [raw conditional
    orders found on the exchange]}. Raises ProtectionFailed if sl/tp was
    requested but no conditional order is visible after the fill."""
    symbol = _bingx_symbol(symbol)
    entry = place_market_order(
        env, symbol, direction, qty,
        client_order_id=client_order_id,
        sl_price=sl_price, tp_price=tp_price,
        confirm_timeout=confirm_timeout,
    )
    protection = []
    if sl_price is not None or tp_price is not None:
        # attached SL/TP become separate conditional orders once entry fills
        expected = (1 if sl_price is not None else 0) + (1 if tp_price is not None else 0)
        for _ in range(5):
            protection = [
                o for o in get_open_orders(env, symbol)
                if o.get("type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT")
            ]
            if len(protection) >= expected:
                break
            time.sleep(1)
        if len(protection) < expected:
            raise ProtectionFailed(
                f"{symbol} {direction} entry FILLED (avg {entry['avg_price']}) but only "
                f"{len(protection)}/{expected} protective orders visible — position may be "
                f"naked, alert the user NOW"
            )
    return {"entry": entry, "protection": protection}


def close_position(env, symbol, direction, qty, client_order_id=None):
    """Market-close (part of) an existing position, confirmed. direction is the
    POSITION being closed ('long'|'short')."""
    symbol = _bingx_symbol(symbol)
    return place_market_order(
        env, symbol, direction, qty,
        client_order_id=client_order_id, reduce_only=True,
    )


# ── fills ────────────────────────────────────────────────────────────────────

def get_fills(env, symbol, start_ms, end_ms):
    """Fill records for a time range (≤30 days back). Quirks handled: params
    are startTs/endTs (NOT startTime/endTime), response key is data.fill_orders
    (snake_case), filledTime is an ISO-8601 STRING (not ms). tradingUnit=COIN
    is sent so `volume` is already coin quantity (CONT would mean contracts)."""
    data = _request("GET", "/openApi/swap/v2/trade/allFillOrders", env, {
        "symbol": _bingx_symbol(symbol),
        "startTs": str(int(start_ms)),
        "endTs": str(int(end_ms)),
        "tradingUnit": "COIN",
    }) or {}
    fills = data.get("fill_orders") if isinstance(data, dict) else data
    if fills is None:
        raise BingXError("N/A", f"allFillOrders missing fill_orders key: {list(data)}",
                         "allFillOrders")
    return fills


# ── leverage ─────────────────────────────────────────────────────────────────

def set_leverage(env, symbol, leverage, side="LONG"):
    """Set leverage. In one-way mode BingX still takes side LONG/SHORT — set
    both sides for symmetry. Never silently inherit: query first, set only on
    user instruction (see references/manager.md confirmation rules)."""
    return _request("POST", "/openApi/swap/v2/trade/leverage", env, {
        "symbol": _bingx_symbol(symbol), "side": side, "leverage": str(int(leverage)),
    })


def get_leverage(env, symbol):
    data = _request("GET", "/openApi/swap/v2/trade/leverage", env,
                    {"symbol": _bingx_symbol(symbol)}) or {}
    return {
        "long": int(data.get("longLeverage") or data.get("leverage") or 0),
        "short": int(data.get("shortLeverage") or data.get("leverage") or 0),
    }


# ── VST demo ─────────────────────────────────────────────────────────────────

def claim_demo_funds(env, amount=100000):
    """Credit VST paper-trading balance (BINGX_DEMO=true only; the endpoint
    exists only on the VST domain). amount: whole number, ≤1,000,000 per call,
    10,000,000 lifetime cap."""
    if _bases(env) != [DEMO_URL]:
        raise ValueError("claim_demo_funds only works with BINGX_DEMO=true")
    return _request("POST", "/openApi/swap/v2/trade/getVst", env, {
        "adjustType": "0", "amount": str(int(amount)),
    })


def close_position_partial(env, symbol, direction, qty, client_order_id=None):
    """Cross-venue reconciler contract name. BingX's close_position already
    takes qty (partial-capable) — same call; OKX splits full vs partial, so
    the reconciler standardises on close_position_partial."""
    return close_position(env, symbol, direction, qty, client_order_id=client_order_id)
