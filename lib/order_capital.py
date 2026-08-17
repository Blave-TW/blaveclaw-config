"""
Capital (群益) Taiwan order execution library — SKCOM COM API, Windows only.

One login covers BOTH markets (same certificate): TAIFEX index futures
(TX00 / MTX00 / TM0000 market orders) and TWSE/TPEx securities (whole-lot
limit/market + intraday odd-lot limit). Taiwan-broker shape follows
lib/order_sinopac.py, NOT lib/order_TEMPLATE.py's perp contract. Companion
references: references/capital-broker.md (structs, report field tables, error
codes — read it before any Capital work) and references/manager.md § Capital
broker exception (service identity).

EXECUTION IDENTITY CONSTRAINT (error 602) — READ BEFORE RUNNING:
SKCOM binds the certificate to the Windows identity that ISSUED it (always
Administrator on Blave Agent machines) and additionally requires a PASSWORD
LOGON: the same code succeeds under NSSM `ObjectName .\\Administrator <pwd>`
or `schtasks /ru Administrator /rp <pwd>`, and fails with 602 under
LocalSystem or a key-auth SSH session (DPAPI cannot unlock the cert's private
key without password-derived credentials — field-verified, see
capital-broker.md Step 2). NEVER call this module from the agent gateway
shell or SSH; route through the NSSM reconciler service or a one-shot
schtasks vehicle (references/manager.md has the exact commands).

Everything below was live-verified 2026-08-14 (uid=1 real account: TM0000
futures round trip, whole-lot + intraday odd-lot stock fills). Design rules:

1. TESTED PATHS ONLY. Cancel (刪單), modify (改價/改量), stop orders, and
   after-hours odd lot (盤後零股, SendStockOddLotOrder) were NOT live-tested
   and raise NotImplementedError — extend only after a user-approved live
   verification, never from the manual alone.
2. UNITS DIFFER PER MARKET. Futures qty is 口數 (lots); whole-lot stock qty
   is 張 (1 lot = 1000 shares, and fill reports come back in SHARES); odd-lot
   qty is raw shares (1–999). All prices/amounts are TWD.
3. SENT ≠ FILLED. ncode 0 + a 13-digit sequence number means the broker
   ACCEPTED the order. Fill confirmation arrives async via OnNewData `D` rows
   matched on our sequence number (idx 45 — idx 0 is empty on futures D
   rows). Confirmation timeout returns status 'sent', not an error: a resting
   ROD limit order is normal, and futures IOC may still have filled — query
   reports before resubmitting.
4. REPORT REPLAY + DUPLICATES. SKReplyLib_ConnectByID replays every report
   of the session day on each connect, and TS rows arrive duplicated —
   every OnNewData row is deduped on (KeyNo idx45, type idx2, exchange seq
   idx30) before use.
5. ALIAS vs RESOLVED CODE. Orders are sent with the near-month alias
   (TM0000), but every report/position carries the RESOLVED contract
   (e.g. TM2608) — reconcile on the resolved code, never the alias.
6. HALTABLE + AUDITED. Entry orders check lib/guard's state/HALT before any
   COM call (audit 'order_denied_halt'); closes/sells always pass. Every
   accepted order audits 'order_sent'; rejections 'order_error'; confirmed
   fills 'order_filled'. sNewClose=2 (auto) cannot express intent, so the
   futures function REQUIRES an explicit intent= argument (fail-closed).
7. NO BROKER ATTRIBUTION — 群益 is the broker itself; there is no header /
   client-order-id prefix / aff code to attach (unlike every exchange lib).

Credentials in .env: capital_api_key (身分證字號) + capital_password —
canonical names, see capital-broker.md Step 4 (legacy capital_id accepted).
COM runs single-threaded apartment: keep every call on the thread that made
the first one (the hand-wired TW reconciler is single-threaded — fine).
"""

import logging
import os
import threading
import time

from lib import guard

# Touched after every accepted order/confirmed fill: lib/capital_worker.py's
# sleep loop early-ticks on it so the account snapshot (and through the file
# watcher, the user's dashboard) reflects the trade in seconds instead of at
# the next 60s poll. Derived from __file__ (this module IS lib/order_capital.py
# in the same workspace as capital_worker.py), not the BLAVE_AGENT_WORKSPACE
# env var — see capital_worker.py's REFRESH_FLAG comment (audit P2-2).
_REFRESH_FLAG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "state", "capital_refresh")


def _request_snapshot_refresh():
    try:
        with open(_REFRESH_FLAG, "w"):
            pass
    except OSError:
        pass  # refresh is best-effort; the 60s poll still covers it

# Near-month aliases — the only futures symbols live-tested; month codes like
# TX03 auto-roll surprisingly (see capital-broker.md) and stay unsupported.
FUTURES_ALIASES = {"TX00", "MTX00", "TM0000"}

SKCOM_DLL = r"C:\skcom\x64\SKCOM.dll"

# COM is Windows-only; deferred so the module still imports for structure
# checks on non-Windows machines (comtypes fails to import there).
comtypes = None
pythoncom = None
sk = None

_session = None
_lock = threading.Lock()


class CapitalError(Exception):
    """Raised when SKCOM rejects an order/call, with the broker's message."""


def _init_com():
    global comtypes, pythoncom, sk
    if sk is not None:
        return
    import comtypes as _comtypes
    import comtypes.client
    import pythoncom as _pythoncom

    _comtypes.client.GetModule(SKCOM_DLL)
    import comtypes.gen.SKCOMLib as _sk

    comtypes, pythoncom, sk = _comtypes, _pythoncom, _sk


class _Events:
    def __init__(self):
        self.futures_accounts = []
        self.stock_accounts = []
        self.seen = set()   # (KeyNo idx45, type idx2, exchange seq idx30)
        self.fills = {}     # seq_no -> [fill dicts]

    def OnReplyMessage(self, bstrUserID, bstrMessages):
        return -1  # MUST return -1, registered BEFORE login (else code 2017)

    def OnAccount(self, bstrLogInID, bstrAccountData):
        f = bstrAccountData.split(",")
        acct = f[1] + f[3]
        if f[0] == "TF":
            self.futures_accounts.append(acct)
        elif f[0] == "TS":
            self.stock_accounts.append(acct)

    def OnNewData(self, bstrUserID, bstrData):
        f = bstrData.split(",")
        if len(f) < 46:
            return
        key = (f[45], f[2], f[30])
        if key in self.seen:  # session-day replay + duplicated TS rows
            return
        self.seen.add(key)
        if f[2] == "D" and f[45]:
            self.fills.setdefault(f[45], []).append({
                "market": f[1],                                   # TF / TS / TC
                "symbol": f[8],                                   # RESOLVED contract / ticker
                "price": float(f[11]) if f[11] else 0.0,
                "qty": float(f[20]) if f[20] else 0.0,            # lots (TF) / SHARES (TS, TC)
                "fill_id": f[38],                                 # 成交編號
            })


class _Session:
    __slots__ = ("center", "reply", "order", "events", "login_id",
                 "futures_account", "stock_account", "_conns")


def _pump_until(cond, timeout):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        pythoncom.PumpWaitingMessages()
        time.sleep(0.05)
    return cond()


def _get_session(env):
    """Process-wide singleton: one login + report connection, reused. Fails
    loudly at connect time so the 602 identity trap is hit before any order."""
    global _session
    with _lock:
        if _session is not None:
            return _session

        _init_com()
        login_id = env.get("capital_api_key") or env.get("capital_id")
        password = env.get("capital_password")
        if not login_id or not password:
            raise ValueError("capital_api_key / capital_password missing from .env")

        center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
        reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)
        order = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)
        events = _Events()
        conns = [comtypes.client.GetEvents(reply, events),
                 comtypes.client.GetEvents(order, events)]

        code = center.SKCenterLib_Login(login_id, password)
        if code not in (0, 2003):
            msg = center.SKCenterLib_GetReturnCodeMessage(code)
            raise CapitalError(
                f"login failed code={code} {msg}"
                + (" — 602 means this process is NOT under a password-logon Administrator "
                   "session (NSSM ObjectName / schtasks); see capital-broker.md Step 2"
                   if code == 602 else ""))

        if (rc := order.SKOrderLib_Initialize()) != 0:
            raise CapitalError(f"SKOrderLib_Initialize code={rc}")
        if (rc := order.ReadCertByID(login_id)) != 0:
            raise CapitalError(f"ReadCertByID code={rc} (cert/identity — see 602 notes)")

        if (rc := order.GetUserAccount()) != 0:
            raise CapitalError(f"GetUserAccount code={rc}")
        # One OnAccount per account; pump a grace window after the first so a
        # late TS/TF row isn't dropped for the process lifetime.
        _pump_until(lambda: bool(events.futures_accounts or events.stock_accounts), 10)
        grace = time.time() + 2
        while time.time() < grace:
            pythoncom.PumpWaitingMessages()
            time.sleep(0.05)
        if not events.futures_accounts and not events.stock_accounts:
            raise CapitalError("GetUserAccount returned no TF/TS accounts — declaration "
                               "not signed, or no account of that market exists")

        # Order/fill reports; connect replays the whole session day (deduped
        # in _Events). 0 = success.
        if (rc := reply.SKReplyLib_ConnectByID(login_id)) != 0:
            raise CapitalError(f"SKReplyLib_ConnectByID code={rc}")

        s = _Session()
        s.center, s.reply, s.order, s.events = center, reply, order, events
        s.login_id = login_id
        s.futures_account = events.futures_accounts[0] if events.futures_accounts else None
        s.stock_account = events.stock_accounts[0] if events.stock_accounts else None
        s._conns = conns
        logging.info(f"capital login OK: TF={s.futures_account} TS={s.stock_account}")
        _session = s
        return _session


def reset_session():
    """Drop the singleton so the next call re-logs-in."""
    global _session
    with _lock:
        _session = None


# ── order send + confirmation ────────────────────────────────────────────────

def _send(sess, send_fn, fields):
    """Common send path: ncode check, audit, return the 13-digit seq_no."""
    msg, ncode = send_fn()
    if ncode != 0:
        err = sess.center.SKCenterLib_GetReturnCodeMessage(ncode)
        guard.audit("order_error", code=ncode, error=err, **fields)
        raise CapitalError(f"order rejected code={ncode}: {err} (msg={msg!r})")
    seq_no = str(msg).strip()
    guard.audit("order_sent", seq_no=seq_no, **fields)
    _request_snapshot_refresh()
    return seq_no


def _await_fill(sess, seq_no, sent_symbol, timeout):
    """Pump OnNewData for `D` rows carrying our seq_no. Returns 'filled' with
    aggregated exchange-reported qty/price, or 'sent' at timeout — the order
    was ACCEPTED (13-digit seq) and may fill later (resting ROD limit) or may
    already have filled unseen; query reports, never blindly resubmit."""
    deadline = time.time() + timeout
    first_fill_at = None
    while time.time() < deadline:
        pythoncom.PumpWaitingMessages()
        if sess.events.fills.get(seq_no):
            if first_fill_at is None:
                first_fill_at = time.time()
            elif time.time() - first_fill_at >= 1.0:
                break  # 1s grace so multi-row partial fills aggregate
        time.sleep(0.05)

    rows = sess.events.fills.get(seq_no, [])
    if not rows:
        return {"seq_no": seq_no, "status": "sent", "symbol": sent_symbol,
                "fill_qty": 0.0, "avg_fill_price": 0.0, "fill_ids": [], "market": None}
    qty = sum(r["qty"] for r in rows)
    avg = sum(r["price"] * r["qty"] for r in rows) / qty if qty else 0.0
    return {"seq_no": seq_no, "status": "filled",
            "symbol": rows[0]["symbol"],       # resolved contract (TM2608), not the alias
            "fill_qty": qty,                   # lots (futures) / SHARES (stocks, both periods)
            "avg_fill_price": avg,
            "fill_ids": [r["fill_id"] for r in rows],
            "market": rows[0]["market"]}


def _finish(sess, seq_no, symbol, timeout, fields):
    result = _await_fill(sess, seq_no, symbol, timeout)
    if result["status"] == "filled":
        guard.audit("order_filled", seq_no=seq_no, fill_qty=result["fill_qty"],
                    avg_fill_price=result["avg_fill_price"],
                    resolved_symbol=result["symbol"], **fields)
        _request_snapshot_refresh()  # fill may land after the send-time tick
    logging.info(
        f"capital {fields['action']} {symbol} {fields['qty']} {fields['unit']} → "
        f"{result['status']} filled={result['fill_qty']} avg={result['avg_fill_price']:.2f}")
    return result


def _check_halt(fields):
    if fields["intent"] == "entry" and guard.halted():
        guard.audit("order_denied_halt", **fields)
        raise guard.Halted(
            f"state/HALT is set ({guard.halt_info()}) — entry order for "
            f"{fields['symbol']} refused before reaching the broker. Closes/sells "
            f"still work. Only the user may clear the halt (guard.clear_halt).")


# ── futures ──────────────────────────────────────────────────────────────────

def place_futures_market_order(env, symbol, action, lots, intent, confirm_timeout=15):
    """One futures market order: IOC + price 'M', sNewClose=2 (auto new/close)
    — the exact combination live-verified 2026-08-14 (TM0000 round trip).

    symbol: near-month alias only ('TX00' 大台 / 'MTX00' 小台 / 'TM0000' 微台).
    action: 'buy'|'sell'. lots: 口數 (int ≥ 1).
    intent: REQUIRED, 'entry'|'reduce' — sNewClose=2 can't tell opening from
    closing, so the caller must declare it: 'entry' is blocked under HALT,
    'reduce' (flattening) always passes. Declaring a real entry as 'reduce'
    defeats the kill switch — don't.

    Returns _await_fill()'s dict; 'filled' carries the RESOLVED contract
    (e.g. TM2608) in 'symbol' — reconcile against that, not the alias."""
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")
    if intent not in ("entry", "reduce"):
        raise ValueError(f"intent must be 'entry' or 'reduce', got {intent!r}")
    symbol = str(symbol).upper()
    if symbol not in FUTURES_ALIASES:
        raise ValueError(f"symbol must be a near-month alias {sorted(FUTURES_ALIASES)}, "
                         f"got {symbol!r} — month-specific codes are untested here")
    lots = int(lots)
    if lots < 1:
        raise ValueError(f"lots must be >= 1, got {lots}")

    fields = {"venue": "capital", "market": "futures", "symbol": symbol,
              "action": action, "qty": lots, "unit": "lots", "intent": intent}
    _check_halt(fields)

    sess = _get_session(env)
    if not sess.futures_account:
        raise CapitalError("no TF futures account on this login")

    p = sk.FUTUREORDER()
    p.bstrFullAccount = sess.futures_account
    p.bstrStockNo = symbol
    p.sBuySell = 0 if action == "buy" else 1
    p.sTradeType = 1        # IOC — 'M' market price is only valid with IOC/FOK
    p.bstrPrice = "M"
    p.nQty = lots
    p.sNewClose = 2         # auto 新倉/平倉 — the live-tested setting
    p.sDayTrade = 0
    p.sReserved = 0

    seq_no = _send(sess, lambda: sess.order.SendFutureOrderCLR(sess.login_id, False, p), fields)
    return _finish(sess, seq_no, symbol, confirm_timeout, fields)


# ── securities ───────────────────────────────────────────────────────────────

def place_stock_order(env, symbol, action, lots, price=None, confirm_timeout=15):
    """One whole-lot (整股, sPeriod=0) stock order, live-verified 2026-08-14.

    lots: 張數 (1 張 = 1000 shares) — fill reports come back in SHARES.
    price: numeric limit price (nSpecialTradeType=2) or None for market
    (nSpecialTradeType=1, bstrPrice '0'). ROD in both cases; a limit order
    resting unfilled returns status 'sent' at timeout — that is not a failure.
    Buys are entries (HALT-blocked); sells reduce a long-only cash account
    (sFlag=0 現股) and always pass.

    T+2 settlement is NOT pre-checked by the broker (live-verified): an
    unfunded account's buy still fills — remind the user to fund by T+2."""
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")
    lots = int(lots)
    if lots < 1:
        raise ValueError(f"lots must be >= 1, got {lots}")

    fields = {"venue": "capital", "market": "stock", "symbol": symbol,
              "action": action, "qty": lots, "unit": "lots_1000sh",
              "price": price if price is not None else "market",
              "intent": "entry" if action == "buy" else "reduce"}
    _check_halt(fields)

    sess = _get_session(env)
    if not sess.stock_account:
        raise CapitalError("no TS securities account on this login")

    p = sk.STOCKORDER()
    p.bstrFullAccount = sess.stock_account
    p.bstrStockNo = str(symbol)
    p.sPrime = 0            # 上市上櫃
    p.sPeriod = 0           # 盤中
    p.sFlag = 0             # 現股
    p.sBuySell = 0 if action == "buy" else 1
    p.nQty = lots
    p.nTradeType = 0        # ROD
    if price is None:
        p.nSpecialTradeType = 1   # 市價
        p.bstrPrice = "0"
    else:
        p.nSpecialTradeType = 2   # 限價
        p.bstrPrice = str(price)

    # NOT SendStockOrderCLR — the CLR suffix is futures-only (capital-broker.md).
    seq_no = _send(sess, lambda: sess.order.SendStockOrder(sess.login_id, False, p), fields)
    return _finish(sess, seq_no, str(symbol), confirm_timeout, fields)


def place_odd_lot_order(env, symbol, action, shares, price, confirm_timeout=15):
    """One intraday odd-lot (盤中零股, sPeriod=4) LIMIT order, live-verified
    2026-08-14. shares: 1–999. Odd lots are limit-only, so price is required.

    sPeriod=4 needs the FULL struct — omitting nSpecialTradeType rejects with
    1067 (live-measured); nTradeType=0 ROD + nSpecialTradeType=2 限價.
    Matching runs every 5s from 09:10; unfilled orders do NOT carry over to
    the 盤後零股 window. Reports come back under market type 'TC', not 'TS'.
    Buys are entries (HALT-blocked); sells always pass."""
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")
    shares = int(shares)
    if not 1 <= shares <= 999:
        raise ValueError(f"odd-lot order must be 1-999 shares, got {shares}")
    if price is None:
        raise ValueError("odd-lot orders are limit-only — price is required")

    fields = {"venue": "capital", "market": "stock_odd_lot", "symbol": symbol,
              "action": action, "qty": shares, "unit": "shares", "price": price,
              "intent": "entry" if action == "buy" else "reduce"}
    _check_halt(fields)

    sess = _get_session(env)
    if not sess.stock_account:
        raise CapitalError("no TS securities account on this login")

    p = sk.STOCKORDER()
    p.bstrFullAccount = sess.stock_account
    p.bstrStockNo = str(symbol)
    p.sPrime = 0
    p.sPeriod = 4           # 盤中零股
    p.sFlag = 0
    p.sBuySell = 0 if action == "buy" else 1
    p.nQty = shares         # raw shares for odd lot
    p.nTradeType = 0        # ROD
    p.nSpecialTradeType = 2  # 限價 — required even for sPeriod=4 (error 1067)
    p.bstrPrice = str(price)

    seq_no = _send(sess, lambda: sess.order.SendStockOrder(sess.login_id, False, p), fields)
    return _finish(sess, seq_no, str(symbol), confirm_timeout, fields)


# ── generic close-all support (manager/flatten.py, order_TEMPLATE.py contract) ──
# flatten() calls format_qty()/close_position_partial() on EVERY venue's order
# lib generically (it doesn't distinguish auto-wired crypto from hand-wired TW
# brokers) — added 2026-08-14 after a real close-all attempt on a live 1-lot
# TM2608 position hit AttributeError on both (live-verified: this is what
# actually blocks 「全部出場」, not a hypothetical). Both are thin wrappers
# around place_futures_market_order — no new COM calls, no new untested paths.

# resolved contract prefix (as account_capital.get_positions() reports it,
# e.g. "TM2608") -> the near-month alias place_futures_market_order takes.
# Mirrors manager/reconciler.py's _CAPITAL_FUTURES_SPEC resolved_prefix table
# — keep the two in sync if either changes.
_RESOLVED_PREFIX_TO_ALIAS = {"TM": "TM0000", "MTX": "MTX00", "TX": "TX00"}


def _alias_for_resolved(symbol):
    resolved = str(symbol).upper()
    for prefix, alias in _RESOLVED_PREFIX_TO_ALIAS.items():
        if resolved.startswith(prefix):
            return alias
    raise ValueError(f"capital: {symbol!r} does not match any known TW futures "
                     f"resolved-contract prefix {sorted(_RESOLVED_PREFIX_TO_ALIAS)} — "
                     f"securities close-all is not wired")


def format_qty(env: dict, symbol: str, qty: float, price: float = None) -> str:
    """Futures lots are always whole numbers — no venue-specific step/tick
    table needed (unlike crypto). Raises ValueError (flatten()'s dust-skip
    signal) for anything below 1 lot; never silently floors to 0."""
    lots = float(qty)
    if lots < 1:
        raise ValueError(f"capital: {qty} lots is below the 1-lot minimum")
    return str(int(round(lots)))


def close_position_partial(env: dict, symbol: str, direction: str, qty: float,
                           client_order_id: str = None):
    """Reduce-only close, order_TEMPLATE.py's contract. `symbol` here is the
    RESOLVED contract account_capital.get_positions() reports (e.g. "TM2608"),
    not the alias — translate before calling place_futures_market_order,
    which only accepts near-month aliases. `direction` is the position being
    closed ('long'/'short'); the closing order is the opposite action."""
    alias = _alias_for_resolved(symbol)
    action = "sell" if direction == "long" else "buy"
    lots = int(round(float(qty)))
    if lots < 1:
        raise ValueError(f"capital: {qty} lots is below the 1-lot minimum")
    result = place_futures_market_order(env, alias, action, lots, intent="reduce")
    return {
        "avg_price": result.get("avg_fill_price") or 0.0,
        "executed_qty": result.get("fill_qty") or 0.0,
        "exchange": "capital",
        "resolved_symbol": result.get("symbol"),
        "status": result.get("status"),
    }


# ── not live-verified — deliberately unimplemented ───────────────────────────

_NOT_VERIFIED = ("not live-verified on Capital — do not guess the COM call from the "
                 "manual; run a user-approved live test first (capital-broker.md "
                 "§ Limits & Gotchas: no confirmed simulation environment), then "
                 "implement from the measured behavior.")


def cancel_order(*_args, **_kwargs):
    """刪單."""
    raise NotImplementedError(f"cancel_order is {_NOT_VERIFIED}")


def modify_order(*_args, **_kwargs):
    """改價/改量."""
    raise NotImplementedError(f"modify_order is {_NOT_VERIFIED}")


def place_stop_order(*_args, **_kwargs):
    """停損/移動停損單."""
    raise NotImplementedError(f"place_stop_order is {_NOT_VERIFIED}")


def place_after_hours_odd_lot_order(*_args, **_kwargs):
    """盤後零股 (13:40–14:30, SendStockOddLotOrder — same signature per the
    manual, but untested)."""
    raise NotImplementedError(f"place_after_hours_odd_lot_order is {_NOT_VERIFIED}")
