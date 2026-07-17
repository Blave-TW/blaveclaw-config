"""
SinoPac (永豐金證券) Taiwan stock order execution library — Shioaji 1.5.4.

Companion to references/sinopac-broker.md (account setup / CA certificate).
Credentials in .env: sinopac_api_key, sinopac_secret_key, sinopac_ca_path,
sinopac_ca_passwd, optional sinopac_person_id. Set SINOPAC_LIVE=true for real
orders — without it Shioaji runs in simulation mode against a FAKE account
whose holdings have nothing to do with the user's real account.

Harvested from the first live Taiwan-stock deployment (fleet uid 18198,
2026-07), which lost three rounds of orders to SILENT rejections before the
combination below was proven live. Design rules:

1. FAIL LOUD. Shioaji's place_order does NOT raise on rejection — a rejected
   order just sits with status Failed (or never leaves PendingSubmit). Every
   order here is polled via update_status until the exchange acknowledges it;
   Failed/Cancelled/Inactive raises SinopacError with the broker's message.
2. ENUMS, NEVER STRINGS. price_type=sj.StockPriceType.MKT,
   order_type=sj.OrderType.ROD, order_lot=sj.StockOrderLot.Odd. Passing the
   bare string "MKT" as order_type was silently rejected in the field.
3. ODD LOT FOR <1000 SHARES. A Common-lot (整股) order whose quantity is a
   share count silently vanishes: Common-lot quantity is in 張 (1000-share
   board lots). Everything here trades odd lot (max 999 shares per order;
   larger sizes are split into multiple odd-lot orders).
4. IDEMPOTENT-ISH. Pass client_tag (≤6 alphanumeric chars — Shioaji's
   custom_field) unique per intent; a resubmit with the same tag the same day
   is refused locally before reaching the exchange. Weaker than an
   exchange-side check, but Shioaji has no clientOrderId-style dedup.
5. HALTABLE + AUDITED. Buy (entry) orders check lib/guard's state/HALT before
   any network call and every attempt/outcome is appended to
   state/audit.jsonl. Sells (reduce-only for a long-only stock account) are
   never trapped.

Field-verified quirks (all cost real debugging on a live account):
- CA must be activated AFTER login (api.activate_ca), and only exists in live
  mode; orders before activation fail with "CA not activated for: <person>".
- sinopac_person_id defaults to sinopac_ca_passwd: SinoPac issues CA
  certificates with the national ID as the default password, so for most
  users they are the same string.
- Warning-flagged stocks (警示股, e.g. "警示股預收條件") may require
  pre-funded settlement — surface status msg to the user, don't retry.
- Orders placed outside 09:00-13:30 TWN rest as PendingSubmit/PreSubmitted
  until the next session; that is not a rejection.
- One Shioaji connection per account; login with fetch_contract=True takes
  5-10 s. This module keeps a process-wide singleton.
"""

import logging
import re
import threading
import time

import shioaji as sj

from lib import guard

# Odd-lot orders carry at most 999 shares (1000 = one board lot).
MAX_ODD_LOT_SHARES = 999

# Terminal-failure statuses. Filled is terminal-success; Submitted/
# PreSubmitted mean the exchange accepted the order (it may rest, e.g. placed
# after hours); PendingSubmit means the broker has NOT acknowledged it yet.
FAILED_STATUSES = {"Failed", "Cancelled", "Inactive"}

_api = None
_account = None
_simulation = None
_lock = threading.Lock()


class SinopacError(Exception):
    """Raised when Shioaji reports a failed order or an unexpected state."""


class OrderNotConfirmed(Exception):
    """Order was sent but never acknowledged by the exchange within timeout.
    It MAY still go through — query again before resubmitting."""


class DuplicateOrder(Exception):
    """An order with the same client_tag already exists today — refused
    locally so a resubmit cannot double the position."""


# ── connection ───────────────────────────────────────────────────────────────

def _get_api(env):
    """Thread-safe singleton. Logs in, and in live mode activates the CA
    certificate — raising immediately if activation fails, so the CA trap is
    hit at connect time instead of surfacing as a rejected first order."""
    global _api, _account, _simulation

    with _lock:
        if _api is not None:
            return _api, _account

        _simulation = str(env.get("SINOPAC_LIVE", "")).lower() != "true"
        api_key = env.get("sinopac_api_key", "")
        secret_key = env.get("sinopac_secret_key", "")
        if not api_key or not secret_key:
            raise ValueError("sinopac_api_key / sinopac_secret_key missing from .env")

        api = sj.Shioaji(simulation=_simulation)
        accounts = api.login(api_key=api_key, secret_key=secret_key, fetch_contract=True)

        if not _simulation:
            ca_path = env.get("sinopac_ca_path", "")
            ca_passwd = env.get("sinopac_ca_passwd", "")
            # SinoPac CA passwords default to the national ID → usable as person_id
            person_id = env.get("sinopac_person_id", "") or ca_passwd
            if not ca_path or not ca_passwd:
                raise SinopacError(
                    "SINOPAC_LIVE=true but sinopac_ca_path / sinopac_ca_passwd missing — "
                    "live orders are impossible without the CA certificate"
                )
            try:
                ok = api.activate_ca(ca_path=ca_path, ca_passwd=ca_passwd, person_id=person_id)
            except Exception as e:
                raise SinopacError(f"CA activation failed: {e}") from e
            if not ok:
                raise SinopacError(
                    "CA activation returned falsy — certificate not usable. The user may "
                    "still need to activate it in the SinoPac desktop app (e-Leader/iLeader)."
                )

        account = None
        for acct in accounts:
            if str(getattr(acct, "account_type", "")) == "S":
                account = acct
                break
        if account is None:
            raise SinopacError("no stock account on this login — check e-MANAGER permissions")

        logging.info(f"Sinopac login OK: {account} (simulation={_simulation})")
        _api, _account = api, account
        return _api, _account


def reset_connection():
    """Drop the singleton so the next call re-logs-in (e.g. after changing
    SINOPAC_LIVE in .env)."""
    global _api, _account
    with _lock:
        if _api is not None:
            try:
                _api.logout()
            except Exception:
                pass
        _api = None
        _account = None


# ── account info ─────────────────────────────────────────────────────────────

def get_account_balance(env):
    """Stock account settlement balance in TWD. Raises on failure — a silent
    0.0 would make the reconciler think the account is empty."""
    api, account = _get_api(env)
    balance = api.account_balance(account)
    if balance is None:
        raise SinopacError("account_balance returned None")
    row = balance[0] if isinstance(balance, (list, tuple)) else balance
    return float(row.acc_balance)


def get_sinopac_positions(env):
    """{symbol: {'side': 'long', 'size': value_in_TWD}} — reconciler shape.
    unit=Share so odd-lot holdings are counted in shares, not board lots."""
    api, account = _get_api(env)
    positions = {}
    for item in api.list_positions(account, unit=sj.Unit.Share):
        symbol = str(item.code)
        qty = float(item.quantity)
        if not symbol or qty <= 0:
            continue
        price = _last_price(api, symbol)
        if price > 0:
            positions[symbol] = {"side": "long", "size": qty * price}
    return positions


def _last_price(api, symbol):
    contract = api.Contracts.Stocks[symbol]
    if contract is None:
        logging.warning(f"sinopac: symbol {symbol} not found in contracts")
        return 0.0
    snap = api.snapshots([contract])
    return float(snap[0].close) if snap else 0.0


# ── order status helpers ─────────────────────────────────────────────────────

def _normalize(trade):
    st = trade.status
    deals = list(getattr(st, "deals", None) or [])
    filled = sum(float(d.quantity) for d in deals)
    avg = (
        sum(float(d.price) * float(d.quantity) for d in deals) / filled if filled else 0.0
    )
    return {
        "seqno": str(trade.order.seqno),
        "status": str(st.status),
        "filled_qty": filled,
        "order_qty": float(getattr(st, "order_quantity", None) or trade.order.quantity),
        "avg_fill_price": avg,
        "msg": str(getattr(st, "msg", "") or ""),
        "simulation": bool(_simulation),
    }


def _refresh(api, account, trade):
    """update_status mutates trades in place; re-find ours by seqno to be safe."""
    api.update_status(account)
    seqno = str(trade.order.seqno)
    for t in api.list_trades():
        if str(t.order.seqno) == seqno:
            return t
    return trade


def confirm_order(env, trade, timeout=90):
    """Poll a trade until the exchange acknowledges it. Returns the normalized
    dict once status is Filled OR accepted-and-resting (Submitted/PreSubmitted/
    PartFilled after the fill wait). Raises SinopacError on Failed/Cancelled/
    Inactive (with the broker's msg — this is the silent-rejection killer),
    OrderNotConfirmed if still PendingSubmit at timeout."""
    api, account = _get_api(env)
    deadline = time.time() + timeout
    result = None
    while time.time() < deadline:
        trade = _refresh(api, account, trade)
        result = _normalize(trade)
        status = result["status"]
        if status in FAILED_STATUSES:
            raise SinopacError(
                f"order {result['seqno']} {status}: {result['msg'] or '(no msg from broker)'}"
            )
        if status == "Filled":
            return result
        # Accepted but not filled — keep polling for a fill until timeout,
        # then return the resting order honestly (normal outside 09:00-13:30).
        time.sleep(2)
    if result is None or result["status"] == "PendingSubmit":
        raise OrderNotConfirmed(
            f"order still {result['status'] if result else 'UNKNOWN'} after {timeout}s — "
            f"not acknowledged by the exchange; query again before resubmitting"
        )
    return result


def _check_duplicate(api, account, symbol, client_tag):
    api.update_status(account)
    for t in api.list_trades():
        if (
            str(getattr(t.order, "custom_field", "") or "") == client_tag
            and str(t.contract.code) == symbol
            and str(t.status.status) not in FAILED_STATUSES
        ):
            raise DuplicateOrder(
                f"order with client_tag={client_tag} for {symbol} already exists today "
                f"(seqno {t.order.seqno}, status {t.status.status}) — refusing resubmit"
            )


# ── order placement ──────────────────────────────────────────────────────────

def place_odd_lot_order(env, symbol, action, shares, client_tag=None, confirm_timeout=90):
    """One odd-lot (零股) market order, CONFIRMED. action: 'buy'|'sell',
    1 ≤ shares ≤ 999. Returns confirm_order()'s normalized dict.

    client_tag: ≤6 alphanumeric chars, unique per intent (e.g. 'twm107' =
    strategy initials + day-of-year) — the same tag resubmitted the same day
    is refused locally (DuplicateOrder) instead of doubling the position."""
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")
    shares = int(shares)
    if not 1 <= shares <= MAX_ODD_LOT_SHARES:
        raise ValueError(f"odd-lot order must be 1-{MAX_ODD_LOT_SHARES} shares, got {shares}")
    if client_tag is not None and not re.fullmatch(r"[A-Za-z0-9]{1,6}", client_tag):
        raise ValueError(f"client_tag must be 1-6 alphanumeric chars, got {client_tag!r}")

    api, account = _get_api(env)
    contract = api.Contracts.Stocks[symbol]
    if contract is None:
        raise SinopacError(f"symbol {symbol} not found in contracts")

    fields = {
        "symbol": symbol, "action": action, "shares": shares,
        "lot": "Odd", "price_type": "MKT",
        "intent": "entry" if action == "buy" else "reduce",
        "simulation": bool(_simulation),
        **({"client_tag": client_tag} if client_tag else {}),
    }
    if action == "buy" and guard.halted():
        guard.audit("order_denied_halt", **fields)
        raise guard.Halted(
            f"state/HALT is set ({guard.halt_info()}) — buy order for {symbol} refused "
            f"before reaching the broker. Sells still work. Only the user may clear "
            f"the halt (guard.clear_halt)."
        )
    if client_tag:
        _check_duplicate(api, account, symbol, client_tag)

    order = sj.Order(
        action=sj.Action.Buy if action == "buy" else sj.Action.Sell,
        price=0,
        quantity=shares,
        price_type=sj.StockPriceType.MKT,   # enum — the string "MKT" is rejected silently
        order_type=sj.OrderType.ROD,
        order_lot=sj.StockOrderLot.Odd,     # <1000 shares MUST be odd lot
        custom_field=client_tag,
        account=account,
    )
    guard.audit("order_attempt", **fields)
    try:
        trade = api.place_order(contract, order)
        result = confirm_order(env, trade, timeout=confirm_timeout)
    except Exception as e:
        guard.audit("order_error", error=str(e), **fields)
        raise
    guard.audit("order_ok", seqno=result["seqno"], status=result["status"],
                filled_qty=result["filled_qty"], **fields)
    logging.info(
        f"sinopac {action} {symbol} {shares} shares → {result['status']} "
        f"filled={result['filled_qty']} avg={result['avg_fill_price']:.2f}"
    )
    return result


def place_order_sinopac(env, symbol, signed_diff, client_tag=None, confirm_timeout=90):
    """Reconciler entry point. signed_diff is in TWD (>0 buy, <0 sell);
    shares = floor(|signed_diff| / last price), split into ≤999-share odd-lot
    orders. Returns a summary dict {'orders': [...], 'filled_qty', 'target_qty'},
    or False if the diff is below one share (skip, matching lib conventions).
    Chunk tags derive from client_tag: 'twm7' → 'twm7', 'twm7a', 'twm7b'…"""
    api, _ = _get_api(env)
    price = _last_price(api, symbol)
    if price <= 0:
        raise SinopacError(f"no valid last price for {symbol}")

    target_qty = int(abs(signed_diff) / price)
    if target_qty < 1:
        logging.info(
            f"sinopac skip {symbol}: diff {abs(signed_diff):.0f} TWD < 1 share @ {price:.2f}"
        )
        return False

    action = "buy" if signed_diff > 0 else "sell"
    results = []
    remaining = target_qty
    chunk_i = 0
    while remaining > 0:
        shares = min(remaining, MAX_ODD_LOT_SHARES)
        tag = None
        if client_tag:
            tag = client_tag if chunk_i == 0 else f"{client_tag[:5]}{chr(ord('a') + chunk_i - 1)}"
        results.append(
            place_odd_lot_order(env, symbol, action, shares,
                                client_tag=tag, confirm_timeout=confirm_timeout)
        )
        remaining -= shares
        chunk_i += 1

    return {
        "symbol": symbol,
        "action": action,
        "target_qty": target_qty,
        "filled_qty": sum(r["filled_qty"] for r in results),
        "orders": results,
    }
