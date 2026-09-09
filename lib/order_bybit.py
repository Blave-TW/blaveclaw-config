"""
Bybit order execution — USDT perpetual (linear) + spot, Unified Trading Account.

Ships implemented: NEVER hand-write Bybit order calls in a strategy, import from
here. Same contract and surface as lib/order_bingx.py — the four reconciler names
(get_contract_rules / format_qty / place_market_order / close_position_partial),
the limit layer, the spot layer, open_position with atomic SL/TP, and lib/guard
halt + audit built into the transport.

All functions take `env` (dotenv dict) first. `direction` is always the POSITION's
direction ('long' / 'short'); position mode (one-way vs hedge) is auto-detected.

Bybit facts, all measured live on a UTA account 2026-09-09:
- SL/TP are NATIVE and atomic — passed on /v5/order/create itself, no algo-order
  endpoint and no naked window.
- post-only that would cross is ACCEPTED then auto-cancelled with
  rejectReason EC_PostOnlyWillTakeLiquidity (OKX-style, not an error code).
- orderLinkId dedup is PERMANENT, not just the unfilled window: resending a
  FILLED id returns retCode 110072. Unlike Binance/OKX, a blind resubmit cannot
  double the position.
- Hedge mode HARD-REJECTS an order with no positionIdx (retCode 10001).
- Spot lives inside the UNIFIED wallet (no separate spot account), spot buy fees
  are charged in the BASE coin, and both spot sides gate on minOrderAmt (value).
"""

import json
import time
from decimal import ROUND_DOWN, Decimal

import requests

from lib import guard

HOST = "https://api.bybit.com"
RECV_WINDOW = "5000"
BROKER_REFERER = "Ue001036"  # broker attribution — MANDATORY on every request

_RULES_CACHE = {}

# Bybit's own terminal vocabularies, normalized to the platform's words here so
# venue_wiring._norm_status never has to learn them. PartiallyFilledCanceled is
# the one that bites: it is TERMINAL but reads like an open partial, and a chase
# loop that treats it as open idles out its whole window.
_FILLED = {"filled"}
_GONE = {"cancelled", "canceled", "rejected", "deactivated",
         "partiallyfilledcanceled"}
# post-only that would have crossed: accepted, then killed with this reason
_POST_ONLY_REJECT = "ec_postonlywilltakeliquidity"
# cancel of an order that is already gone (filled/cancelled) OR never existed
_CANCEL_GONE_CODE = 110001


class BybitError(Exception):
    pass


class OrderNotConfirmed(Exception):
    """Placed but not terminal within the timeout — it may still fill.
    Re-query; NEVER blindly resubmit."""


class ProtectionFailed(Exception):
    """Entry filled but SL/TP is not visible — ALERT THE USER NOW."""


def _creds(env):
    api_key = env.get("BYBIT_API_KEY")
    # BYBIT_SECRET_KEY is what the platform writes (the {ID}_SECRET_KEY shape
    # used by the web bind and lib/venue.py). BYBIT_API_SECRET is the name the
    # published blave-quant-skill documented until 2026-09, so BYO-agent users
    # have it in their own .env — accept it rather than breaking them, same as
    # account_gateio.py accepts both GATEIO_* and GATE_*.
    secret = env.get("BYBIT_SECRET_KEY") or env.get("BYBIT_API_SECRET")
    if not api_key or not secret:
        raise ValueError("BYBIT_API_KEY / BYBIT_SECRET_KEY missing from .env")
    return api_key, secret


def _order_intent(method, path, body):
    """'entry' | 'reduce' | 'protective' | 'cancel' | None.

    Classified from the BODY, not the URL: HALT must block new exposure while
    letting every close, SL/TP and cancel through. A conditional order type
    alone is NOT protective — it must carry reduce semantics."""
    if method != "POST":
        return None
    p = body or {}
    if path in ("/v5/order/cancel", "/v5/order/cancel-all"):
        return "cancel"
    if path == "/v5/position/trading-stop":
        return "protective"
    if path == "/v5/order/create":
        if p.get("category") == "spot":
            # spot has no positions: BUY builds exposure, SELL liquidates
            return "reduce" if str(p.get("side", "")).lower() == "sell" else "entry"
        if p.get("reduceOnly"):
            return "reduce"
        return "entry"
    return None


_AUDIT_KEYS = ("category", "symbol", "side", "qty", "price", "orderType",
               "timeInForce", "reduceOnly", "positionIdx", "orderLinkId",
               "takeProfit", "stopLoss", "marketUnit")


def _request(env, method, path, params=None, body=None, retries=3):
    """Signed v5 request with guard gate + audit on order-mutating calls."""
    intent = _order_intent(method, path, body)
    if intent is None:
        return _send(env, method, path, params, body, retries)
    fields = {k: (body or {})[k] for k in _AUDIT_KEYS if k in (body or {})}
    fields["intent"] = intent
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
        data = _send(env, method, path, params, body, retries)
    except Exception as e:
        guard.audit("order_error", error=str(e), **fields)
        raise
    guard.audit("order_ok", order_id=str((data or {}).get("orderId") or ""), **fields)
    return data


def _send(env, method, path, params=None, body=None, retries=3):
    api_key, secret = _creds(env)
    import hashlib
    import hmac
    last_err = None
    for attempt in range(retries):
        ts = str(int(time.time() * 1000))
        if method.upper() == "GET":
            payload = "&".join(f"{k}={v}" for k, v in (params or {}).items())
            url = f"{HOST}{path}" + (f"?{payload}" if payload else "")
            data = None
        else:
            payload = json.dumps(body or {}, separators=(",", ":"))
            url = f"{HOST}{path}"
            data = payload
        sign = hmac.new(secret.encode(),
                        (ts + api_key + RECV_WINDOW + payload).encode(),
                        hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": sign,
            "referer": BROKER_REFERER,
            "Content-Type": "application/json",
        }
        try:
            r = requests.request(method.upper(), url, headers=headers,
                                 data=data, timeout=15)
        except requests.exceptions.ConnectionError as e:
            # A POST whose response died may still have landed. Bybit's
            # orderLinkId dedup is permanent (110072 even after fill), so a
            # retry here CANNOT double the position — unlike Gate.io, POSTs
            # carrying an orderLinkId are safe to retry.
            if method.upper() == "POST" and not (body or {}).get("orderLinkId"):
                raise
            last_err = e
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)
            continue
        r.raise_for_status()
        payload_json = r.json()
        code = payload_json.get("retCode")
        if code == 0:
            return payload_json.get("result") or {}
        link_id = (body or {}).get("orderLinkId")
        if code == 110072 and attempt > 0 and link_id:
            # We only got here by retrying a POST whose connection died. A
            # duplicate orderLinkId therefore means the FIRST attempt actually
            # landed — raising would report a failure for an order that exists
            # (and may already have filled). Resolve it back to its orderId.
            found = _lookup_by_link_id(env, (body or {}).get("category", "linear"),
                                       link_id)
            if found:
                return found
        msg = str(payload_json.get("retMsg") or "")
        # 10004's message echoes the signed origin_string, which CONTAINS THE
        # API KEY — it reaches logs and the web connect-failure page.
        if api_key and api_key in msg:
            msg = msg.replace(api_key, "***")
        if code == 10006 and attempt < retries - 1:  # rate limited — back off
            time.sleep(1 + attempt * 2)
            last_err = BybitError(f"bybit {path} retCode={code}: {msg}")
            continue
        raise BybitError(f"bybit {path} retCode={code}: {msg}")
    raise last_err


def _lookup_by_link_id(env, category, link_id):
    """Resolve an orderLinkId back to its order row (open first, then history).
    Used only to recover from a retried POST that turned out to have landed."""
    for path in ("/v5/order/realtime", "/v5/order/history"):
        try:
            rows = _send(env, "GET", path,
                         {"category": category, "orderLinkId": link_id},
                         retries=1).get("list") or []
        except Exception:
            continue
        if rows:
            return {"orderId": rows[0].get("orderId"), "orderLinkId": link_id}
    return None


def _public(path, params):
    r = requests.get(f"{HOST}{path}", params=params,
                     headers={"referer": BROKER_REFERER}, timeout=10)
    r.raise_for_status()
    body = r.json()
    if body.get("retCode") != 0:
        raise BybitError(f"bybit {path} retCode={body.get('retCode')}: {body.get('retMsg')}")
    return body.get("result") or {}


def _floor_str(value, step):
    """Floor `value` to a whole number of `step`, returned as the venue's own
    plain-decimal string.

    `step` MUST arrive as the ORIGINAL string from the instrument filter
    ('0.00001'), never as a float. float('0.00001') stringifies as '1e-05',
    whose decimal-place count reads as 0, which formats every price as '0' —
    and Bybit ACCEPTS stopLoss/takeProfit '0' silently, treating it as no
    protection at all (measured: an entry reported filled with an audit body
    carrying stopLoss '0' and came back naked). Decimal also avoids the binary
    float error that makes 6.0 / 0.1 = 59.999... drop a whole step."""
    s = Decimal(str(step))
    if s <= 0:
        return str(value)
    v = Decimal(str(value))
    floored = (v / s).to_integral_value(rounding=ROUND_DOWN) * s
    return format(floored.quantize(s), "f")


def _floor_to(value, step):
    """Numeric form of _floor_str, for min-size comparisons."""
    return float(_floor_str(value, step))


# --------------------------------------------------------------------------
# Reconciler's four fixed names
# --------------------------------------------------------------------------

def get_contract_rules(env: dict, symbol: str) -> dict:
    """Linear-perp rules for a CANONICAL symbol. Bybit symbols are already
    canonical ('BTCUSDT'), so there is no conversion. contract_value is 1:
    Bybit linear qty IS base units, never a contract count."""
    key = ("linear", symbol)
    if key not in _RULES_CACHE:
        rows = _public("/v5/market/instruments-info",
                       {"category": "linear", "symbol": symbol}).get("list") or []
        if not rows:
            raise BybitError(f"bybit: unknown linear symbol {symbol}")
        lot, price = rows[0]["lotSizeFilter"], rows[0]["priceFilter"]
        _RULES_CACHE[key] = {
            "step": str(lot["qtyStep"]),
            "min_qty": float(lot["minOrderQty"]),
            "min_notional": float(lot.get("minNotionalValue") or 0),
            "contract_value": 1.0,
            "tick": str(price["tickSize"]),
        }
    return _RULES_CACHE[key]


def format_qty(env: dict, symbol: str, qty: float, price: float = None) -> str:
    """Floor a BASE qty to the step; empty string when it is below the venue
    minimum. MIN-SIZE GATE ONLY — never feed the result back into
    place_market_order (Bybit qty is base units, so it would be a no-op here,
    but the contract is shared across venues where it is not)."""
    rules = get_contract_rules(env, symbol)
    step = rules["step"]
    floored = _floor_to(abs(float(qty)), step)
    if floored < rules["min_qty"]:
        return ""
    if rules["min_notional"] and price and floored * float(price) < rules["min_notional"]:
        return ""
    return _floor_str(floored, step)


def _position_rows(env: dict, symbol: str) -> list:
    """One /v5/position/list read, reused for BOTH the position-mode decision
    and the mark price. NEVER cached: the user can flip one-way<->hedge in the
    Bybit app at any time, and a stale mode sends the wrong positionIdx — which
    is either a 10001 hard reject or, worse, an order that opens the OPPOSITE
    leg (the hedge-mode bug this contract exists to prevent)."""
    return _request(env, "GET", "/v5/position/list",
                    {"category": "linear", "symbol": symbol}).get("list") or []


def _position_mode(rows) -> int:
    """0 = one-way, 3 = hedge. Hedge mode HARD-REJECTS an order carrying no
    positionIdx (retCode 10001 'position idx not match position mode')."""
    return 3 if {int(r.get("positionIdx") or 0) for r in rows} - {0} else 0


def _position_idx(rows, direction):
    """In hedge mode the index identifies the LEG: 1 = long, 2 = short. For a
    reduce order it must name the leg BEING CLOSED, not the order side — an
    order side alone would open the opposite leg."""
    if _position_mode(rows) == 0:
        return 0
    return 1 if direction == "long" else 2


def _mark_price(rows):
    for r in rows:
        px = float(r.get("markPrice") or 0)
        if px > 0:
            return px
    return 0.0


def _order_side(direction, reduce_only):
    if reduce_only:
        return "Sell" if direction == "long" else "Buy"
    return "Buy" if direction == "long" else "Sell"


def _confirm(env, symbol, order_id, category="linear", timeout=20):
    """Poll to a terminal state and report what the EXCHANGE says. Market
    orders split across several fills, so the aggregate on the order row
    (cumExecQty / avgPrice / cumExecFee) is the answer, not any single fill."""
    deadline = time.time() + timeout
    delay = 0.4
    while time.time() < deadline:
        rows = (_request(env, "GET", "/v5/order/realtime",
                         {"category": category, "orderId": order_id}).get("list") or [])
        if not rows:
            rows = (_request(env, "GET", "/v5/order/history",
                             {"category": category, "orderId": order_id}).get("list") or [])
        if rows:
            row = rows[0]
            status = str(row.get("orderStatus") or "").lower()
            if status in _FILLED or status in _GONE:
                return row
        time.sleep(delay)
        delay = min(delay * 1.6, 2.0)  # back off — bursts return retCode 10006
    raise OrderNotConfirmed(
        f"bybit {symbol} order {order_id} not terminal in {timeout}s — re-query, "
        f"do NOT resubmit (orderLinkId dedup is permanent, a resend returns 110072)")


def _fills(row):
    qty = float(row.get("cumExecQty") or 0)
    avg = row.get("avgPrice")
    return {
        "avg_price": float(avg) if avg not in (None, "") else 0.0,
        "executed_qty": qty,
        "commission": float(row.get("cumExecFee") or 0),
        "order_id": row.get("orderId"),
    }


def place_market_order(env: dict, symbol: str, direction: str, qty: float,
                       client_order_id: str = None, reduce_only: bool = False):
    """Confirmed market order. qty is BASE currency. Returns exchange-reported
    fills, or False when the size is below the venue minimum (intentional skip,
    not an error — no phantom-trade notification)."""
    rules = get_contract_rules(env, symbol)
    step = rules["step"]
    sized = _floor_to(abs(float(qty)), step)
    if sized < rules["min_qty"]:
        return False
    rows = _position_rows(env, symbol)
    if rules["min_notional"]:
        # markPrice off the row we already hold — a separate get_bbo call per
        # order is one more request per reconciler cycle against a rate limit
        # that already returns 10006 under load.
        px = _mark_price(rows) or ((lambda b: (b["bid"] + b["ask"]) / 2)(get_bbo(env, symbol)))
        if sized * px < rules["min_notional"]:
            return False
    body = {
        "category": "linear", "symbol": symbol,
        "side": _order_side(direction, reduce_only),
        "orderType": "Market", "qty": _floor_str(sized, step),
        "positionIdx": _position_idx(rows, direction),
    }
    if reduce_only:
        body["reduceOnly"] = True
    if client_order_id:
        body["orderLinkId"] = client_order_id
    res = _request(env, "POST", "/v5/order/create", body=body)
    row = _confirm(env, symbol, res["orderId"])
    status = str(row.get("orderStatus") or "").lower()
    if status in _GONE and float(row.get("cumExecQty") or 0) == 0:
        raise BybitError(
            f"bybit {symbol} market order {status}: "
            f"{row.get('rejectReason') or row.get('cancelType') or 'no reason given'}")
    return _fills(row)


def close_position_partial(env: dict, symbol: str, direction: str, qty: float,
                           client_order_id: str = None):
    """Reduce an existing position by a BASE qty, reduce-only. `direction` is
    the position BEING CLOSED. Works under HALT — closing is always allowed."""
    return place_market_order(env, symbol, direction, qty,
                              client_order_id=client_order_id, reduce_only=True)


# --------------------------------------------------------------------------
# Entry with native protection
# --------------------------------------------------------------------------

def place_protective_orders(env: dict, symbol: str, direction: str,
                            sl_price: float = None, tp_price: float = None):
    """Attach/replace SL and TP on an OPEN position (whole position, Full mode).
    Prefer open_position() — attaching at entry leaves no naked window."""
    if sl_price is None and tp_price is None:
        raise ValueError("place_protective_orders needs sl_price and/or tp_price")
    tick = get_contract_rules(env, symbol)["tick"]
    body = {"category": "linear", "symbol": symbol, "tpslMode": "Full",
            "positionIdx": _position_idx(_position_rows(env, symbol), direction)}
    if sl_price is not None:
        body["stopLoss"] = _floor_str(sl_price, tick)
    if tp_price is not None:
        body["takeProfit"] = _floor_str(tp_price, tick)
    return _request(env, "POST", "/v5/position/trading-stop", body=body)


def open_position(env: dict, symbol: str, direction: str, qty: float,
                  sl_price: float = None, tp_price: float = None,
                  client_order_id: str = None):
    """RECOMMENDED ENTRY FLOW: one atomic market order with SL/TP attached —
    Bybit accepts takeProfit/stopLoss on /v5/order/create itself, so there is
    no window where the position sits unprotected.

    Returns {'entry': <confirmed fills>, 'protection': [...]}. Raises
    ProtectionFailed if protection is not visible after the fill — treat that
    as an ALERT-THE-USER-NOW event, never swallow it."""
    rules = get_contract_rules(env, symbol)
    step = rules["step"]
    tick = rules["tick"]
    sized = _floor_to(abs(float(qty)), step)
    if sized < rules["min_qty"]:
        return False
    body = {
        "category": "linear", "symbol": symbol,
        "side": _order_side(direction, False), "orderType": "Market",
        "qty": _floor_str(sized, step), "tpslMode": "Full",
        "positionIdx": _position_idx(_position_rows(env, symbol), direction),
    }
    if sl_price is not None:
        body["stopLoss"] = _floor_str(sl_price, tick)
    if tp_price is not None:
        body["takeProfit"] = _floor_str(tp_price, tick)
    if client_order_id:
        body["orderLinkId"] = client_order_id
    res = _request(env, "POST", "/v5/order/create", body=body)
    row = _confirm(env, symbol, res["orderId"])
    if str(row.get("orderStatus") or "").lower() not in _FILLED:
        raise BybitError(f"bybit {symbol} entry not filled: "
                         f"{row.get('rejectReason') or row.get('orderStatus')}")
    entry = _fills(row)
    if sl_price is None and tp_price is None:
        return {"entry": entry, "protection": []}
    pos = _position_rows(env, symbol)
    idx = _position_idx(pos, direction)
    row = next((p for p in pos if int(p.get("positionIdx") or 0) == idx), {})
    got = {"stopLoss": row.get("stopLoss") or "", "takeProfit": row.get("takeProfit") or ""}
    if (sl_price is not None and not got["stopLoss"]) or \
       (tp_price is not None and not got["takeProfit"]):
        raise ProtectionFailed(
            f"bybit {symbol} {direction} filled {entry['executed_qty']} but "
            f"protection is missing (sl={got['stopLoss']!r} tp={got['takeProfit']!r}) "
            f"— the position is NAKED, act now")
    return {"entry": entry, "protection": [got]}


# --------------------------------------------------------------------------
# Limit layer (chase / TWAP)
# --------------------------------------------------------------------------

def get_bbo(env: dict, symbol: str) -> dict:
    """True best bid/ask — the ticker carries them directly; never substitute
    mark or last price."""
    t = (_public("/v5/market/tickers",
                 {"category": "linear", "symbol": symbol}).get("list") or [{}])[0]
    return {"bid": float(t["bid1Price"]), "ask": float(t["ask1Price"])}


def get_mark_price(env: dict, symbol: str) -> float:
    """Live mark price (public ticker) — the cross-venue wiring contract for
    USD→qty conversion (lib/venue_wiring.py) and for the min-slice floor in
    lib/execute._venue_min_slice_usd. Falls back to lastPrice: a ticker without
    markPrice must not read as 0, which would collapse the min-slice gate to
    the flat floor and re-open the abort/re-dispatch loop it exists to stop."""
    t = (_public("/v5/market/tickers",
                 {"category": "linear", "symbol": symbol}).get("list") or [{}])[0]
    return float(t.get("markPrice") or t.get("lastPrice"))


def get_spot_price(env: dict, symbol: str) -> float:
    """Last spot price (public)."""
    t = (_public("/v5/market/tickers",
                 {"category": "spot", "symbol": symbol}).get("list") or [{}])[0]
    return float(t["lastPrice"])


def place_limit_order(env: dict, symbol: str, direction: str, qty: float,
                      price: float, client_order_id: str = None,
                      reduce_only: bool = False, time_in_force: str = "GTC",
                      post_only: bool = False):
    """Limit order. Returns {'order_id', 'status'} — or
    {'status': 'post_only_rejected'} when a post-only order would have crossed,
    or False below the venue minimum.

    Bybit ACCEPTS a crossing post-only order and then auto-cancels it with
    rejectReason EC_PostOnlyWillTakeLiquidity (measured) — there is no rejection
    at placement time, so the order must be read back before it can be called
    placed. The chase engine relies on this mapping to re-post."""
    rules = get_contract_rules(env, symbol)
    step, tick = rules["step"], rules["tick"]
    sized = _floor_to(abs(float(qty)), step)
    if sized < rules["min_qty"]:
        return False
    if rules["min_notional"] and sized * float(price) < rules["min_notional"]:
        return False
    body = {
        "category": "linear", "symbol": symbol,
        "side": _order_side(direction, reduce_only), "orderType": "Limit",
        "qty": _floor_str(sized, step), "price": _floor_str(price, tick),
        "timeInForce": "PostOnly" if post_only else time_in_force,
        "positionIdx": _position_idx(_position_rows(env, symbol), direction),
    }
    if reduce_only:
        body["reduceOnly"] = True
    if client_order_id:
        body["orderLinkId"] = client_order_id
    res = _request(env, "POST", "/v5/order/create", body=body)
    oid = res["orderId"]
    if post_only:
        rows = (_request(env, "GET", "/v5/order/realtime",
                         {"category": "linear", "orderId": oid}).get("list") or [])
        if not rows:
            rows = (_request(env, "GET", "/v5/order/history",
                             {"category": "linear", "orderId": oid}).get("list") or [])
        row = rows[0] if rows else {}
        if str(row.get("rejectReason") or "").lower() == _POST_ONLY_REJECT:
            return {"status": "post_only_rejected", "order_id": oid}
    return {"order_id": oid, "status": "open"}


def get_order(env: dict, symbol: str, order_id: str) -> dict:
    """{'status', 'orig_qty', 'executed_qty', 'avg_price'} with the status
    already in the platform's vocabulary. PartiallyFilledCanceled is mapped to
    'canceled': it is terminal, and reading it as open idles out a chase."""
    rows = (_request(env, "GET", "/v5/order/realtime",
                     {"category": "linear", "orderId": order_id}).get("list") or [])
    if not rows:
        rows = (_request(env, "GET", "/v5/order/history",
                         {"category": "linear", "orderId": order_id}).get("list") or [])
    if not rows:
        return {"status": "canceled", "orig_qty": 0.0, "executed_qty": 0.0,
                "avg_price": 0.0}
    row = rows[0]
    raw = str(row.get("orderStatus") or "").lower()
    status = "filled" if raw in _FILLED else ("canceled" if raw in _GONE else "open")
    avg = row.get("avgPrice")
    return {
        "status": status,
        "orig_qty": float(row.get("qty") or 0),
        "executed_qty": float(row.get("cumExecQty") or 0),
        "avg_price": float(avg) if avg not in (None, "") else 0.0,
    }


def cancel_order(env: dict, symbol: str, order_id: str = None,
                 client_order_id: str = None):
    """Cancel by id. An already-filled / already-cancelled / never-existed order
    returns a status instead of raising (Bybit reports all three as retCode
    110001) — the chase engine treats that as confirmation the order is dead."""
    body = {"category": "linear", "symbol": symbol}
    if order_id:
        body["orderId"] = order_id
    elif client_order_id:
        body["orderLinkId"] = client_order_id
    else:
        raise ValueError("cancel_order needs order_id or client_order_id")
    try:
        res = _request(env, "POST", "/v5/order/cancel", body=body)
    except BybitError as e:
        if f"retCode={_CANCEL_GONE_CODE}" in str(e):
            return {"status": "order_not_found", "order_id": order_id}
        raise
    return {"status": "canceled", "order_id": res.get("orderId") or order_id}


def get_open_orders(env: dict, symbol: str = None) -> list:
    """Open orders for the orphan-order sweep.

    EXCLUDES the position's own SL/TP: Bybit lists those as open conditional
    orders (reduceOnly, stopOrderType TakeProfit / StopLoss), and a sweep that
    cancels them strips the position's protection.

    LINEAR ONLY, deliberately: the orphan sweep runs on the perp side, and spot
    has no position to protect. If a spot sweep is ever added it needs the same
    stopOrderType filter with category='spot' — do not reuse this one by
    swapping the category and calling it done."""
    params = {"category": "linear"}
    if symbol:
        params["symbol"] = symbol
    else:
        params["settleCoin"] = "USDT"
    rows = _request(env, "GET", "/v5/order/realtime", params).get("list") or []
    return [{
        "order_id": r.get("orderId"),
        "client_order_id": r.get("orderLinkId"),
        "symbol": r.get("symbol"),
        "side": r.get("side"),
        "qty": float(r.get("qty") or 0),
        "price": float(r.get("price") or 0),
        "reduce_only": bool(r.get("reduceOnly")),
    } for r in rows if not (r.get("stopOrderType") or "")]


# --------------------------------------------------------------------------
# Spot layer (strategies declaring MARKET = "spot")
# --------------------------------------------------------------------------

def get_spot_rules(env: dict, symbol: str) -> dict:
    """Spot filters are a DIFFERENT SHAPE from linear: base step is
    `basePrecision` (not qtyStep) and the minimum is `minOrderAmt`, a QUOTE
    value that gates BOTH sides (measured: selling 1 DOGE ~0.09 USDT is
    rejected with retCode 170140)."""
    key = ("spot", symbol)
    if key not in _RULES_CACHE:
        rows = _public("/v5/market/instruments-info",
                       {"category": "spot", "symbol": symbol}).get("list") or []
        if not rows:
            raise BybitError(f"bybit: unknown spot symbol {symbol}")
        lot, price = rows[0]["lotSizeFilter"], rows[0]["priceFilter"]
        _RULES_CACHE[key] = {
            "step": str(lot["basePrecision"]),
            "min_qty": float(lot["minOrderQty"]),
            "min_amt": float(lot.get("minOrderAmt") or 0),
            "tick": str(price["tickSize"]),
        }
    return _RULES_CACHE[key]


def format_spot_qty(env: dict, symbol: str, qty: float) -> str:
    rules = get_spot_rules(env, symbol)
    step = rules["step"]
    floored = _floor_to(abs(float(qty)), step)
    return "" if floored < rules["min_qty"] else _floor_str(floored, step)


def get_spot_bbo(env: dict, symbol: str) -> dict:
    t = (_public("/v5/market/tickers",
                 {"category": "spot", "symbol": symbol}).get("list") or [{}])[0]
    return {"bid": float(t["bid1Price"]), "ask": float(t["ask1Price"])}


def get_spot_balances(env: dict) -> dict:
    """{ASSET: free qty}. On a UTA account there is NO separate spot wallet —
    spot inventory is the UNIFIED coin array and doubles as margin."""
    rows = _request(env, "GET", "/v5/account/wallet-balance",
                    {"accountType": "UNIFIED"}).get("list") or []
    out = {}
    for c in (rows[0].get("coin") if rows else []) or []:
        amount = float(c.get("walletBalance") or 0)
        if amount:
            out[(c.get("coin") or "").upper()] = amount
    return out


def place_spot_market_order(env: dict, symbol: str, side: str,
                            base_qty: float = None, quote_qty: float = None,
                            client_order_id: str = None):
    """Spot market order. BUYs are sized in QUOTE currency (marketUnit
    quoteCoin — the USD amount goes straight in, no price conversion); SELLs in
    base qty floored to basePrecision. Below minOrderAmt → False.

    Guard: buy = entry (HALT blocks), sell = reduce (never trapped — clearing
    inventory must not be blocked by a halt).

    The buy fee is charged in the BASE coin (measured: a 6 USDT buy reported
    cumExecQty 66.3 DOGE and credited 66.2337), so the sellable amount is always
    a little under the reported fill — size sells from the WALLET, not from the
    buy's cumExecQty."""
    side = side.lower()
    rules = get_spot_rules(env, symbol)
    body = {"category": "spot", "symbol": symbol,
            "side": "Buy" if side == "buy" else "Sell", "orderType": "Market"}
    if side == "buy":
        if not quote_qty:
            raise ValueError("spot BUY must be sized in quote_qty")
        if float(quote_qty) < rules["min_amt"]:
            return False
        body["qty"] = f"{float(quote_qty):.2f}"
        body["marketUnit"] = "quoteCoin"
    else:
        if not base_qty:
            raise ValueError("spot SELL must be sized in base_qty")
        sized = format_spot_qty(env, symbol, base_qty)
        if not sized:
            return False
        if rules["min_amt"] and float(sized) * get_spot_bbo(env, symbol)["bid"] < rules["min_amt"]:
            return False
        body["qty"] = sized
        body["marketUnit"] = "baseCoin"
    if client_order_id:
        body["orderLinkId"] = client_order_id
    res = _request(env, "POST", "/v5/order/create", body=body)
    row = _confirm(env, symbol, res["orderId"], category="spot")
    if str(row.get("orderStatus") or "").lower() in _GONE and \
            float(row.get("cumExecQty") or 0) == 0:
        raise BybitError(f"bybit spot {symbol} {side} rejected: "
                         f"{row.get('rejectReason') or row.get('orderStatus')}")
    return _fills(row)


def place_spot_limit_order(env: dict, symbol: str, side: str, base_qty: float,
                           price: float, client_order_id: str = None,
                           post_only: bool = False, time_in_force: str = "GTC"):
    rules = get_spot_rules(env, symbol)
    sized = format_spot_qty(env, symbol, base_qty)
    if not sized:
        return False
    if rules["min_amt"] and float(sized) * float(price) < rules["min_amt"]:
        return False
    body = {"category": "spot", "symbol": symbol,
            "side": "Buy" if side.lower() == "buy" else "Sell",
            "orderType": "Limit", "qty": sized,
            "price": _floor_str(price, rules["tick"]),
            "timeInForce": "PostOnly" if post_only else time_in_force}
    if client_order_id:
        body["orderLinkId"] = client_order_id
    res = _request(env, "POST", "/v5/order/create", body=body)
    oid = res["orderId"]
    if post_only:
        rows = (_request(env, "GET", "/v5/order/realtime",
                         {"category": "spot", "orderId": oid}).get("list") or [])
        if not rows:
            rows = (_request(env, "GET", "/v5/order/history",
                             {"category": "spot", "orderId": oid}).get("list") or [])
        if rows and str(rows[0].get("rejectReason") or "").lower() == _POST_ONLY_REJECT:
            return {"status": "post_only_rejected", "order_id": oid}
    return {"order_id": oid, "status": "open"}


def get_spot_order(env: dict, symbol: str, order_id: str) -> dict:
    rows = (_request(env, "GET", "/v5/order/realtime",
                     {"category": "spot", "orderId": order_id}).get("list") or [])
    if not rows:
        rows = (_request(env, "GET", "/v5/order/history",
                         {"category": "spot", "orderId": order_id}).get("list") or [])
    if not rows:
        return {"status": "canceled", "orig_qty": 0.0, "executed_qty": 0.0,
                "avg_price": 0.0}
    row = rows[0]
    raw = str(row.get("orderStatus") or "").lower()
    status = "filled" if raw in _FILLED else ("canceled" if raw in _GONE else "open")
    avg = row.get("avgPrice")
    return {
        "status": status,
        "orig_qty": float(row.get("qty") or 0),
        "executed_qty": float(row.get("cumExecQty") or 0),
        "avg_price": float(avg) if avg not in (None, "") else 0.0,
    }


def cancel_spot_order(env: dict, symbol: str, order_id: str = None,
                      client_order_id: str = None):
    body = {"category": "spot", "symbol": symbol}
    if order_id:
        body["orderId"] = order_id
    elif client_order_id:
        body["orderLinkId"] = client_order_id
    else:
        raise ValueError("cancel_spot_order needs order_id or client_order_id")
    try:
        res = _request(env, "POST", "/v5/order/cancel", body=body)
    except BybitError as e:
        if f"retCode={_CANCEL_GONE_CODE}" in str(e):
            return {"status": "order_not_found", "order_id": order_id}
        raise
    return {"status": "canceled", "order_id": res.get("orderId") or order_id}
