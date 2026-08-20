"""
Trading guard: filesystem kill switch + append-only order audit log.

Why this exists: every safety rule in AGENTS.md (verify-then-report,
iteration brakes) runs through the model following instructions. This is the
one brake that does NOT: if the file state/HALT exists, no new-exposure order
leaves this machine, regardless of what the agent believes it is doing.
lib/order_*.py enforces it inside the transport layer, so every path that
uses the lib — strategies, reconciler, TWAP slices, ad-hoc scripts — is
covered without any caller opting in.

HALT semantics — blocks NEW EXPOSURE only:
- Blocked while halted: entry orders (anything that opens or adds to a
  position).
- Still allowed: reduce-only closes, protective SL/TP orders, cancels.
  A kill switch that traps a losing position is worse than none — flattening
  must always work under halt.
- The file's EXISTENCE is authoritative. Its JSON content ({ts, reason,
  source}) is attribution only — a malformed or hand-touched HALT file still
  halts (fail-closed).

Who trips it: the user (e.g. "全部停止" → agent runs trip_halt), a monitor
like manager/healthcheck.py on anomalies, or anyone creating state/HALT by
hand. Clearing requires an explicit user instruction — the agent must NEVER
clear a halt on its own initiative.

Audit log: state/audit.jsonl — one JSON line per order attempt, outcome,
halt denial, and halt change, fsynced per line so it survives a crash.
audit() never raises: a protective order must not fail because the audit
disk write did (errors are logged and swallowed — the one sanctioned
exception to the no-silent-failure rule, and only for the LOG write).
"""

import json
import logging
import os
from datetime import datetime, timezone

HALT_PATH = "state/HALT"
AUDIT_PATH = "state/audit.jsonl"

# In-memory halt flag (2026-08-20 audit): trip_halt's own file write can fail
# under exactly the condition that most needs a halt — a full disk (measured
# on the fleet) fails the orders.jsonl append AND the HALT write in the same
# breath, and without this flag the reconcile loop keeps re-buying the fill
# its ledger never recorded, every round, unbounded. The flag halts THIS
# process even when the file can't land; the file remains authoritative for
# every other process and across restarts. Never cleared except by
# clear_halt — same one-way semantics as the file.
_halt_flag = False


class Halted(Exception):
    """Raised instead of sending an entry order while state/HALT exists."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def halted():
    return _halt_flag or os.path.exists(HALT_PATH)


def halt_info():
    """Attribution dict from the HALT file, or None if not halted.
    Unreadable content still counts as halted — existence is what matters."""
    if not halted():
        return None
    try:
        with open(HALT_PATH) as f:
            return json.load(f)
    except Exception:
        return {"reason": "HALT file present but unreadable"}


def trip_halt(reason, source):
    """Set the kill switch. source: who tripped it — 'user' / 'healthcheck' /
    a strategy name. The in-memory flag is set FIRST so this process is
    halted even if the file write below fails (full disk); the write failure
    still propagates so callers know the halt did not persist machine-wide."""
    global _halt_flag
    _halt_flag = True
    os.makedirs(os.path.dirname(HALT_PATH), exist_ok=True)
    tmp = HALT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"ts": _now(), "reason": reason, "source": source}, f)
    os.replace(tmp, HALT_PATH)
    audit("halt_tripped", reason=reason, source=source)


def clear_halt(source):
    """Remove the kill switch. Only on explicit user instruction."""
    global _halt_flag
    _halt_flag = False
    if os.path.exists(HALT_PATH):
        os.remove(HALT_PATH)
    audit("halt_cleared", source=source)


def audit(event, **fields):
    """Append one JSON line to state/audit.jsonl. NEVER raises (see module
    docstring); returns True if the line was written."""
    try:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        record = {"ts": _now(), "event": event, **fields}
        with open(AUDIT_PATH, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception as e:
        logging.error(f"guard.audit failed for event '{event}': {e}")
        return False
