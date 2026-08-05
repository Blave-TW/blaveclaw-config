"""
BingX account library — swap/futures equity & positions.

Discovered by filename (lib.account_bingx.get_equity / get_positions).
Credentials: BINGX_API_KEY, BINGX_SECRET_KEY in .env.
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


def _wallet_value(data, list_key, marks):
    """USDT value of ALL assets in a spot/fund balance payload — USDT/USDC
    direct, others via the perp mark map (2026-08-04: the old USDT-only sum
    made the breakdown row disagree with get_holdings by exactly the coins'
    value — measured $35.93 shown vs $80.5 held). Unpriceable assets
    contribute 0 here; the per-coin truth (incl. None-valued rows) lives in
    get_holdings."""
    total = 0.0
    for r in (data or {}).get(list_key) or []:
        amt = float(r.get("free", 0)) + float(r.get("locked", 0))
        if amt <= 0:
            continue
        asset = r.get("asset")
        if asset in ("USDT", "USDC"):
            total += amt
        else:
            px = marks.get(f"{asset}-USDT")
            if px:
                total += amt * px
    return round(total, 2)


def get_equity(env: dict) -> dict:
    """Swap/futures account equity, plus a per-account breakdown.

    Returns {'equity': float, 'currency': str, 'accounts': {name: float}}.
    `equity` is the SWAP account only — that is what the reconciler trades
    with, so it is the position-sizing base. `accounts` is display-only:
    BingX keeps fund/spot/swap as separate wallets with no auto-transfer,
    and money sitting outside swap would otherwise look like it vanished.
    (Deliberately NOT /openApi/account/v1/allAccountBalance — measured on a
    live account it omits the fund wallet and reported 0 for a spot wallet
    holding >100 USDT.)

    /openApi/swap/v3/user/balance returns `data` as an array of per-asset
    balance rows (BingX swap supports USDT- and USDC-margined accounts) —
    not a single nested object. Prefers the USDT row; falls back to the
    first row if the account holds no USDT.
    """
    rows = _signed_get("/openApi/swap/v3/user/balance", env) or []
    row = next((r for r in rows if r.get("asset") == "USDT"), rows[0] if rows else {})
    equity = float(row.get("equity", 0))

    accounts = {"swap": equity}
    # Best-effort: the breakdown failing must not take the main equity down.
    marks = {}
    try:
        marks = {m["symbol"]: float(m["markPrice"])
                 for m in (_public_get("/openApi/swap/v2/quote/premiumIndex") or [])}
    except Exception:
        pass
    try:
        accounts["spot"] = _wallet_value(
            _signed_get("/openApi/spot/v1/account/balance", env), "balances", marks
        )
    except Exception:
        pass
    try:
        accounts["fund"] = _wallet_value(
            _signed_get("/openApi/fund/v1/account/balance", env), "assets", marks
        )
    except Exception:
        pass
    # Wallets with no dedicated endpoint (standard futures / coin-M / copy
    # trading) come from the allAccountBalance overview — measured accurate
    # for these types (its spot row is NOT; never use it for spot/fund).
    # ALWAYS listed, zeros included: the complete wallet map is itself the
    # information — a user who can see 標準合約 $0 in the list won't be
    # surprised when a transfer lands there (Wei 2026-08-04).
    try:
        rows2 = _signed_get("/openApi/account/v1/allAccountBalance", env) or []
        extra = {"stdFutures": "std_futures", "coinMPerp": "coinm_perp",
                 "copyTrading": "copy_trading"}
        for r2 in rows2:
            key = extra.get(r2.get("accountType"))
            if key:
                accounts[key] = float(r2.get("usdtBalance", 0) or 0)
    except Exception:
        pass
    return {"equity": equity, "currency": row.get("asset", "USDT"), "accounts": accounts}


def get_holdings(env: dict) -> list:
    """Coin holdings — DISPLAY ONLY, never fed to the reconciler.
    Returns [{'asset', 'amount', 'usdt_value', 'wallet'}, ...] sorted by
    value desc; wallet ∈ {'spot','fund','swap'}. Valuation uses the public
    perp premiumIndex map (shape already field-verified in get_positions);
    assets with no ASSET-USDT perp keep usdt_value=None but are still listed.
    Dust under 0.1 USDT dropped; unpriceable rows kept. Swap rows use the
    per-asset `equity` field (margin balance incl. unrealized PnL — same
    basis as the wallet-breakdown swap row and the sizing equity)."""
    # fail-loud per section (audit M2): a partial list reads as "money
    # vanished" — account_reader is the single best-effort layer, not here
    out = []
    for b in _signed_get("/openApi/swap/v3/user/balance", env) or []:
        amt = float(b.get("equity") or 0)
        if amt != 0:
            out.append({"asset": b.get("asset"), "amount": amt, "wallet": "swap"})
    for b in (_signed_get("/openApi/spot/v1/account/balance", env) or {}).get("balances") or []:
        amt = float(b.get("free") or 0) + float(b.get("locked") or 0)
        if amt > 0:
            out.append({"asset": b.get("asset"), "amount": amt, "wallet": "spot"})
    for b in (_signed_get("/openApi/fund/v1/account/balance", env) or {}).get("assets") or []:
        amt = float(b.get("free") or 0) + float(b.get("locked") or 0)
        if amt > 0:
            out.append({"asset": b.get("asset"), "amount": amt, "wallet": "fund"})
    if not out:
        return []

    marks = {}
    try:
        marks = {m["symbol"]: float(m["markPrice"])
                 for m in (_public_get("/openApi/swap/v2/quote/premiumIndex") or [])}
    except Exception:
        pass

    for h in out:
        if h["asset"] in ("USDT", "USDC"):  # USDC ≈1 — display-grade valuation
            h["usdt_value"] = round(h["amount"], 2)
        else:
            p = marks.get(f"{h['asset']}-USDT")
            h["usdt_value"] = round(h["amount"] * p, 2) if p else None
    # abs(): a negative margin row (multi-asset mode) is information, not dust
    out = [h for h in out if h["usdt_value"] is None or abs(h["usdt_value"]) >= 0.1]
    out.sort(key=lambda h: -(h["usdt_value"] or 0))
    return out


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

    def _side(p):
        s = (p.get("positionSide") or "").lower()
        if s in ("long", "short"):
            return s
        # one-way accounts report positionSide "BOTH" — direction is the sign
        # of positionAmt. Without this, callers see side="both": flatten skips
        # the row and the reconciler treats actual as 0 → doubles the position.
        return "short" if float(p.get("positionAmt", 0)) < 0 else "long"

    return [{
        # canonical dashless uppercase (BingX reports 'BTC-USDT') — the
        # reconciler keys target/actual on this format
        "symbol": (p.get("symbol") or "").replace("-", "").upper(),
        "side": _side(p),
        "size": abs(float(p.get("positionAmt", 0))),
        "mark_price": _mark_price(p),
    } for p in rows]
