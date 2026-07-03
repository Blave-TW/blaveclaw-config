"""
BingX account library — swap/futures equity & positions.

Auto-discovered by manager/snapshot.py (lib.account_bingx.get_equity /
get_positions). Credentials: BINGX_API_KEY, BINGX_SECRET_KEY in .env.
X-SOURCE-KEY: BX-AI-SKILL broker attribution is mandatory on every request —
see skills/blave-quant/references/bingx-skill.md.

Covers the SWAP (perp/futures) account only — BingX keeps fund/spot/swap as
separate accounts with no auto-transfer, so a spot-only user's balance won't
show up here.
"""

import hashlib
import hmac
import time

import requests

BASE_URL = "https://open-api.bingx.com"
FALLBACK = "https://open-api.bingx.pro"


def _sign(secret_key, params):
    params = dict(params)
    params["timestamp"] = str(int(time.time() * 1000))
    canonical = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig = hmac.new(secret_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return canonical + f"&signature={sig}"


def _call(path_and_query, headers):
    for base in (BASE_URL, FALLBACK):
        try:
            r = requests.get(f"{base}{path_and_query}", headers=headers, timeout=10)
            data = r.json()
            if data.get("code") != 0:
                raise Exception(f"BingX error {data.get('code')}: {data.get('msg')}")
            return data.get("data")
        except requests.exceptions.ConnectionError:
            if base == FALLBACK:
                raise
    return None


def _signed_get(path, env, params=None):
    api_key = env.get("BINGX_API_KEY")
    secret_key = env.get("BINGX_SECRET_KEY")
    if not api_key or not secret_key:
        raise ValueError("BINGX_API_KEY / BINGX_SECRET_KEY missing from .env")

    headers = {"X-BX-APIKEY": api_key, "X-SOURCE-KEY": "BX-AI-SKILL"}
    qs = _sign(secret_key, params or {})
    return _call(f"{path}?{qs}", headers)


def _public_get(path, params=None):
    qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    return _call(f"{path}?{qs}" if qs else path, {"X-SOURCE-KEY": "BX-AI-SKILL"})


def get_equity(env: dict) -> dict:
    """Swap/futures account equity. Returns {'equity': float, 'currency': str}.

    /openApi/swap/v3/user/balance returns `data` as an array of per-asset
    balance rows (BingX swap supports USDT- and USDC-margined accounts) —
    not a single nested object. Prefers the USDT row; falls back to the
    first row if the account holds no USDT.
    """
    rows = _signed_get("/openApi/swap/v3/user/balance", env) or []
    row = next((r for r in rows if r.get("asset") == "USDT"), rows[0] if rows else {})
    return {"equity": float(row.get("equity", 0)), "currency": row.get("asset", "USDT")}


def get_positions(env: dict) -> list:
    """Open swap positions. Returns [{'symbol', 'side', 'size', 'mark_price'}, ...], [] if flat.

    /openApi/swap/v2/user/positions usually includes markPrice directly (seen
    in production responses, though BingX's own field docs omit it). If it's
    ever missing for a row, fall back to the public premiumIndex endpoint
    (one call for all symbols), then to avgPrice (entry price) as a last
    resort — only fetched when actually needed.
    """
    rows = _signed_get("/openApi/swap/v2/user/positions", env) or []
    rows = [p for p in rows if float(p.get("positionAmt", 0)) != 0]
    if not rows:
        return []

    mark_prices = {}
    if any(p.get("markPrice") is None for p in rows):
        mark_prices = {
            m["symbol"]: float(m["markPrice"])
            for m in (_public_get("/openApi/swap/v2/quote/premiumIndex") or [])
        }

    def _mark_price(p):
        if p.get("markPrice") is not None:
            return float(p["markPrice"])
        return mark_prices.get(p.get("symbol"), float(p.get("avgPrice", 0)))

    return [{
        "symbol": p.get("symbol"),
        "side": p.get("positionSide", "").lower(),
        "size": abs(float(p.get("positionAmt", 0))),
        "mark_price": _mark_price(p),
    } for p in rows]
