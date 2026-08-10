"""
OKX account library — swap (perpetual futures) equity & positions.

Discovered by filename (lib.account_okx.get_equity / get_positions).
Credentials: OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE in .env.

OKX is unified-account: the trading account (spot + derivatives) is the
equity base; the separate funding wallet appears in the 'accounts' breakdown.
"""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

BASE_URL = "https://www.okx.com"


def _timestamp():
    """ISO 8601 ms UTC: 2024-01-01T00:00:00.000Z"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sign(secret, ts, method, path, body=""):
    prehash = ts + method.upper() + path + body
    return base64.b64encode(
        hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()


def _request(env, method, path, body=None, timeout=10):
    api_key = env.get("OKX_API_KEY")
    secret = env.get("OKX_SECRET_KEY")
    passphrase = env.get("OKX_PASSPHRASE")
    if not api_key or not secret or not passphrase:
        raise ValueError("OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE missing from .env")

    ts = _timestamp()
    body_str = ""
    if body is not None:
        body_str = body if isinstance(body, str) else json.dumps(body)

    sig = _sign(secret, ts, method, path, body_str)

    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "User-Agent": "Mozilla/5.0",
    }
    # OKX demo trading (paired with demo-mode keys) — same flag as order_okx.py
    if str(env.get("OKX_DEMO", "")).lower() == "true":
        headers["x-simulated-trading"] = "1"

    if method == "POST":
        headers["Content-Type"] = "application/json"
        r = requests.post(f"{BASE_URL}{path}", data=body_str, headers=headers, timeout=timeout)
    else:
        r = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=timeout)

    data = r.json()
    code = data.get("code", "")
    if code != "0":
        msg = data.get("msg", "")
        raise Exception(f"OKX error {code}: {msg} | {method} {path}")
    return data.get("data", [])


def get_equity(env: dict) -> dict:
    """Trading-account equity plus per-wallet breakdown.

    Returns {'equity': float, 'currency': str, 'accounts': {name: float}}.
    OKX is a unified-account venue: /api/v5/account/balance is the TRADING
    account (spot and derivatives share it; `totalEq` is USD-denominated),
    and /api/v5/asset/balances is the separate FUNDING wallet (USDT summed
    here). `equity` = the trading account — the wallet orders draw on.
    """
    rows = _request(env, "GET", "/api/v5/account/balance")
    trading = 0.0
    for acct in rows:
        trading += float(acct.get("totalEq", 0) or 0)

    accounts = {"trading": trading}
    # Best-effort: the funding breakdown failing must not take equity down.
    try:
        fund_rows = _request(env, "GET", "/api/v5/asset/balances")
        accounts["funding"] = sum(
            float(r.get("bal", 0) or 0) for r in fund_rows if r.get("ccy") == "USDT"
        )
    except Exception:
        pass

    # totalEq is USD-denominated (not USDT) — near-parity but label honestly
    return {"equity": trading, "currency": "USD", "accounts": accounts}


def get_holdings(env: dict) -> list:
    """Coin holdings — DISPLAY ONLY, never fed to the reconciler.

    OKX is unified-account: spot coins live inside the TRADING account
    (/api/v5/account/balance data[0].details[], per-asset `eq` with `eqUsd`
    already USD-valued — the same response get_equity reads); the separate
    FUNDING wallet (/api/v5/asset/balances) is valued via ONE bulk spot
    tickers call (per-asset lookups on a dusty wallet can eat the reader's
    60s alarm — audit M4). Returns [{'asset', 'amount', 'usdt_value',
    'wallet'}, ...] sorted by value desc; wallet ∈ {'trading','funding'}.
    Dust under 0.1 USD dropped — abs(), negative rows (borrow / multi-asset
    margin) are information, not noise. Unpriceable rows kept with
    usdt_value=None. Fail-loud per section: account_reader is the single
    best-effort layer, a partial list reads as "money vanished"."""
    out = []
    for acct in _request(env, "GET", "/api/v5/account/balance"):
        for d in acct.get("details", []):
            amt = float(d.get("eq", 0) or 0)
            if amt == 0:
                continue
            usd = d.get("eqUsd")
            out.append({"asset": d.get("ccy"), "amount": amt,
                        "usdt_value": round(float(usd), 2) if usd else None,
                        "wallet": "trading"})
    fund_rows = []
    for r in _request(env, "GET", "/api/v5/asset/balances"):
        amt = float(r.get("bal", 0) or 0)
        if amt == 0:
            continue
        row = {"asset": r.get("ccy"), "amount": amt, "usdt_value": None,
               "wallet": "funding"}
        fund_rows.append(row)
        out.append(row)
    if fund_rows:
        last = {}
        try:  # valuation only — a tickers hiccup degrades to usdt_value=None
            last = {r.get("instId"): float(r.get("last") or 0)
                    for r in _request(env, "GET", "/api/v5/market/tickers?instType=SPOT")}
        except Exception:
            pass
        for h in fund_rows:
            if h["asset"] == "USDT":
                h["usdt_value"] = round(h["amount"], 2)
            else:
                px = last.get(f"{h['asset']}-USDT")
                h["usdt_value"] = round(h["amount"] * px, 2) if px else None
    out = [h for h in out if h["usdt_value"] is None or abs(h["usdt_value"]) >= 0.1]
    out.sort(key=lambda h: -(h["usdt_value"] or 0))
    return out


def _flow_rows(env, path, id_key, since_ms):
    """All completed on-chain rows of an asset-history endpoint since since_ms.

    Server-side filters: type=4 (on-chain only — 3 would be internal transfer,
    both endpoints document the same request param) and state=2 (deposit
    successful / withdrawal success). Cursor pagination: `before` pins the
    lower ts bound (records NEWER than it — ccxt's executed reading; the doc
    example's from/to comment has them backwards), `after` walks older pages.
    Pages are 100 rows max; `after` is set to oldest_ts+1 so equal-ts rows on
    a page boundary are re-fetched instead of dropped, deduped via id."""
    seen, rows, after = set(), [], None
    for _ in range(60):
        params = {"type": "4", "state": "2",
                  "before": str(max(since_ms - 1, 0)), "limit": "100"}
        if after is not None:
            params["after"] = str(after)
        page = _request(env, "GET", f"{path}?{urlencode(params)}") or []
        new = 0
        for r in page:
            rid = r.get(id_key) or ""
            if rid in seen:
                continue
            seen.add(rid)
            new += 1
            rows.append(r)
        if len(page) < 100:
            return rows
        if new == 0:
            raise Exception(f"OKX flows: pagination stuck | {path}")
        after = min(int(r.get("ts") or 0) for r in page) + 1
    raise Exception(f"OKX flows: >6k records, pagination did not finish | {path}")


def get_flows(env: dict, since: int) -> list:
    """External (on-chain) deposit/withdraw records since `since` (epoch secs).

    Returns [{'ts', 'direction' ('in'/'out'), 'currency', 'amount', 'txid'}, ...]
    ascending by ts; amounts always positive. COMPLETED records only (state=2
    on both endpoints). Internal transfers (user-to-user / sub-account, dest=3)
    are excluded server-side via type=4 — only real external flows count for
    netting equity-snapshot PnL. No documented query-window cap on these
    endpoints; pagination is cursor-based (see _flow_rows). `txid` is the chain
    tx hash, falling back to OKX's depId/wdId. Withdrawal `amount` excludes the
    withdrawal fee (fee/feeCcy are separate fields), so fee-sized residue lands
    in PnL as a cost.
    """
    since_ms = int(since) * 1000
    flows = []
    for path, id_key, direction in (
            ("/api/v5/asset/deposit-history", "depId", "in"),
            ("/api/v5/asset/withdrawal-history", "wdId", "out")):
        for r in _flow_rows(env, path, id_key, since_ms):
            ts = int(r.get("ts") or 0)
            amt = float(r.get("amt") or 0)
            if ts < since_ms or amt <= 0:
                continue
            flows.append({
                "ts": ts // 1000,
                "direction": direction,
                "currency": r.get("ccy") or "",
                "amount": amt,
                "txid": r.get("txId") or str(r.get(id_key) or ""),
            })
    flows.sort(key=lambda f: f["ts"])
    return flows


_ctval_cache = {}  # instId -> float ctVal (per-process)


def _ct_val(env, inst_id):
    """ctVal (base units per contract) from the public instruments endpoint,
    cached. None on any failure — callers fall back to notional/markPx."""
    if inst_id not in _ctval_cache:
        try:
            rows = _request(env, "GET",
                            f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}")
            _ctval_cache[inst_id] = float(rows[0]["ctVal"]) if rows else None
        except Exception:
            return None
    return _ctval_cache[inst_id]


def get_positions(env: dict) -> list:
    """Open swap positions.

    Returns [{'symbol': str, 'side': str, 'size': float, 'mark_price': float}, ...]
    Returns [] if flat.

    'size' is base-currency units, ctVal-exact (pos × ctVal via the public
    instruments lookup; notionalUsd / markPx only as fallback — see the inline
    comment), so the reconciler's `size * mark_price = USDT` formula works
    regardless of the instrument's ctVal. Symbols are CANONICAL (dashless
    uppercase, 'BTCUSDT') — the reconciler contract; venue formats never leak.
    """
    rows = _request(env, "GET", "/api/v5/account/positions?instType=SWAP")
    if not rows:
        return []

    positions = []
    for p in rows:
        pos = float(p.get("pos", 0))
        if pos == 0:
            continue
        inst_id = p.get("instId", "")
        pos_side = p.get("posSide", "")
        notional = float(p.get("notionalUsd", 0))
        mark_px = float(p.get("markPx", 0))

        if mark_px <= 0:
            continue

        # Determine direction from posSide
        if pos_side == "short":
            side = "short"
        elif pos_side == "long":
            side = "long"
        else:
            # net mode — pos sign determines direction
            side = "short" if pos < 0 else "long"

        # canonical dashless uppercase — reconciler keys on this
        symbol = inst_id[:-5] if inst_id.endswith("-SWAP") else inst_id
        symbol = symbol.replace("-", "").upper()

        # size in base units: pos(contracts) × ctVal (EXACT — notionalUsd/markPx
        # loses precision and under-sizes closes: a 0.001 ETH position read
        # back 0.00099911, floored to 0 contracts, unclosable dust — measured
        # 2026-08-05). The positions response does NOT carry ctVal (verified
        # live: field present but null) — it comes from the public instruments
        # endpoint, cached per instId; lookup failure falls back to the
        # notional derivation.
        ct_val = _ct_val(env, inst_id)
        size = abs(pos) * ct_val if ct_val else notional / mark_px
        positions.append({
            "symbol": symbol,
            "side": side,
            "size": size,
            "mark_price": mark_px,
        })

    return positions
