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


def get_positions(env: dict) -> list:
    """Open swap positions.

    Returns [{'symbol': str, 'side': str, 'size': float, 'mark_price': float}, ...]
    Returns [] if flat.

    'size' is returned in base-currency units (notionalUsd / markPx), so the
    reconciler's standard `size * mark_price = USDT` formula works correctly
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

        # size in base-currency units = notionalUsd / markPx
        # (reconciler does size * mark_price → USDT)
        positions.append({
            "symbol": symbol,
            "side": side,
            "size": notional / mark_px,  # base currency units (e.g., BTC)
            "mark_price": mark_px,
        })

    return positions
