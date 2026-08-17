"""Capital (群益) account snapshot worker.

Runs as a persistent NSSM service under the Administrator account (password
logon) — the ONLY context where SKCOM's certificate check passes (see
references/capital-broker.md, error 602). Polls futures equity / open interest
/ securities inventory over COM and writes state/capital_account.json;
lib/account_capital.py (executed by the platform account_reader, which runs as
LocalSystem and therefore cannot touch COM itself) only reads that file.

Cadence: 60s. GetFutureRights is rate-limited (error 1019 when called too
often); 60s measured safe 2026-08-13.
"""
import json
import os
import sys
import time
from pathlib import Path

WORKSPACE = os.environ.get("BLAVE_AGENT_WORKSPACE", r"C:\blave-agent\workspace")
OUT_PATH = os.path.join(WORKSPACE, "state", "capital_account.json")
HEARTBEAT_PATH = Path(WORKSPACE) / "state" / "heartbeat" / "capital_worker"
# lib/order_capital.py touches this after every accepted order; the sleep loop
# below early-ticks on it so a fill reaches the snapshot in seconds, not at the
# next 60s poll (fill→dashboard was ~3.5min without it, measured 2026-08-17).
# Derived from __file__ like account_capital.py's _SNAPSHOT (NOT the
# BLAVE_AGENT_WORKSPACE env var above) — producer (order_capital.py) and
# consumer (this file) are both lib/<name>.py in the same workspace, so this
# guarantees they agree on the path even if the two NSSM services
# (blaveclaw-reconciler / blave-agent-capital) ever end up with different
# environments (audit P2-2: env-or-default would silently write/read
# different files instead of erroring).
REFRESH_FLAG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "state", "capital_refresh")
POLL_S = 60
REFRESH_CHECK_S = 2
# GetFutureRights/GetOpenInterest/GetRealBalanceReport are rate-limited (1019
# SK_ERROR_QUERY_IN_PROCESSING); manual/broker measured 60s safe, not this
# floor (audit P1 — no burst test at 10s). A hit is handled by
# QueryInProgress below (skip the tick, keep the last good snapshot) rather
# than assumed impossible, so the floor being untested is no longer a
# correctness risk — only extra broker-side query traffic.
MIN_TICK_SPACING_S = 10
EVENT_TIMEOUT_S = 15

# COM is Windows-only; deferred to main() so the module still imports for
# structure checks on non-Windows machines (comtypes fails to import there).
comtypes = None
pythoncom = None
sk = None


def _init_com():
    global comtypes, pythoncom, sk
    import comtypes as _comtypes
    import comtypes.client
    import pythoncom as _pythoncom

    _comtypes.client.GetModule(r"C:\skcom\x64\SKCOM.dll")
    import comtypes.gen.SKCOMLib as _sk

    comtypes, pythoncom, sk = _comtypes, _pythoncom, _sk


def _log(msg):
    print(f"[capital_worker] {msg}", flush=True)


def _read_env():
    env = {}
    try:
        with open(os.path.join(WORKSPACE, ".env")) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
    except OSError as e:
        _write_snapshot({"ok": False, "error": f".env unreadable: {e}"})
        time.sleep(30)  # don't hot-loop through NSSM restarts
        sys.exit(1)
    return env


def _write_snapshot(payload):
    payload["read_at"] = int(time.time())
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, OUT_PATH)


class QueryInProgress(RuntimeError):
    """SKCOM 1019 SK_ERROR_QUERY_IN_PROCESSING — transient rate-limit, not a
    real failure. Raised instead of a plain RuntimeError so the main loop can
    skip this tick (keep the last good snapshot on disk) instead of writing
    an error snapshot over it and tearing down the COM session."""


class Events:
    """Shared event buffers; cleared before each query."""

    futures_accounts = []
    stock_accounts = []
    rights_rows = []
    rights_done = False
    balance_rows = []
    balance_done = False
    oi_rows = []
    oi_done = False

    def OnReplyMessage(self, bstrUserID, bstrMessages):
        return -1

    def OnAccount(self, bstrLogInID, bstrAccountData):
        f = bstrAccountData.split(",")
        acct = f[1] + f[3]
        if f[0] == "TF":
            Events.futures_accounts.append(acct)
        elif f[0] == "TS":
            Events.stock_accounts.append(acct)

    def OnFutureRights(self, bstrData):
        if bstrData.startswith("##"):
            Events.rights_done = True
        else:
            Events.rights_rows.append(bstrData)

    def OnRealBalanceReport(self, bstrData):
        if bstrData.startswith("##"):
            Events.balance_done = True
        else:
            Events.balance_rows.append(bstrData)

    def OnOpenInterest(self, bstrData):
        if bstrData.startswith("##"):
            Events.oi_done = True
        else:
            Events.oi_rows.append(bstrData)


def _pump_until(cond, timeout=EVENT_TIMEOUT_S):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        pythoncom.PumpWaitingMessages()
        time.sleep(0.05)
    return cond()


def _check_query_code(code, call_name):
    if code == 1019:
        raise QueryInProgress(f"{call_name} code=1019 (rate-limited, transient)")
    if code != 0:
        raise RuntimeError(f"{call_name} code={code}")


def query_rights(order, login_id, tf_acct):
    """First OnFutureRights row = base currency. Field table: capital-broker.md
    Step 6c (manual V2.13.59 §4-2-i). 權益數 = idx 6, 幣別 = idx 25."""
    Events.rights_rows, Events.rights_done = [], False
    code = order.GetFutureRights(login_id, tf_acct, 1)
    _check_query_code(code, "GetFutureRights")
    if not _pump_until(lambda: Events.rights_done):
        raise RuntimeError("GetFutureRights: no ## terminator within timeout")
    if not Events.rights_rows:
        raise RuntimeError("GetFutureRights: terminator without data rows")
    f = Events.rights_rows[0].split(",")
    currency = f[25].strip()
    return {
        "equity": float(f[6]),
        "available": float(f[31]) if f[31] else None,
        "currency": "TWD" if currency == "NTD" else currency,
    }


def query_open_interest(order, login_id, tf_acct):
    """Rows: 市場別,帳號,商品,買賣別,未平倉部位,當沖未平倉,平均成本,一點價值,
    單口手續費,交易稅,LOGIN_ID. Empty account → single '001,查無資料,帳號' row.
    Ends with a '##' terminator row like the other reports (observed live
    2026-08-13 — comma-padded)."""
    Events.oi_rows, Events.oi_done = [], False
    code = order.GetOpenInterest(login_id, tf_acct)
    _check_query_code(code, "GetOpenInterest")
    if not _pump_until(lambda: Events.oi_done):
        raise RuntimeError("GetOpenInterest: no ## terminator within timeout")
    positions = []
    for row in Events.oi_rows:
        f = row.split(",")
        if f[0] == "001":  # 查無資料
            continue
        # Parsed fields only — the trailing LOGIN_ID is the user's national ID;
        # never persist the raw row (PII in a file that may be shipped later).
        # Lots not notional: conversion needs a mark price; the reconciler for
        # TW futures is hand-wired, so display keeps lots. Field meanings
        # unverified against live data until the first real fill.
        # 買賣別 is a LETTER — live row 2026-08-14: "TF,acct,TM2608,B,1,0,46138.0000,..."
        # ("B"/"S", not the 0/1 the order structs use).
        positions.append({
            "symbol": f[2],
            "side": "buy" if f[3] in ("B", "0") else "sell",
            "lots": float(f[4]) if f[4] else 0.0,
            "avg_cost": float(f[6]) if len(f) > 6 and f[6] else None,
        })
    return positions


def query_balance(order, login_id, ts_acct):
    """Securities inventory rows (capital-broker.md Step 6b): 股票代號 idx 0,
    即時庫存 idx 14."""
    Events.balance_rows, Events.balance_done = [], False
    code = order.GetRealBalanceReport(login_id, ts_acct)
    _check_query_code(code, "GetRealBalanceReport")
    if not _pump_until(lambda: Events.balance_done):
        raise RuntimeError("GetRealBalanceReport: no ## terminator within timeout")
    # Aggregate per ticker — one row per 庫存種類 (集保/融資/融券) otherwise
    # shows the same stock as duplicate lines.
    shares_by_asset = {}
    for row in Events.balance_rows:
        f = row.split(",")
        shares = float(f[14]) if len(f) > 14 and f[14] else 0.0
        if shares:
            shares_by_asset[f[0]] = shares_by_asset.get(f[0], 0.0) + shares
    return [
        {"asset": a, "amount": s, "usdt_value": None, "wallet": "securities"}
        for a, s in sorted(shares_by_asset.items())
    ]


def main():
    _init_com()
    env = _read_env()
    login_id = env.get("capital_api_key") or env.get("capital_id")
    password = env.get("capital_password")
    if not login_id or not password:
        _write_snapshot({"ok": False, "error": "capital_api_key/capital_password missing in .env"})
        sys.exit(1)

    center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
    reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)
    order = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)
    handler = Events()
    _reply_h = comtypes.client.GetEvents(reply, handler)
    _order_h = comtypes.client.GetEvents(order, handler)

    code = center.SKCenterLib_Login(login_id, password)
    if code not in (0, 2003):
        msg = center.SKCenterLib_GetReturnCodeMessage(code)
        _write_snapshot({"ok": False, "error": f"login failed code={code} {msg}"})
        time.sleep(30)  # don't crash-loop hot on a bad password
        sys.exit(1)
    _log(f"login ok ({code})")

    if (rc := order.SKOrderLib_Initialize()) != 0:
        _write_snapshot({"ok": False, "error": f"SKOrderLib_Initialize code={rc}"})
        sys.exit(1)
    if (rc := order.ReadCertByID(login_id)) != 0:
        _write_snapshot({"ok": False, "error": f"ReadCertByID code={rc} (cert/identity issue — see 602 notes)"})
        sys.exit(1)

    Events.futures_accounts, Events.stock_accounts = [], []
    if (rc := order.GetUserAccount()) != 0:
        _write_snapshot({"ok": False, "error": f"GetUserAccount code={rc}"})
        time.sleep(30)
        sys.exit(1)
    # One OnAccount event fires per account — the first arrival proves nothing
    # about the OTHER market's account. Keep pumping a grace window after the
    # first event or a late TS/TF row is silently dropped for the whole
    # process lifetime (audit P1-2).
    _pump_until(lambda: bool(Events.futures_accounts or Events.stock_accounts))
    grace = time.time() + 2
    while time.time() < grace:
        pythoncom.PumpWaitingMessages()
        time.sleep(0.05)
    tf = Events.futures_accounts[0] if Events.futures_accounts else None
    ts = Events.stock_accounts[0] if Events.stock_accounts else None
    _log(f"accounts TF={tf} TS={ts}")
    if not tf and not ts:
        _write_snapshot({"ok": False, "error": "GetUserAccount returned no TF/TS accounts"})
        sys.exit(1)

    while True:
        # heartbeat for manager/healthcheck.py — a stale file means this daemon died
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.touch()

        try:
            # equity None = securities-only account (no TF) — written explicitly
            # so lib/account_capital.py can raise a READABLE error instead of
            # KeyError (audit P1-1). Securities valuation is a known gap.
            snap = {"ok": True, "error": None, "positions": [], "holdings": [],
                    "equity": None, "currency": "TWD", "accounts": None,
                    "available": None}
            if tf:
                rights = query_rights(order, login_id, tf)
                snap["equity"] = rights["equity"]
                snap["currency"] = rights["currency"]
                snap["accounts"] = {"futures": rights["equity"]}
                snap["available"] = rights["available"]
                snap["positions"] = query_open_interest(order, login_id, tf)
            if ts:
                snap["holdings"] = query_balance(order, login_id, ts)
            _write_snapshot(snap)
            _log(f"snapshot ok equity={snap.get('equity')} {snap.get('currency')} "
                 f"pos={len(snap['positions'])} hold={len(snap['holdings'])}")
        except QueryInProgress as e:
            # Transient rate-limit — the early-tick path (audit P1) can bunch
            # queries closer than the 60s cadence the manual measured safe.
            # Skip this tick WITHOUT touching the snapshot: the last good one
            # (written by a prior tick) stays on disk and stays correct, and
            # we don't tear down the COM session over a retry-worthy blip.
            _log(f"tick skipped (rate-limited): {e}")
        except Exception as e:
            _write_snapshot({"ok": False, "error": f"{type(e).__name__}: {e}"})
            _log(f"tick failed: {e}")
            time.sleep(30)  # broker outage must not become a 1.5s relogin storm
            sys.exit(1)  # NSSM restarts us with a fresh COM session

        # Sleep in small slices, early-ticking when an order just went out
        # (REFRESH_FLAG touched by lib/order_capital) so fills hit the snapshot
        # fast. MIN_TICK_SPACING_S keeps a floor under back-to-back orders —
        # the flag stays put and is consumed on the next slice after the floor.
        slept = 0
        while slept < POLL_S:
            time.sleep(REFRESH_CHECK_S)
            slept += REFRESH_CHECK_S
            if slept >= MIN_TICK_SPACING_S and os.path.exists(REFRESH_FLAG):
                try:
                    os.remove(REFRESH_FLAG)
                except OSError:
                    pass
                _log("refresh flag -> early tick")  # ASCII only: log rides cp950 console redirects
                break


if __name__ == "__main__":
    main()
