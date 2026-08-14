# Account library for Capital (群益) — securities + futures, Windows only.
#
# UNLIKE every other account lib, this one never talks to the venue: SKCOM
# (COM) only works under a password-logon Windows identity, and the platform
# account_reader runs as LocalSystem — a direct call would fail with cert
# error 602. lib/capital_worker.py (NSSM service `blave-agent-capital`,
# runs as Administrator) polls the venue and writes
# state/capital_account.json; this module just reads that snapshot.
# See references/capital-broker.md for the 602 background and field tables.
import json
import os
import time

_SNAPSHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "state", "capital_account.json")
_STALE_S = 300  # worker cadence is 60s; 5 misses = stale


def _read_snapshot():
    try:
        with open(_SNAPSHOT) as f:
            snap = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            "capital snapshot missing — is the blave-agent-capital service running?")
    age = time.time() - snap.get("read_at", 0)
    if age > _STALE_S:
        raise RuntimeError(
            f"capital snapshot stale ({int(age)}s old) — blave-agent-capital service down?")
    if not snap.get("ok"):
        raise RuntimeError(f"capital worker error: {snap.get('error')}")
    return snap


def get_equity(env: dict) -> dict:
    snap = _read_snapshot()
    if snap.get("equity") is None:
        # Securities-only Capital account: no TF futures account, and
        # securities inventory valuation isn't implemented yet — a readable
        # error beats a silent 0 pretending to be equity.
        raise RuntimeError(
            "capital: no futures account on this ID — equity for "
            "securities-only accounts is not supported yet (holdings still "
            "listed; valuation pending)")
    out = {"equity": snap["equity"], "currency": snap.get("currency", "TWD")}
    if isinstance(snap.get("accounts"), dict):
        out["accounts"] = snap["accounts"]
    return out


def get_positions(env: dict) -> dict:
    """{symbol: {'side', 'size'}} — size in LOTS, not account currency:
    TWD notional needs a mark price the worker doesn't fetch. TW futures
    never enter the auto-wired reconciler (hand-wired per venue-onboarding),
    so lots-for-display is the honest value.

    'side' is 'long'/'short' (account_TEMPLATE.py's contract — every other
    venue's account lib uses this, and manager/flatten.py's close-all rejects
    anything else: 'bad position row (side=buy)', live-verified 2026-08-14,
    silently skipped the position instead of closing it). The worker's raw
    snapshot uses the exchange's own 'buy'/'sell' wording — normalize here,
    once, at the contract boundary.

    GetOpenInterest reports buy and sell as separate rows — net them per
    symbol or a hedged/day-trade book shows only whichever row came last."""
    snap = _read_snapshot()
    net = {}
    for p in snap.get("positions", []):
        lots = p.get("lots", 0.0) or 0.0
        net[p["symbol"]] = net.get(p["symbol"], 0.0) + (
            lots if p.get("side") == "buy" else -lots)
    return {
        sym: {"side": "long" if n > 0 else "short", "size": abs(n)}
        for sym, n in net.items() if n != 0
    }


def get_snapshot_read_at() -> float:
    """Unix seconds of the worker's most recent successful snapshot write.
    Used by manager/reconciler.py's Read-Your-Writes guard to detect a
    just-placed order the snapshot hasn't caught up to yet (worker cadence is
    60s). Reuses _read_snapshot()'s freshness/ok checks — a genuinely
    stale/dead worker raises the same RuntimeError get_positions() would."""
    return _read_snapshot()["read_at"]


def get_holdings(env: dict) -> list:
    """Securities inventory as display-only holdings (wallet='securities');
    usdt_value None — no on-machine price source, platform lists them unpriced
    rather than hiding them."""
    return _read_snapshot().get("holdings", [])
