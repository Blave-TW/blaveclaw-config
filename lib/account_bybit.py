"""
Bybit account library — Unified Trading Account (UTA) equity, positions,
holdings and external cash flows.

Discovered by filename (lib.account_bybit.get_equity / get_positions).
Credentials: BYBIT_API_KEY / BYBIT_SECRET_KEY in .env (the web 下單設定 names).

Wallet model: a UTA account has exactly TWO wallets and they need TWO different
endpoints — /v5/account/wallet-balance accepts only accountType=UNIFIED (every
other type returns retCode 10001), while the FUND wallet is only readable via
/v5/asset/transfer/query-account-coins-balance (which in turn cannot enumerate
UNIFIED — it demands an explicit 1-10 coin list). Orders draw on UNIFIED, so
that is the equity base for sizing; FUND money cannot trade and is NOT counted
in totalEquity, which is exactly why it must appear in the breakdown.

Spot has no separate wallet here: a spot buy lands in the UNIFIED coin array
and doubles as margin.

UTA ONLY. A classic (non-UTA) account exposes different account types that were
never verified against a real account, so it raises instead of guessing.
"""

import hashlib
import hmac
import json
import time

import requests

HOST = "https://api.bybit.com"
RECV_WINDOW = "5000"
BROKER_REFERER = "Ue001036"  # broker attribution — mandatory on every request


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


def _request(env, method, path, params=None, body=None, timeout=10):
    """Signed v5 request. The signed payload is the query string (GET) or the
    compact JSON body (POST) — it must byte-match what is sent."""
    api_key, secret = _creds(env)
    ts = str(int(time.time() * 1000))
    if method.upper() == "GET":
        payload = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        url = f"{HOST}{path}" + (f"?{payload}" if payload else "")
        data = None
    else:
        payload = json.dumps(body or {}, separators=(",", ":"))
        url = f"{HOST}{path}"
        data = payload
    sign = hmac.new(secret.encode(), (ts + api_key + RECV_WINDOW + payload).encode(),
                    hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN": sign,
        "referer": BROKER_REFERER,
        "Content-Type": "application/json",
    }
    r = requests.request(method.upper(), url, headers=headers, data=data, timeout=timeout)
    r.raise_for_status()
    payload_json = r.json()
    code = payload_json.get("retCode")
    if code != 0:
        # Error classification the connect page can explain (template item ④):
        # 10003/10004 = bad key or signature, 10005/10010 = permission or IP,
        # 10006 = rate limited.
        # Bybit's 10004 message echoes the signed origin_string, which CONTAINS
        # THE API KEY — this string reaches logs and the web connect-failure
        # page, so redact the key out of it before it ever leaves this function.
        msg = str(payload_json.get("retMsg") or "")
        if api_key and api_key in msg:
            msg = msg.replace(api_key, "***")
        raise Exception(f"bybit {path} retCode={code}: {msg}")
    return payload_json.get("result") or {}


def _assert_uta(env):
    """UTA-only guard. Everything in this module was verified against uta=1;
    a classic account's CONTRACT/SPOT wallets were never tested, so refuse
    rather than half-read the account.

    The flag lives on /v5/user/query-api, NOT on /v5/account/info — that
    endpoint returns marginMode/unifiedMarginStatus/dcpStatus and no `uta`
    key at all, so guarding on it would pass vacuously for every account.
    The same call carries `permissions`, used below for a readable
    missing-permission error instead of a bare venue code."""
    api = _request(env, "GET", "/v5/user/query-api")
    if str(api.get("uta") or "") != "1":
        raise Exception(
            "bybit: this account is not a Unified Trading Account (UTA). "
            "Upgrade it in the Bybit app, then rebind the key."
        )
    perms = api.get("permissions") or {}
    if not (perms.get("ContractTrade") or perms.get("Derivatives")):
        raise Exception(
            "bybit: this API key has no derivatives trading permission — "
            "recreate it with Contract/Derivatives enabled and rebind."
        )


def _unified(env):
    """UNIFIED wallet row: totalEquity plus the coin array (which is also the
    spot inventory on UTA)."""
    rows = _request(env, "GET", "/v5/account/wallet-balance",
                    {"accountType": "UNIFIED"}).get("list") or []
    return rows[0] if rows else {}


def _fund(env):
    """FUND wallet coin balances. Best-effort: the key may lack the wallet
    permission, and a FUND read failing must not take equity down with it."""
    try:
        res = _request(env, "GET", "/v5/asset/transfer/query-account-coins-balance",
                       {"accountType": "FUND"})
        return res.get("balance") or []
    except Exception:
        return []


def get_equity(env: dict) -> dict:
    """Equity = the UNIFIED account's totalEquity (USD-valued, not USDT face
    value: a 90 USDT balance reads back as 89.97579). FUND is reported in the
    breakdown but never added in — it cannot trade, and sizing off it would
    over-lever the account."""
    _assert_uta(env)
    row = _unified(env)
    equity = float(row.get("totalEquity") or 0)
    fund = 0.0
    for b in _fund(env):
        # FUND rows carry no usdValue; non-USDT coins are valued by the caller,
        # so only stable face value is summed here (documented, not silent).
        if (b.get("coin") or "").upper() in ("USDT", "USDC"):
            fund += float(b.get("walletBalance") or 0)
    return {
        "equity": equity,
        "currency": "USDT",
        "accounts": {"unified": round(equity, 8), "fund": round(fund, 8)},
    }


def get_positions(env: dict) -> list:
    """Open USDT-perp positions. Symbols are already canonical on Bybit
    ('BTCUSDT'), so no mapping is needed."""
    rows = _request(env, "GET", "/v5/position/list",
                    {"category": "linear", "settleCoin": "USDT"}).get("list") or []
    positions = []
    for p in rows:
        size = float(p.get("size") or 0)
        if size == 0:
            continue
        symbol = p.get("symbol", "")
        mark_px = float(p.get("markPrice") or 0)
        # A non-zero position we cannot value must RAISE, never be skipped: a
        # dropped row reads as flat and the reconciler re-buys on top of it.
        if mark_px <= 0:
            raise Exception(f"bybit position {symbol}: no markPrice on a "
                            f"non-zero position")
        positions.append({
            "symbol": symbol,
            "side": "long" if (p.get("side") or "").lower() == "buy" else "short",
            "size": size,
            "mark_price": mark_px,
        })
    return positions


def get_holdings(env: dict) -> list:
    """Coins held across both wallets — DISPLAY ONLY, never positions.

    UNIFIED rows carry their own usdValue; FUND rows do not, so FUND coins are
    priced off the public ticker and left at None when there is no live market
    (a delisted ticker is still LISTED — hiding it is money vanishing)."""
    out = []
    for c in (_unified(env).get("coin") or []):
        amount = float(c.get("walletBalance") or 0)
        if amount == 0:
            continue
        usd = c.get("usdValue")
        out.append({"asset": (c.get("coin") or "").upper(), "amount": amount,
                    "usdt_value": float(usd) if usd not in (None, "") else None,
                    "wallet": "unified"})
    fund_rows = [b for b in _fund(env) if float(b.get("walletBalance") or 0) != 0]
    prices = _spot_prices(env) if fund_rows else {}
    for b in fund_rows:
        asset = (b.get("coin") or "").upper()
        amount = float(b.get("walletBalance") or 0)
        if asset in ("USDT", "USDC"):
            value = amount
        else:
            px = prices.get(f"{asset}USDT")
            value = amount * px if px else None
        out.append({"asset": asset, "amount": amount, "usdt_value": value,
                    "wallet": "fund"})
    # Dust under 0.1 USDT is noise; unpriceable coins are kept regardless.
    out = [h for h in out if h["usdt_value"] is None or h["usdt_value"] >= 0.1]
    out.sort(key=lambda h: (h["usdt_value"] is not None, h["usdt_value"] or 0),
             reverse=True)
    return out


def _spot_prices(env):
    """Public spot last prices, one call. Best-effort: pricing failure means
    usdt_value None, never a dropped holding."""
    try:
        r = requests.get(f"{HOST}/v5/market/tickers", params={"category": "spot"},
                         headers={"referer": BROKER_REFERER}, timeout=10)
        r.raise_for_status()
        return {t["symbol"]: float(t["lastPrice"])
                for t in (r.json().get("result") or {}).get("list", [])
                if t.get("lastPrice")}
    except Exception:
        return {}


def _flow_pages(env, path, params):
    """Cursor-paged private list endpoint."""
    cursor, seen = "", 0
    while True:
        q = dict(params, limit=50)
        if cursor:
            q["cursor"] = cursor
        res = _request(env, "GET", path, q)
        rows = res.get("rows") or []
        for r in rows:
            yield r
        seen += len(rows)
        cursor = res.get("nextPageCursor") or ""
        if not cursor or not rows or seen > 5000:
            return


def get_flows(env: dict, since: int) -> list:
    """External (on-chain) deposit/withdraw records since `since` (epoch secs).

    Returns [{'ts', 'direction' ('in'/'out'), 'currency', 'amount', 'txid'}, ...]
    ascending by ts; amounts always positive.

    Bybit separates internal from external BY ENDPOINT, not by a flag — do not
    port Binance's transferType or BingX's direction-flag handling here.
    /v5/asset/deposit/query-record is on-chain only (internal deposits have
    their own query-internal-record endpoint) and withdrawType=0 restricts
    withdrawals to on-chain. Wallet-to-wallet transfers live on
    /v5/asset/transfer/query-inter-transfer-list and never appear in either,
    so no post-filtering is needed (verified live: a FUND->UNIFIED transfer
    showed up only in the inter-transfer list).

    Window: both bounds are always sent; the pull slides 29-day windows because
    the range is capped at 30 days. `since` is floored at 2 years back — Bybit
    rejects an epoch-0 startTime outright (retCode 131002) and the platform's
    own first pull only looks back 2 days, so an unbounded caller would buy
    thousands of pointless windows and a rate-limit ban.

    FEE SEMANTICS (measured on a live withdrawal): `amount` is the requested
    amount and `withdrawFee` is charged ON TOP — a 10 USDT withdrawal with a
    1 USDT fee debited the account 11 (confirmed against
    /v5/account/transaction-log: one TRANSFER_OUT of -11). The contract wants
    the FULL balance debit, so the out leg reports amount + withdrawFee.
    Withdrawals are debited from UNIFIED, not FUND, even when FUND is funded.
    """
    now = int(time.time())
    window = 29 * 24 * 3600
    since = max(int(since), now - 730 * 24 * 3600)
    flows = []
    for path, direction, extra in (
        ("/v5/asset/deposit/query-record", "in", {}),
        ("/v5/asset/withdraw/query-record", "out", {"withdrawType": 0}),
    ):
        start = since
        while start <= now:
            end = min(start + window, now)
            params = dict(extra, startTime=start * 1000, endTime=end * 1000)
            for r in _flow_pages(env, path, params):
                if direction == "in":
                    # deposit status 3 = success (measured on a live record)
                    if int(r.get("status") or 0) != 3:
                        continue
                    ts = int(r.get("successAt") or 0) // 1000
                else:
                    if str(r.get("status") or "").lower() not in (
                            "success", "blockchainconfirmed"):
                        continue
                    ts = int(r.get("updateTime") or r.get("createTime") or 0) // 1000
                amount = abs(float(r.get("amount") or 0))
                if direction == "out":
                    # the fee is charged on top of `amount`; the equity curve
                    # needs the full debit or the fee reads as a trading loss
                    amount += abs(float(r.get("withdrawFee") or 0))
                if amount == 0 or ts == 0:
                    continue
                flows.append({
                    "ts": ts,
                    "direction": direction,
                    "currency": (r.get("coin") or "").upper(),
                    "amount": amount,
                    "txid": r.get("txID") or r.get("txId") or str(r.get("withdrawId") or ""),
                })
            start = end + 1
    flows.sort(key=lambda f: f["ts"])
    return flows
