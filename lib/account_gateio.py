"""
Gate.io account library — USDT perpetual futures equity & positions.

Discovered by filename (lib.account_gateio.get_equity / get_positions).
Credentials: GATEIO_API_KEY / GATEIO_SECRET_KEY in .env (the web 下單設定
names; GATE_API_KEY / GATE_SECRET_KEY accepted as a fallback — the
blave-quant-skill convention).

Wallet model: Gate.io is multi-wallet (spot / margin / futures / delivery /
options / earn ...). Orders draw on the FUTURES (USDT-settled) account —
that is the equity base for sizing. /wallet/total_balance gives the
every-wallet breakdown so transferred money never vanishes from display.
"""

import hashlib
import hmac
import json
import time

import requests

HOST = "https://api.gateio.ws"
PREFIX = "/api/v4"


def _creds(env):
    api_key = env.get("GATEIO_API_KEY") or env.get("GATE_API_KEY")
    secret = env.get("GATEIO_SECRET_KEY") or env.get("GATE_SECRET_KEY")
    if not api_key or not secret:
        raise ValueError("GATEIO_API_KEY / GATEIO_SECRET_KEY missing from .env")
    return api_key, secret


def _request(env, method, path, query="", body=None, timeout=10):
    """Signed APIv4 request. The signed path includes the /api/v4 prefix and
    the query string must byte-match what is sent (unencoded form)."""
    api_key, secret = _creds(env)
    body_str = "" if body is None else json.dumps(body)
    ts = str(int(time.time()))
    payload_hash = hashlib.sha512(body_str.encode()).hexdigest()
    sign_str = "\n".join([method.upper(), PREFIX + path, query, payload_hash, ts])
    sig = hmac.new(secret.encode(), sign_str.encode(), hashlib.sha512).hexdigest()
    headers = {
        "KEY": api_key,
        "Timestamp": ts,
        "SIGN": sig,
        "Content-Type": "application/json",
        "X-Gate-Channel-Id": "blave",  # broker attribution — every request
    }
    url = HOST + PREFIX + path + ("?" + query if query else "")
    r = requests.request(method, url, headers=headers,
                         data=body_str or None, timeout=timeout)
    if not r.ok:
        try:
            err = r.json()
        except ValueError:
            err = {}
        raise Exception(
            f"Gate.io error {err.get('label', r.status_code)}: "
            f"{err.get('message', r.text[:120])} | {method} {path}"
        )
    return r.json()


def get_equity(env: dict) -> dict:
    """Futures-account equity plus per-wallet breakdown.

    Returns {'equity': float, 'currency': str, 'accounts': {name: float}}.
    `equity` = USDT futures account total + unrealised PnL — the wallet
    orders draw on (sizing base). `accounts` comes from /wallet/total_balance
    (needs wallet read permission — best-effort, its failure must not take
    equity down): every wallet Gate.io shows in the transfer menu, valued in
    USDT, so a transfer to any of them stays visible.
    """
    # A Gate.io futures account does not EXIST until the first transfer into
    # it — a fresh binding raises USER_NOT_FOUND ("please transfer funds
    # first"). That is a state, not a broken key: equity is 0, the link is
    # fine, and the wallet breakdown below still shows where the money sits.
    try:
        acct = _request(env, "GET", "/futures/usdt/accounts")
    except Exception as e:
        if "USER_NOT_FOUND" not in str(e):
            raise
        acct = {}
    equity = float(acct.get("total", 0) or 0) + float(acct.get("unrealised_pnl", 0) or 0)

    accounts = {"futures": round(equity, 2)}
    try:
        tb = _request(env, "GET", "/wallet/total_balance")
        for name, d in (tb.get("details") or {}).items():
            if name == "futures":
                continue  # already the exact figure above
            amt = float((d or {}).get("amount", 0) or 0)
            if amt:
                accounts[name] = round(amt, 2)
    except Exception:
        pass

    return {"equity": equity, "currency": "USDT", "accounts": accounts}


def get_holdings(env: dict) -> list:
    """Coin holdings — DISPLAY ONLY, never fed to the reconciler.

    Per-asset rows for the two wallets that can be enumerated by asset:
    SPOT (/spot/accounts, valued via ONE bulk tickers call) and FUTURES
    (USDT-settled — a single USDT row incl. unrealised PnL). Other wallets
    (margin/earn/...) surface as USDT-valued totals in get_equity's accounts
    breakdown. Returns [{'asset','amount','usdt_value','wallet'}, ...]
    sorted by value desc; dust under 0.1 USD dropped; unpriceable assets
    kept with usdt_value=None (never hidden)."""
    out = []

    spot_rows = _request(env, "GET", "/spot/accounts")
    if spot_rows:
        last = {}
        try:  # valuation only — a tickers hiccup degrades to usdt_value=None
            last = {t.get("currency_pair"): float(t.get("last") or 0)
                    for t in _request(env, "GET", "/spot/tickers")}
        except Exception:
            pass
        for r in spot_rows:
            amt = float(r.get("available", 0) or 0) + float(r.get("locked", 0) or 0)
            if amt == 0:
                continue
            asset = r.get("currency")
            if asset in ("USDT", "USDC"):
                usd = amt
            else:
                px = last.get(f"{asset}_USDT")
                usd = amt * px if px else None
            out.append({"asset": asset, "amount": amt,
                        "usdt_value": round(usd, 2) if usd is not None else None,
                        "wallet": "spot"})

    try:
        acct = _request(env, "GET", "/futures/usdt/accounts")
    except Exception as e:  # same not-yet-created state as get_equity
        if "USER_NOT_FOUND" not in str(e):
            raise
        acct = {}
    fut = float(acct.get("total", 0) or 0) + float(acct.get("unrealised_pnl", 0) or 0)
    if fut:
        out.append({"asset": acct.get("currency") or "USDT", "amount": fut,
                    "usdt_value": round(fut, 2), "wallet": "futures"})

    out = [h for h in out if h["usdt_value"] is None or abs(h["usdt_value"]) >= 0.1]
    out.sort(key=lambda h: -(h["usdt_value"] or 0))
    return out


def _flow_pages(env, path, start, end):
    """All rows of a wallet-history endpoint for one time window, offset-paged
    (limit max 500 per the official SDK). Raises rather than truncating."""
    rows, offset = [], 0
    while True:
        page = _request(env, "GET", path,
                        query=f"from={start}&to={end}&limit=500&offset={offset}") or []
        rows.extend(page)
        if len(page) < 500:
            return rows
        offset += 500
        if offset > 10000:
            raise Exception(f"Gate.io flows: >10k records in one window | {path}")


def _flow_currency(r):
    """Deposit records may carry a legacy multichain currency id ('USDT_TRX');
    strip the suffix only when it matches the record's chain — anything else
    is kept verbatim (an odd label beats a wrong merge)."""
    cur = r.get("currency") or ""
    if "_" in cur:
        base, suffix = cur.rsplit("_", 1)
        if suffix.upper() == (r.get("chain") or "").upper():
            return base
    return cur


def get_flows(env: dict, since: int) -> list:
    """External (on-chain) deposit/withdraw records since `since` (epoch secs).

    Returns [{'ts', 'direction' ('in'/'out'), 'currency', 'amount', 'txid'}, ...]
    ascending by ts; amounts always positive. COMPLETED records only — status
    DONE (which also excludes BCODE = GateCode, an off-chain value transfer);
    'b'-prefixed record ids (GateCode) are skipped defensively too. These
    wallet endpoints list on-chain records only — same-exchange internal
    transfers (/wallet/transfers, sub-account, push/UID transfers) live on
    separate endpoints and never appear here, so only real external flows
    count for netting equity-snapshot PnL.

    from/to are epoch SECONDS, window capped at 30 days (official SDK:
    "Record query time range cannot exceed 30 days"; default is last 7 days,
    so both bounds are always passed) — the pull slides 29-day windows from
    `since` to now. `txid` is the chain tx hash, falling back to Gate's own
    record id. Withdrawal `amount` INCLUDES the fee (ccxt subtracts `fee` to
    get the net-received figure), i.e. it is already the full balance debit.
    """
    now = int(time.time())
    flows = []
    window = 29 * 24 * 3600
    for path, direction in (("/wallet/deposits", "in"),
                            ("/wallet/withdrawals", "out")):
        start = int(since)
        while start <= now:
            end = min(start + window, now)
            for r in _flow_pages(env, path, start, end):
                rid = str(r.get("id") or "")
                if (r.get("status") or "") != "DONE" or rid.startswith("b"):
                    continue
                amt = float(r.get("amount") or 0)
                ts = int(float(r.get("timestamp") or 0))
                if amt <= 0 or ts < int(since):
                    continue
                flows.append({
                    "ts": ts,
                    "direction": direction,
                    "currency": _flow_currency(r),
                    "amount": amt,
                    "txid": r.get("txid") or rid,
                })
            start = end + 1
    flows.sort(key=lambda f: f["ts"])
    return flows


_mult_cache = {}  # contract -> float quanto_multiplier (per-process)


def _multiplier(env, contract):
    """quanto_multiplier (base units per contract) from the public contract
    detail, cached. None on failure — callers fall back to value/mark."""
    if contract not in _mult_cache:
        try:
            c = _request(env, "GET", f"/futures/usdt/contracts/{contract}")
            _mult_cache[contract] = float(c.get("quanto_multiplier") or 0) or None
        except Exception:
            return None
    return _mult_cache[contract]


def get_positions(env: dict) -> list:
    """Open USDT-perp positions.

    Returns [{'symbol': str, 'side': str, 'size': float, 'mark_price': float}, ...]
    Returns [] if flat.

    Gate.io `size` is signed CONTRACTS — converted to base units via the
    contract's quanto_multiplier (the OKX ctVal lesson: exact multiplication,
    never a notional/price division). Dual (hedge) mode rows carry
    mode=dual_long/dual_short — side comes from mode there, sign otherwise.
    Symbols are CANONICAL (dashless uppercase 'BTCUSDT') — venue formats
    never leak."""
    try:
        rows = _request(env, "GET", "/futures/usdt/positions")
    except Exception as e:  # futures account not created yet = flat, not broken
        if "USER_NOT_FOUND" not in str(e):
            raise
        rows = []
    positions = []
    for p in rows or []:
        ct = float(p.get("size", 0) or 0)
        if ct == 0:
            continue
        contract = p.get("contract", "")
        mark_px = float(p.get("mark_price", 0) or 0)
        # A non-zero position we cannot value must RAISE, never be skipped:
        # a silently dropped row reads as flat and the reconciler re-buys the
        # whole target on top of it (venue_wiring "errors propagate" rule).
        if mark_px <= 0:
            raise Exception(f"gateio position {contract}: no mark_price on a "
                            f"non-zero position")
        mode = p.get("mode", "single")
        if mode == "dual_short":
            side = "short"
        elif mode == "dual_long":
            side = "long"
        else:
            side = "short" if ct < 0 else "long"
        mult = _multiplier(env, contract)
        if mult:
            size = abs(ct) * mult
        else:
            value = float(p.get("value", 0) or 0)  # position value in USDT
            size = value / mark_px if value else 0.0
        if size <= 0:
            raise Exception(f"gateio position {contract}: cannot size position "
                            f"(quanto_multiplier and value both unavailable)")
        positions.append({
            "symbol": contract.replace("_", "").upper(),
            "side": side,
            "size": size,
            "mark_price": mark_px,
        })
    return positions
