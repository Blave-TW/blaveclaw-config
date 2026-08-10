"""
Binance account library — USDⓈ-M futures equity & positions.

Discovered by filename (lib.account_binance.get_equity / get_positions).
Credentials: BINANCE_API_KEY, BINANCE_SECRET_KEY in .env.
Broker attribution on Binance is per-ORDER (newClientOrderId prefix), not a
header — see lib/order_binance.py and skills/blave-quant/references/binance-skill.md.

`equity` is the USDⓈ-M futures account only — that is what the reconciler
trades with, so it is the position-sizing base. Binance splits funds across ten
wallets with no auto-transfer, so `accounts` lists every one of them (zeros
included): a wallet the user can see in the list is a wallet whose money never
looks lost.
"""

import calendar
import hashlib
import hmac
import time
from urllib.parse import urlencode

import requests

SPOT_URL = "https://api.binance.com"
FAPI_URL = "https://fapi.binance.com"

# walletName (from /sapi/v1/asset/wallet/balance) -> breakdown key. Keys the
# web already has labels for are reused (spot / funding / futures / coinm_perp
# / copy_trading); the rest fall back to showing the key itself.
_WALLET_KEYS = {
    "Spot": "spot",
    "Funding": "funding",
    "Cross Margin": "cross_margin",
    "Isolated Margin": "isolated_margin",
    "USDⓈ-M Futures": "futures",
    "COIN-M Futures": "coinm_perp",
    "Earn": "earn",
    "Options": "options",
    "Trading Bots": "trading_bots",
    "Copy Trading": "copy_trading",
}

_time_offset = {"ms": 0}  # server-clock correction, set on -1021


def _signed(method, base, path, env, params=None):
    api_key = env.get("BINANCE_API_KEY")
    secret_key = env.get("BINANCE_SECRET_KEY")
    if not api_key or not secret_key:
        raise ValueError("BINANCE_API_KEY / BINANCE_SECRET_KEY missing from .env")

    for attempt in range(2):
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000) + _time_offset["ms"]
        p["recvWindow"] = 5000
        qs = urlencode(p)
        sig = hmac.new(secret_key.encode(), qs.encode(), hashlib.sha256).hexdigest()
        r = requests.request(method, f"{base}{path}?{qs}&signature={sig}",
                             headers={"X-MBX-APIKEY": api_key}, timeout=15)
        try:
            data = r.json()
        except ValueError:
            # Binance serves an HTML error page for a wrong host/path — say so
            # instead of dying on 'str' object has no attribute 'get'
            raise Exception(f"Binance HTTP {r.status_code} on {path}: non-JSON response")
        # Errors are {"code": -2015, "msg": "..."} — surface the exchange's own
        # code and message. "-2015 Invalid API-key, IP, or permissions" is the
        # difference between "whitelist the machine IP" and "my money is gone".
        if isinstance(data, dict) and data.get("code") is not None and "msg" in data:
            if data["code"] == -1021 and attempt == 0:
                _sync_time(base)
                continue
            raise Exception(f"Binance error {data['code']}: {data['msg']} | {path}")
        return data
    raise Exception(f"Binance error -1021 persisted after clock resync | {path}")


def _sync_time(base):
    """-1021 = our clock drifted outside recvWindow. Correct against the
    venue's clock instead of failing the whole read on machine drift."""
    path = "/fapi/v1/time" if base == FAPI_URL else "/api/v3/time"
    server = requests.get(f"{base}{path}", timeout=10).json()["serverTime"]
    _time_offset["ms"] = int(server) - int(time.time() * 1000)


def _wallets_usdt(env):
    """Every wallet Binance's transfer menu offers, USDT-valued.

    /sapi/v1/asset/wallet/balance is the only endpoint that enumerates ALL of
    them (including Earn / Options / Trading Bots / Copy Trading, which have no
    per-wallet balance endpoint) — it reports in BTC, so convert with the
    public BTCUSDT price. Measured 2026-08-04 against a live account: Spot,
    Funding and Cross Margin match their dedicated endpoints, and the USDⓈ-M
    row decomposes exactly into margin balance + the BNB sitting in the futures
    wallet — i.e. unlike BingX's allAccountBalance, every row here is usable.
    """
    rows = _signed("GET", SPOT_URL, "/sapi/v1/asset/wallet/balance", env) or []
    btc = float(requests.get(f"{SPOT_URL}/api/v3/ticker/price",
                             params={"symbol": "BTCUSDT"}, timeout=10).json()["price"])
    out = {}
    for r in rows:
        name = r.get("walletName") or ""
        # Unknown wallet names still show up (slugified) — a wallet Binance
        # adds later must not silently swallow the user's money.
        key = _WALLET_KEYS.get(name) or name.lower().replace(" ", "_").replace("-", "_")
        out[key] = round(float(r.get("balance") or 0) * btc, 2)
    return out


def get_equity(env: dict) -> dict:
    """USDⓈ-M futures equity, plus a per-wallet breakdown.

    Returns {'equity': float, 'currency': 'USDT', 'accounts': {name: float}}.
    `equity` is totalMarginBalance (wallet balance + unrealized PnL) of the
    USDⓈ-M futures account — the sizing base. The `futures` row of `accounts`
    can exceed it: the wallet may also hold non-USDT assets (BNB kept for fee
    discounts), which are not margin in single-asset mode.
    """
    acct = _signed("GET", FAPI_URL, "/fapi/v2/account", env)
    equity = float(acct.get("totalMarginBalance") or 0)

    accounts = {"futures": round(equity, 2)}
    # Best-effort: a failing breakdown must never take the main equity down.
    try:
        accounts = _wallets_usdt(env) or accounts
    except Exception:
        pass
    return {"equity": equity, "currency": "USDT", "accounts": accounts}


def get_holdings(env: dict) -> list:
    """Coin holdings outside the futures account — DISPLAY ONLY, never fed to
    the reconciler (a spot coin listed as a position would make it net futures
    orders against spot inventory).

    Returns [{'asset', 'amount', 'usdt_value', 'wallet'}, ...] sorted by value
    desc; wallet ∈ {'spot','funding','futures'}. usdt_value is None when the
    asset has no live USDT (or via-BTC) pair — e.g. delisted tickers like
    ETHW, which still must be LISTED (hiding an unpriceable coin = money
    vanishing). Dust under 0.1 USDT is dropped; unpriceable rows are kept.

    Futures rows use per-asset MARGIN balance (wallet balance + that asset's
    cross uPnL) — the number the Binance App shows per asset, and the split
    that sums to the wallet-breakdown futures row (raw wallet balance would
    disagree with it by exactly the unrealized PnL, reading as a bug).
    """
    out = []
    for b in _signed("GET", FAPI_URL, "/fapi/v2/balance", env) or []:
        amt = float(b.get("balance") or 0) + float(b.get("crossUnPnl") or 0)
        if amt != 0:
            out.append({"asset": b.get("asset"), "amount": amt, "wallet": "futures"})
    spot = _signed("GET", SPOT_URL, "/api/v3/account", env,
                   {"omitZeroBalances": "true"})
    for b in spot.get("balances", []):
        amt = float(b.get("free") or 0) + float(b.get("locked") or 0)
        if amt > 0:
            out.append({"asset": b.get("asset"), "amount": amt, "wallet": "spot"})
    fund = _signed("POST", SPOT_URL, "/sapi/v1/asset/get-funding-asset", env) or []
    for b in fund:
        amt = float(b.get("free") or 0) + float(b.get("locked") or 0)
        if amt > 0:
            out.append({"asset": b.get("asset"), "amount": amt, "wallet": "funding"})
    if not out:
        return []

    # one bulk call for every pair — per-asset lookups would be N round-trips
    prices = {r["symbol"]: float(r["price"])
              for r in requests.get(f"{SPOT_URL}/api/v3/ticker/price", timeout=10).json()}

    def _value(asset, amount):
        if asset in ("USDT",):
            return round(amount, 2)
        p = prices.get(f"{asset}USDT")
        if p is None and prices.get(f"{asset}BTC") and prices.get("BTCUSDT"):
            p = prices[f"{asset}BTC"] * prices["BTCUSDT"]
        return round(amount * p, 2) if p else None

    for h in out:
        h["usdt_value"] = _value(h["asset"], h["amount"])
    # abs(): a negative margin row (multi-asset mode) is information, not dust
    out = [h for h in out if h["usdt_value"] is None or abs(h["usdt_value"]) >= 0.1]
    out.sort(key=lambda h: -(h["usdt_value"] or 0))
    return out


def _flow_pages(env, path, start_ms, end_ms):
    """All rows of a capital-history endpoint for one time window, offset-paged
    (limit is 1000, default AND max). Raises rather than silently truncating."""
    rows, offset = [], 0
    while True:
        page = _signed("GET", SPOT_URL, path, env,
                       {"startTime": start_ms, "endTime": end_ms,
                        "limit": 1000, "offset": offset}) or []
        rows.extend(page)
        if len(page) < 1000:
            return rows
        offset += 1000
        if offset > 20000:
            raise Exception(f"Binance flows: >20k records in one window | {path}")


def get_flows(env: dict, since: int) -> list:
    """External (on-chain) deposit/withdraw records since `since` (epoch secs).

    Returns [{'ts', 'direction' ('in'/'out'), 'currency', 'amount', 'txid'}, ...]
    ascending by ts; amounts always positive. COMPLETED records only — deposits
    status 1 (success) or 6 (credited, not yet withdrawable: already in equity),
    withdrawals status 6 (completed). Internal (off-chain, user-to-user /
    sub-account) transfers are excluded via transferType != 0 — inside the
    whole-account equity they net out or belong to PnL adjustment done
    platform-side, only real external flows count.

    Both endpoints cap the query window at 90 days (verified: official docs +
    ccxt), so the pull slides 89-day windows from `since` to now. `txid` is the
    chain tx hash; falls back to Binance's own record id when the hash is
    missing. Withdrawal `ts` is applyTime (UTC — when the balance left the
    account); withdrawal `amount` excludes the network fee (Binance reports it
    separately), so fee-sized residue lands in PnL as a cost.
    """
    since_ms = int(since) * 1000
    now_ms = int(time.time() * 1000)
    flows = []
    window = 89 * 24 * 3600 * 1000
    start = since_ms
    while start <= now_ms:
        end = min(start + window, now_ms)
        for d in _flow_pages(env, "/sapi/v1/capital/deposit/hisrec", start, end):
            if d.get("status") not in (1, 6) or int(d.get("transferType") or 0) != 0:
                continue
            amt = float(d.get("amount") or 0)
            if amt <= 0:
                continue
            flows.append({
                "ts": int(d.get("insertTime") or 0) // 1000,
                "direction": "in",
                "currency": d.get("coin") or "",
                "amount": amt,
                "txid": d.get("txId") or str(d.get("id") or ""),
            })
        for w in _flow_pages(env, "/sapi/v1/capital/withdraw/history", start, end):
            if w.get("status") != 6 or int(w.get("transferType") or 0) != 0:
                continue
            amt = float(w.get("amount") or 0)
            if amt <= 0:
                continue
            # applyTime is "YYYY-MM-DD HH:MM:SS" in UTC (ccxt parses it as UTC)
            ts = calendar.timegm(time.strptime(w["applyTime"], "%Y-%m-%d %H:%M:%S"))
            flows.append({
                "ts": ts,
                "direction": "out",
                "currency": w.get("coin") or "",
                "amount": amt,
                "txid": w.get("txId") or str(w.get("id") or ""),
            })
        start = end + 1
    flows = [f for f in flows if f["ts"] >= int(since)]
    flows.sort(key=lambda f: f["ts"])
    return flows


def get_positions(env: dict) -> list:
    """Open USDⓈ-M futures positions.

    Returns [{'symbol', 'side', 'size', 'mark_price'}, ...], [] if flat.
    Binance already reports canonical dashless uppercase symbols (BTCUSDT).
    """
    rows = _signed("GET", FAPI_URL, "/fapi/v2/positionRisk", env) or []
    out = []
    for p in rows:
        amt = float(p.get("positionAmt") or 0)
        if amt == 0:
            continue
        side = (p.get("positionSide") or "").upper()
        out.append({
            "symbol": (p.get("symbol") or "").upper(),
            # one-way accounts report positionSide BOTH — direction is the sign
            # of positionAmt. Without this, callers see side="both": flatten
            # skips the row and the reconciler reads actual as 0 → doubles up.
            "side": side.lower() if side in ("LONG", "SHORT")
                    else ("short" if amt < 0 else "long"),
            "size": abs(amt),
            "mark_price": float(p.get("markPrice") or 0),
        })
    return out
