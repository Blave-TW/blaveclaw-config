"""
OKX order execution library — USDT perpetual swap + spot.

Companion to lib/account_okx.py (equity/positions). Credentials: OKX_API_KEY,
OKX_SECRET_KEY, OKX_PASSPHRASE in .env. Set OKX_DEMO=true to route to OKX
demo trading (x-simulated-trading header, demo-mode keys).

Broker attribution (Blave) is the body field `"tag": "96ee7de3fd4bBCDE"` on
every order-creating/modifying POST — injected by the transport here, callers
never handle it.

Design rules (same contract as lib/order_bingx.py / order_binance.py):
FAIL LOUD / CONFIRMED-NOT-SENT / IDEMPOTENT (bounded: clOrdId dedup covers
only the un-filled window — a reused id is ACCEPTED once the original is
terminal, measured live 2026-08-05, same semantics as Binance; on
OrderNotConfirmed re-query, never blindly resubmit) / HALTABLE+AUDITED via
lib/guard / ATTRIBUTION on every order.

OKX quirks handled here (verified against ccxt's production okx parser +
official docs + live orders 2026-08-05):
- swap `sz` is CONTRACTS — base qty is converted via the instrument's ctVal
  (the units pitfall that 10×'d ETH orders elsewhere; callers always pass
  BASE units, conversion lives only in this file)
- the REAL error is data[].sCode/sMsg — the top-level code/msg often just
  says "Operation failed"; both layers are surfaced
- net mode supports reduceOnly natively; long_short (hedge) mode does NOT —
  there closing is expressed by posSide opposite to the order side
- spot market BUYs size in QUOTE currency (tgtCcy=quote_ccy, sz=USDT amount);
  spot SELLs size in base (tgtCcy=base_ccy)
- conditional (SL/TP) orders live on /api/v5/trade/order-algo with their own
  algoId + pending/cancel endpoints — regular order queries never see them
- a crossing post_only order is NEVER rejected at placement — OKX accepts it
  (sCode 0, ordId) and the engine auto-cancels it (cancelSource 31); the
  limit layer probes the order once after placement to surface the chase
  contract's {'status': 'post_only_rejected'}
- cancel failures 51400/51401/51402/51410 all mean "already gone" (does not
  exist / already canceled / already filled / already cancelling) — the
  idempotent-safe cancel returns a status for these instead of raising
"""

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

import requests

from lib import guard

BASE_URL = "https://www.okx.com"
BROKER_TAG = "96ee7de3fd4bBCDE"  # Blave broker attribution — every order POST

_rules_cache = {}       # instId -> instrument rules (per-process)
_pos_mode_cache = {}    # api_key -> "net_mode" | "long_short_mode"


class OKXError(Exception):
    """Raised on any OKX error response or unexpected shape. code/msg carry
    the innermost (sCode/sMsg) detail when present."""

    def __init__(self, code, msg, path=""):
        self.code = code
        self.msg = msg
        super().__init__(f"OKX error {code}: {msg or '(empty msg)'} | {path}")


class OrderNotConfirmed(Exception):
    """Order accepted but not terminal within timeout. It may still fill —
    query again, never blindly resubmit."""


class ProtectionFailed(Exception):
    """Position is OPEN but a protective (SL/TP) order could not be placed or
    verified. The position is NAKED — alert the user immediately."""


# ── transport ────────────────────────────────────────────────────────────────

def _timestamp():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# Order-mutating endpoints and how to classify their intent for the guard.
_MUTATING_PATHS = {
    ("POST", "/api/v5/trade/order"): "place",
    ("POST", "/api/v5/trade/order-algo"): "place",
    ("POST", "/api/v5/trade/cancel-order"): "cancel",
    ("POST", "/api/v5/trade/cancel-algos"): "cancel",
    ("POST", "/api/v5/trade/close-position"): "reduce",
}

_AUDIT_PARAM_KEYS = ("instId", "tdMode", "side", "posSide", "ordType", "sz",
                     "px", "tgtCcy", "reduceOnly", "clOrdId", "algoId",
                     "slTriggerPx", "tpTriggerPx")


def _order_intent(method, path, body):
    """'entry' | 'reduce' | 'protective' | 'cancel' | None. Same S1 rule as
    the other libs: a conditional TYPE alone is not protective — it must
    carry reduce semantics (reduceOnly / hedge opposite-posSide), otherwise
    it is an entry and HALT blocks it."""
    kind = _MUTATING_PATHS.get((method, path))
    if kind != "place":
        return kind
    p = body or {}
    if isinstance(p, list):  # batch bodies — classify by the strictest member
        intents = {_order_intent(method, path, row) for row in p}
        return "entry" if "entry" in intents else (intents.pop() if intents else None)
    is_reduce = str(p.get("reduceOnly", "")).lower() == "true"
    side, pos_side = p.get("side"), p.get("posSide")
    if (pos_side == "long" and side == "sell") or (pos_side == "short" and side == "buy"):
        is_reduce = True
    if p.get("tdMode") == "cash":
        # spot has no positions: BUY builds exposure, SELL liquidates inventory
        return "reduce" if side == "sell" else "entry"
    if path.endswith("order-algo"):
        return "protective" if is_reduce else "entry"
    return "reduce" if is_reduce else "entry"


def _request(method, path, env, body=None, params=None, retries=3):
    """Signed request with guard gate + audit on order-mutating calls, broker
    tag injection, and sCode/sMsg surfacing."""
    intent = _order_intent(method, path, body)
    if intent is not None:
        # broker attribution rides every order-creating POST body — list
        # bodies (batch endpoints) get it per row (audit L3: no batch caller
        # exists yet, but a silent per-row omission would be invisible)
        if isinstance(body, dict) and "tag" not in body:
            body = {**body, "tag": BROKER_TAG}
        elif isinstance(body, list):
            body = [({**row, "tag": BROKER_TAG} if isinstance(row, dict)
                     and "tag" not in row else row) for row in body]
        fields = {k: body[k] for k in _AUDIT_PARAM_KEYS
                  if isinstance(body, dict) and k in body}
        fields["intent"] = intent
        if intent == "entry" and guard.halted():
            guard.audit("order_denied_halt", **fields)
            raise guard.Halted(
                f"state/HALT is set ({guard.halt_info()}) — entry order for "
                f"{fields.get('instId')} refused before reaching the exchange. "
                f"Closes, SL/TP and cancels still work. Only the user may "
                f"clear the halt (guard.clear_halt)."
            )
        guard.audit("order_attempt", **fields)
        try:
            data = _send(method, path, env, body, params, retries)
        except Exception as e:
            guard.audit("order_error", error=str(e), **fields)
            raise
        row = data[0] if isinstance(data, list) and data else {}
        guard.audit("order_ok", order_id=str(row.get("ordId") or row.get("algoId") or ""),
                    **fields)
        return data
    return _send(method, path, env, body, params, retries)


def _send(method, path, env, body=None, params=None, retries=3):
    api_key = env.get("OKX_API_KEY")
    secret = env.get("OKX_SECRET_KEY")
    passphrase = env.get("OKX_PASSPHRASE")
    if not api_key or not secret or not passphrase:
        raise ValueError("OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE missing from .env")

    qs = ""
    if params:
        qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
    body_str = "" if body is None else json.dumps(body)
    request_path = path + qs

    last_err = None
    for attempt in range(retries):
        ts = _timestamp()
        prehash = ts + method.upper() + request_path + body_str
        sig = base64.b64encode(
            hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        headers = {
            "OK-ACCESS-KEY": api_key,
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": passphrase,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        }
        if str(env.get("OKX_DEMO", os.environ.get("OKX_DEMO", ""))).lower() == "true":
            headers["x-simulated-trading"] = "1"
        try:
            if method == "POST":
                r = requests.post(f"{BASE_URL}{request_path}", data=body_str,
                                  headers=headers, timeout=10)
            else:
                r = requests.get(f"{BASE_URL}{request_path}", headers=headers, timeout=10)
        except requests.exceptions.ConnectionError as e:
            last_err = e
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)
            continue
        try:
            data = r.json()
        except ValueError:
            raise OKXError(r.status_code, f"non-JSON response ({r.text[:120]})", path)
        code = str(data.get("code", ""))
        rows = data.get("data") or []
        if code != "0":
            # top-level msg is often "Operation failed" — the truth is in
            # data[].sCode/sMsg (measured: OKX code=1 hides the real reason)
            row = rows[0] if rows else {}
            s_code = row.get("sCode") or code
            s_msg = row.get("sMsg") or data.get("msg") or ""
            raise OKXError(s_code, s_msg, path)
        # code 0 can still carry per-row failures on order endpoints
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("sCode") not in (None, "0", ""):
                raise OKXError(row.get("sCode"), row.get("sMsg") or "", path)
        return rows
    raise last_err


# ── symbols / rules ─────────────────────────────────────────────────────────

def _swap_inst(sym):
    """Canonical dashless (BTCUSDT) → OKX swap instId (BTC-USDT-SWAP)."""
    sym = (sym or "").replace("-", "").upper()
    if sym.endswith("USDT"):
        return f"{sym[:-4]}-USDT-SWAP"
    if sym.endswith("USDC"):
        return f"{sym[:-4]}-USDC-SWAP"
    raise ValueError(f"cannot derive OKX swap instId from {sym!r}")


def _spot_inst(sym):
    """Canonical dashless (BTCUSDT) → OKX spot instId (BTC-USDT)."""
    sym = (sym or "").replace("-", "").upper()
    if sym.endswith("USDT"):
        return f"{sym[:-4]}-USDT"
    if sym.endswith("USDC"):
        return f"{sym[:-4]}-USDC"
    raise ValueError(f"cannot derive OKX spot instId from {sym!r}")


def _instrument(env, inst_id, inst_type):
    if inst_id not in _rules_cache:
        rows = _send("GET", "/api/v5/public/instruments", env,
                     params={"instType": inst_type, "instId": inst_id})
        if not rows:
            raise OKXError("N/A", f"{inst_id} not found in instruments", "instruments")
        r = rows[0]
        _rules_cache[inst_id] = {
            "ct_val": float(r.get("ctVal") or 1),   # base units per contract (swap)
            "lot_sz": str(r.get("lotSz") or "1"),   # sz step (contracts for swap, base for spot)
            "min_sz": float(r.get("minSz") or 0),   # min sz, same unit as lotSz
            "tick_sz": str(r.get("tickSz") or "0.01"),
            "active": r.get("state") == "live",
        }
    return _rules_cache[inst_id]


def get_contract_rules(env, symbol):
    """Cross-venue contract: {'step','min_qty','min_notional','contract_value'}
    with min_qty expressed in BASE units (min_sz × ctVal). step is the sz step
    in CONTRACTS — use format_qty, never do the conversion by hand."""
    r = _instrument(env, _swap_inst(symbol), "SWAP")
    return {
        "step": r["lot_sz"],
        "min_qty": r["min_sz"] * r["ct_val"],
        "min_notional": 0.0,  # OKX swaps gate on min_sz, not notional
        "contract_value": r["ct_val"],
        "price_tick": r["tick_sz"],
        "active": r["active"],
    }


def _floor_to_step(value, step_str):
    return Decimal(str(value)).quantize(Decimal(str(step_str)).normalize(),
                                        rounding=ROUND_DOWN)


def format_qty(env, symbol, qty, price=None):
    """BASE qty → CONTRACTS string floored to lotSz. Raises ValueError below
    minSz or on a suspended instrument. Callers use this ONLY as the min-size
    gate — never feed the return into place_market_order (it would re-convert
    contracts→contracts and oversize N× on ctVal≠1 — the manager.md pitfall)."""
    r = _instrument(env, _swap_inst(symbol), "SWAP")
    if not r["active"]:
        raise ValueError(f"{symbol} is not open for trading")
    # Decimal all the way: float division under-sizes by a whole lot at exact
    # multiples (0.01/0.1 = 0.09999… → floors to 0.09 ct — measured live, the
    # entry filled 0.009 ETH instead of 0.01)
    contracts = _floor_to_step(Decimal(str(qty)) / Decimal(str(r["ct_val"])), r["lot_sz"])
    if float(contracts) < r["min_sz"] or float(contracts) <= 0:
        raise ValueError(
            f"{symbol} qty {qty} = {contracts} contracts, below minimum {r['min_sz']}"
        )
    return format(contracts, "f")


def format_price(env, symbol, price):
    r = _instrument(env, _swap_inst(symbol), "SWAP")
    return format(_floor_to_step(price, r["tick_sz"]), "f")


def format_spot_price(env, symbol, price):
    """Spot twin of format_price — spot instruments carry their own tickSz."""
    r = _instrument(env, _spot_inst(symbol), "SPOT")
    return format(_floor_to_step(price, r["tick_sz"]), "f")


# ── position mode ───────────────────────────────────────────────────────────

def get_position_mode(env):
    """'net_mode' | 'long_short_mode' (hedge). Cached per api key."""
    key = env.get("OKX_API_KEY", "")
    if key not in _pos_mode_cache:
        rows = _send("GET", "/api/v5/account/config", env)
        _pos_mode_cache[key] = (rows[0] if rows else {}).get("posMode", "net_mode")
    return _pos_mode_cache[key]


# ── orders (swap) ───────────────────────────────────────────────────────────

def get_order(env, symbol, ord_id):
    """Swap order by id, normalized. orig_qty / executed_qty are BASE units
    (sz / accFillSz are contracts — converted here; partial-fill accounting
    needs the original size, chase contract). commission from the order's fee
    field (negative on OKX — abs'd), commission_asset = feeCcy."""
    inst = _swap_inst(symbol)
    rows = _send("GET", "/api/v5/trade/order", env,
                 params={"instId": inst, "ordId": str(ord_id)})
    o = rows[0] if rows else {}
    if "state" not in o:
        raise OKXError("N/A", f"order query missing state: {list(o)}", "trade/order")
    ct_val = _instrument(env, inst, "SWAP")["ct_val"]
    filled_ct = float(o.get("accFillSz") or 0)
    return {
        "order_id": str(o.get("ordId", "")),
        "status": o.get("state"),
        "avg_price": float(o.get("avgPx") or 0),
        "orig_qty": float(o.get("sz") or 0) * ct_val,
        "executed_qty": filled_ct * ct_val,
        "commission": abs(float(o.get("fee") or 0)),
        "commission_asset": o.get("feeCcy") or "",
        "client_order_id": o.get("clOrdId") or "",
        "raw": o,
    }


TERMINAL_STATES = {"filled", "canceled", "mmp_canceled"}


def _confirm(env, symbol, ord_id, getter, timeout=15):
    deadline = time.time() + timeout
    order = None
    while time.time() < deadline:
        order = getter(env, symbol, ord_id)
        if order["status"] == "filled":
            return order
        if order["status"] in TERMINAL_STATES:
            raise OKXError("N/A", f"order {ord_id} ended {order['status']}", "confirm")
        time.sleep(1)
    raise OrderNotConfirmed(
        f"order {ord_id} still {order['status'] if order else 'UNKNOWN'} after {timeout}s"
    )


def place_market_order(env, symbol, direction, qty, client_order_id=None,
                       reduce_only=False, confirm_timeout=15):
    """Confirmed swap market order. qty is BASE units (converted to contracts
    internally). direction: 'long'|'short' = the position this order builds
    (when reduce_only, the position it reduces). Below min sz → False
    (intentional skip). Net mode sends native reduceOnly; hedge mode has none
    — closing is expressed by posSide opposite to the order side."""
    inst = _swap_inst(symbol)
    mode = get_position_mode(env)
    try:
        sz = format_qty(env, symbol, qty)
    except ValueError as e:
        if "not open for trading" in str(e):
            raise
        return False
    if reduce_only:
        side = "sell" if direction == "long" else "buy"
    else:
        side = "buy" if direction == "long" else "sell"
    body = {
        "instId": inst,
        "tdMode": "cross",
        "side": side,
        "ordType": "market",
        "sz": sz,
    }
    if mode == "long_short_mode":
        body["posSide"] = direction  # open AND close address the position's side
    elif reduce_only:
        body["reduceOnly"] = "true"
    if client_order_id:
        body["clOrdId"] = client_order_id  # 1-32 alphanumeric
    rows = _request("POST", "/api/v5/trade/order", env, body)
    ord_id = (rows[0] if rows else {}).get("ordId")
    if not ord_id:
        raise OKXError("N/A", f"order response missing ordId: {rows}", "trade/order")
    return _confirm(env, symbol, ord_id, get_order, timeout=confirm_timeout)


# time_in_force → OKX ordType: OKX has no separate TIF field, the order type
# IS the TIF dialect (market/limit/post_only/fok/ioc — skill reference).
_TIF_TO_ORDTYPE = {"GTC": "limit", "IOC": "ioc", "FOK": "fok"}


def _post_only_probe(env, symbol, ord_id, getter):
    """OKX never rejects a crossing post_only order at placement — it is
    ACCEPTED (sCode 0, ordId) and the matching engine auto-cancels it,
    marking cancelSource 31 ("POST_ONLY would take liquidity",
    nautilus_trader-verified; ccxt/gocryptotrader confirm no placement sCode
    exists for this). One probe right after placement turns that into the
    chase contract's {'status': 'post_only_rejected'} — a same-instant cancel
    of a fresh post-only order has no other engine-side cause, and a wrong
    guess only costs the caller one re-post. Probe failure is NOT a placement
    failure: the order exists — return None and report it as resting."""
    try:
        o = getter(env, symbol, ord_id)
    except Exception:
        return None
    if o["status"] == "canceled":
        return {"status": "post_only_rejected", "order_id": str(ord_id),
                "cancel_source": o["raw"].get("cancelSource", ""), "raw": o["raw"]}
    return None


def place_limit_order(env, symbol, direction, qty, price, client_order_id=None,
                      reduce_only=False, time_in_force="GTC", post_only=False):
    """Swap limit order, UNCONFIRMED — it may rest; track via get_order and
    cancel via cancel_order, always by the returned order_id (chase contract).
    qty is BASE units (converted to contracts internally, same as
    place_market_order), price a raw float floored to the instrument tick
    here. post_only=True sends ordType=post_only and probes the accepted
    order once — see _post_only_probe. Below min sz → False (intentional
    skip). Guard classification rides reduceOnly/posSide exactly like the
    market path — ordType is not consulted."""
    inst = _swap_inst(symbol)
    mode = get_position_mode(env)
    try:
        sz = format_qty(env, symbol, qty)
    except ValueError as e:
        if "not open for trading" in str(e):
            raise
        return False
    if reduce_only:
        side = "sell" if direction == "long" else "buy"
    else:
        side = "buy" if direction == "long" else "sell"
    tif = str(time_in_force).upper()
    if not post_only and tif not in _TIF_TO_ORDTYPE:
        raise ValueError(f"unsupported time_in_force {time_in_force!r}")
    body = {
        "instId": inst,
        "tdMode": "cross",
        "side": side,
        "ordType": "post_only" if post_only else _TIF_TO_ORDTYPE[tif],
        "sz": sz,
        "px": format_price(env, symbol, price),
    }
    if mode == "long_short_mode":
        body["posSide"] = direction
    elif reduce_only:
        body["reduceOnly"] = "true"
    if client_order_id:
        body["clOrdId"] = client_order_id
    rows = _request("POST", "/api/v5/trade/order", env, body)
    ord_id = (rows[0] if rows else {}).get("ordId")
    if not ord_id:
        raise OKXError("N/A", f"limit order response missing ordId: {rows}",
                       "trade/order")
    if post_only:
        rejected = _post_only_probe(env, symbol, ord_id, get_order)
        if rejected is not None:
            return rejected
    return {"order_id": str(ord_id), "status": "open",
            "client_order_id": client_order_id or "", "raw": rows[0]}


def get_mark_price(env, symbol):
    """Live swap price (public ticker last) — the cross-venue wiring contract
    for USD→qty conversion (lib/venue_wiring.py)."""
    rows = _send("GET", "/api/v5/market/ticker", env,
                 params={"instId": _swap_inst(symbol)})
    return float((rows[0] if rows else {})["last"])


def _bbo(env, inst, symbol):
    rows = _send("GET", "/api/v5/market/ticker", env, params={"instId": inst})
    t = rows[0] if rows else {}
    bid, ask = t.get("bidPx"), t.get("askPx")
    if not bid or not ask:
        raise OKXError("N/A", f"{symbol} ticker missing top-of-book "
                       f"(bid={bid!r} ask={ask!r})", "market/ticker")
    return {"bid": float(bid), "ask": float(ask)}


def get_bbo(env, symbol):
    """Real top-of-book {'bid','ask'} from the public ticker's bidPx/askPx
    (fields verified: ccxt parseTicker + live response samples) — never
    mark/last price (chase contract). Fails loud on an empty book side."""
    return _bbo(env, _swap_inst(symbol), symbol)


def close_position_partial(env, symbol, direction, qty, client_order_id=None):
    """Cross-venue reconciler contract name — reduce the position by a BASE
    qty, reduce-only semantics, works under HALT."""
    return place_market_order(env, symbol, direction, qty,
                              client_order_id=client_order_id, reduce_only=True)


def _annotate_open_rows(rows):
    """Cross-venue open-order row contract (venue_wiring.sweep_orphan_orders):
    every row carries canonical dashless 'symbol' + 'order_id' +
    'client_order_id' alongside OKX's raw fields — the reconciler sweeps
    orphaned rc*-prefixed orders by these keys after a restart."""
    for o in rows:
        o["symbol"] = (o.get("instId") or "").replace("-SWAP", "").replace("-", "")
        o["order_id"] = str(o.get("ordId", ""))
        o["client_order_id"] = o.get("clOrdId") or ""
    return rows


def get_open_orders(env, symbol=None):
    """Open swap orders (market/limit — conditionals live on the algo
    endpoints), rows annotated per _annotate_open_rows."""
    params = {"instType": "SWAP"}
    if symbol:
        params["instId"] = _swap_inst(symbol)
    return _annotate_open_rows(
        _send("GET", "/api/v5/trade/orders-pending", env, params=params) or [])


# Cancellation-failure sCodes that mean the order is already gone/terminal
# (ccxt + gocryptotrader both verify the meanings): 51400 does not exist,
# 51401 already canceled, 51402 already completed, 51410 already under
# cancelling. 51403 (type does not support cancellation) stays a raise — that
# is a caller bug, not a race.
_CANCEL_GONE_SCODES = {"51400", "51401", "51402", "51410"}


def _cancel(env, inst, order_id, client_order_id):
    body = {"instId": inst}
    if order_id:
        body["ordId"] = str(order_id)
    elif client_order_id:
        body["clOrdId"] = client_order_id
    else:
        raise ValueError("order_id or client_order_id required")
    try:
        return _request("POST", "/api/v5/trade/cancel-order", env, body)
    except OKXError as e:
        if str(e.code) in _CANCEL_GONE_SCODES:
            return {"status": "already_gone", "code": str(e.code),
                    "msg": e.msg, "order_id": str(order_id or client_order_id)}
        raise


def cancel_order(env, symbol, order_id=None, client_order_id=None):
    """Cancel one swap order by ordId (or clOrdId). Idempotent-safe (chase
    contract): an order that is already filled / canceled / unknown RETURNS
    {'status': 'already_gone', ...} instead of raising — the caller always
    re-reads fills afterwards. Other refusals raise."""
    return _cancel(env, _swap_inst(symbol), order_id, client_order_id)


# ── protective orders (swap) ────────────────────────────────────────────────

def get_open_algo_orders(env, symbol=None):
    params = {"ordType": "conditional"}
    if symbol:
        params["instId"] = _swap_inst(symbol)
    return _send("GET", "/api/v5/trade/orders-algo-pending", env, params=params)


def cancel_algo_order(env, symbol, algo_id):
    return _request("POST", "/api/v5/trade/cancel-algos", env,
                    [{"instId": _swap_inst(symbol), "algoId": str(algo_id)}])


def place_protective_orders(env, symbol, direction, qty=None, sl_price=None,
                            tp_price=None, verify=True):
    """Reduce-only conditional SL/TP for an EXISTING swap position via
    /api/v5/trade/order-algo (ordType=conditional, market execution via
    ordPx=-1). qty=None protects the whole position (closeFraction=1).
    Verified in orders-algo-pending; raises ProtectionFailed otherwise."""
    inst = _swap_inst(symbol)
    mode = get_position_mode(env)
    close_side = "sell" if direction == "long" else "buy"
    placed = {}
    try:
        for kind, trig_key, px_key, trigger in (
            ("sl", "slTriggerPx", "slOrdPx", sl_price),
            ("tp", "tpTriggerPx", "tpOrdPx", tp_price),
        ):
            if trigger is None:
                continue
            body = {
                "instId": inst,
                "tdMode": "cross",
                "side": close_side,
                "ordType": "conditional",
                trig_key: format_price(env, symbol, trigger),
                px_key: "-1",  # market execution on trigger
            }
            if mode == "long_short_mode":
                body["posSide"] = direction
            else:
                body["reduceOnly"] = "true"
            if qty is None:
                body["closeFraction"] = "1"
            else:
                body["sz"] = format_qty(env, symbol, qty)
            rows = _request("POST", "/api/v5/trade/order-algo", env, body)
            algo_id = (rows[0] if rows else {}).get("algoId")
            if not algo_id:
                raise OKXError("N/A", f"{kind} response missing algoId: {rows}",
                               "order-algo")
            placed[kind] = {"algoId": str(algo_id), "raw": rows[0]}
    except Exception as e:
        raise ProtectionFailed(
            f"{symbol} {direction} position may be NAKED — placing protective orders "
            f"failed after {list(placed)} succeeded: {e}"
        ) from e

    if verify and placed:
        open_ids = {str(o.get("algoId")) for o in get_open_algo_orders(env, symbol)}
        missing = [k for k, o in placed.items() if o["algoId"] not in open_ids]
        if missing:
            raise ProtectionFailed(
                f"{symbol} {direction}: protective order(s) {missing} accepted but "
                f"NOT in pending algo orders — verify manually, position may be naked"
            )
    return placed


def open_position(env, symbol, direction, qty, sl_price=None, tp_price=None,
                  client_order_id=None, confirm_timeout=15):
    """Confirmed market entry, then protective orders (OKX's attachAlgoOrds
    exists but is deliberately not used yet — unverified; the two-step flow
    matches order_binance and raises ProtectionFailed on a naked position)."""
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


# ── spot execution (MARKET="spot" strategies) ───────────────────────────────

def get_spot_rules(env, symbol):
    """{'step','min_qty','min_notional','active'} — spot sz is BASE units;
    min buy notional on OKX spot is expressed by minSz at market price, there
    is no separate notional filter (min_notional kept 0 for the contract)."""
    r = _instrument(env, _spot_inst(symbol), "SPOT")
    return {"step": r["lot_sz"], "min_qty": r["min_sz"],
            "min_notional": 0.0, "active": r["active"]}


def format_spot_qty(env, symbol, qty):
    r = get_spot_rules(env, symbol)
    if not r["active"]:
        raise ValueError(f"{symbol} is not open for spot trading")
    q = _floor_to_step(qty, r["step"])
    if float(q) < r["min_qty"] or float(q) <= 0:
        raise ValueError(
            f"{symbol} spot qty {qty} floors to {q}, below minimum {r['min_qty']}"
        )
    return format(q, "f")


def get_spot_order(env, symbol, ord_id):
    """Spot order by id — accFillSz is BASE units on spot (no ctVal).
    orig_qty is the order's sz: BASE units for limit orders and market sells
    (the chase consumers); for a market BUY sized in quote (tgtCcy=quote_ccy)
    sz — and therefore orig_qty — is QUOTE units."""
    rows = _send("GET", "/api/v5/trade/order", env,
                 params={"instId": _spot_inst(symbol), "ordId": str(ord_id)})
    o = rows[0] if rows else {}
    if "state" not in o:
        raise OKXError("N/A", f"spot order query missing state: {list(o)}", "trade/order")
    executed = float(o.get("accFillSz") or 0)
    avg = float(o.get("avgPx") or 0)
    return {
        "order_id": str(o.get("ordId", "")),
        "status": o.get("state"),
        "avg_price": avg,
        "orig_qty": float(o.get("sz") or 0),
        "executed_qty": executed,
        "quote_qty": executed * avg,
        "commission": abs(float(o.get("fee") or 0)),
        "commission_asset": o.get("feeCcy") or "",
        "client_order_id": o.get("clOrdId") or "",
        "raw": o,
    }


def place_spot_market_order(env, symbol, side, base_qty=None, quote_qty=None,
                            client_order_id=None, confirm_timeout=15):
    """Confirmed spot market order. side: 'buy'|'sell'. BUYs size in QUOTE
    currency (tgtCcy=quote_ccy, sz=USDT amount — no price conversion on our
    side); SELLs in base qty floored to the spot step. Below min → False.
    Guard: buy=entry (halt blocks), sell=reduce (never trapped) — classified
    off tdMode=cash in _order_intent."""
    inst = _spot_inst(symbol)
    side = side.lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy|sell, got {side!r}")
    body = {"instId": inst, "tdMode": "cash", "side": side, "ordType": "market"}
    if side == "buy":
        if quote_qty is None:
            raise ValueError("spot buys are sized in quote currency — pass quote_qty")
        body["tgtCcy"] = "quote_ccy"
        body["sz"] = f"{float(quote_qty):.2f}"
    else:
        if base_qty is None:
            raise ValueError("spot sells are sized in base currency — pass base_qty")
        body["tgtCcy"] = "base_ccy"
        try:
            body["sz"] = format_spot_qty(env, symbol, base_qty)
        except ValueError as e:
            if "not open for spot trading" in str(e):
                raise
            return False
    if client_order_id:
        body["clOrdId"] = client_order_id
    try:
        rows = _request("POST", "/api/v5/trade/order", env, body)
    except OKXError as e:
        # 51020 = order amount below the minimum — the contract's skip
        if str(e.code) == "51020":
            return False
        raise
    ord_id = (rows[0] if rows else {}).get("ordId")
    if not ord_id:
        raise OKXError("N/A", f"spot order response missing ordId: {rows}", "trade/order")
    return _confirm(env, symbol, ord_id, get_spot_order, timeout=confirm_timeout)


def place_spot_limit_order(env, symbol, side, base_qty, price, client_order_id=None,
                           post_only=False):
    """Spot limit order, UNCONFIRMED — it may rest; track via get_spot_order,
    cancel via cancel_spot_order (chase contract). sz is BASE units on BOTH
    sides for limit orders — tgtCcy only applies to spot MARKET orders, the
    quote-sized market-buy convention does NOT carry over (OKX docs +
    ccxt-verified: ccxt only branches on tgtCcy for market buys). price a raw
    float floored to the SPOT tick here. post_only=True sends
    ordType=post_only and probes the accepted order once — see
    _post_only_probe. Below min sz → False (intentional skip).
    Guard: buy=entry (halt blocks), sell=reduce — off tdMode=cash."""
    inst = _spot_inst(symbol)
    side = side.lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy|sell, got {side!r}")
    try:
        sz = format_spot_qty(env, symbol, base_qty)
    except ValueError as e:
        if "not open for spot trading" in str(e):
            raise
        return False
    body = {
        "instId": inst,
        "tdMode": "cash",
        "side": side,
        "ordType": "post_only" if post_only else "limit",
        "sz": sz,
        "px": format_spot_price(env, symbol, price),
    }
    if client_order_id:
        body["clOrdId"] = client_order_id
    try:
        rows = _request("POST", "/api/v5/trade/order", env, body)
    except OKXError as e:
        # 51020 = order amount below the minimum — the contract's skip
        if str(e.code) == "51020":
            return False
        raise
    ord_id = (rows[0] if rows else {}).get("ordId")
    if not ord_id:
        raise OKXError("N/A", f"spot limit order response missing ordId: {rows}",
                       "trade/order")
    if post_only:
        rejected = _post_only_probe(env, symbol, ord_id, get_spot_order)
        if rejected is not None:
            return rejected
    return {"order_id": str(ord_id), "status": "open",
            "client_order_id": client_order_id or "", "raw": rows[0]}


def cancel_spot_order(env, symbol, order_id=None, client_order_id=None):
    """Spot twin of cancel_order — same idempotent-safe contract
    (already filled / canceled / unknown → {'status': 'already_gone'})."""
    return _cancel(env, _spot_inst(symbol), order_id, client_order_id)


def get_spot_bbo(env, symbol):
    """Spot twin of get_bbo — real top-of-book from the spot ticker's
    bidPx/askPx, never last price."""
    return _bbo(env, _spot_inst(symbol), symbol)


def get_spot_open_orders(env, symbol=None):
    """Open spot orders, rows annotated per _annotate_open_rows (spot instIds
    have no -SWAP suffix — the same strip is a no-op)."""
    params = {"instType": "SPOT"}
    if symbol:
        params["instId"] = _spot_inst(symbol)
    return _annotate_open_rows(
        _send("GET", "/api/v5/trade/orders-pending", env, params=params) or [])


def get_spot_balances(env):
    """{asset: available+frozen} in the TRADING account (OKX is unified —
    spot coins live there, not in a separate spot wallet)."""
    rows = _send("GET", "/api/v5/account/balance", env)
    out = {}
    for acct in rows:
        for d in acct.get("details", []):
            amt = float(d.get("eq", 0) or 0)
            if amt > 0:
                out[d.get("ccy")] = amt
    return out


def get_spot_price(env, symbol):
    rows = _send("GET", "/api/v5/market/ticker", env,
                 params={"instId": _spot_inst(symbol)})
    return float((rows[0] if rows else {})["last"])
