"""
Deployment healthcheck — alerts when something that SHOULD be running has gone quiet.

Complements manager/alert_failure.py:
  alert_failure.py fires when a run happened and CRASHED.
  healthcheck.py  fires when a run did NOT happen at all — lost cron entry,
                  dead daemon, wiped crontab, or "deployed" but never scheduled.

Data model (all under state/, which the nightly config sync never overwrites):
  state/deployments.json      registry of what should be running:
      {"<name>": {"type": "cron"|"daemon", "expect_every_minutes": 60,
                  "registered_at": "<iso>"}}
  state/heartbeat/<name>      touched after each successful run (run_strategy.sh)
                              or loop tick (daemons)
  state/healthcheck_alerts.json  last-alert timestamps (cooldown bookkeeping)

Cron (add ONCE, like the snapshot cron — see references/deployment.md):
  */30 * * * * cd $BLAVECLAW_HOME/workspace && python3 manager/healthcheck.py
  ($BLAVECLAW_HOME defaults to /root/.openclaw if unset)

Self-healing: any `run_strategy.sh <name>` entry found in crontab is
auto-registered, so strategies deployed before this healthcheck existed are
covered without anyone re-registering them. Heartbeat files without a registry
entry are shown in the daily snapshot but never alerted (a one-off manual run
is not a deployment).
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REGISTRY_PATH = "state/deployments.json"
HEARTBEAT_DIR = "state/heartbeat"
ALERTS_PATH = "state/healthcheck_alerts.json"
ALERT_COOLDOWN_HOURS = 6
GRACE_MINUTES = 10


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"[healthcheck] failed to read {path}: {e}")
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _read_crontab():
    """Return crontab text, or None when unavailable (Windows, no crontab)."""
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return None


def _expect_minutes_from_cron(line):
    """Conservative expected-interval guess from a cron expression.

    */N in the minute field → N; hourly → 60; daily → 1440. Restricted
    schedules are tiered by their longest possible gap: month-restricted →
    527040 (yearly — effectively disables staleness checks; the signal is
    meaningless for rare jobs), day-of-month → 44640 (monthly), day-of-week →
    10080 (weekly). When unsure, guess LONG — a late alert beats a false one.
    """
    fields = line.split()
    if len(fields) < 5:
        return 1440
    minute, hour, dom, month, dow = fields[:5]
    if month != "*":
        return 527040
    if dom != "*":
        return 44640
    if dow != "*":
        return 10080
    m = re.fullmatch(r"\*/(\d+)", minute)
    if m and hour == "*":
        return max(int(m.group(1)), 1)
    if minute == "*" and hour == "*":
        return 1
    if hour == "*":
        return 60
    return 1440


def _cron_strategy_entries(crontab_text):
    """{name: expect_every_minutes} for every run_strategy.sh entry in crontab."""
    entries = {}
    for line in crontab_text.splitlines():
        line = line.strip()
        if line.startswith("#") or "run_strategy.sh" not in line:
            continue
        m = re.search(r"run_strategy\.sh\s+(\S+)", line)
        if m:
            entries[m.group(1)] = _expect_minutes_from_cron(line)
    return entries


def _autoregister(registry, cron_entries):
    """Add crontab strategies missing from the registry. Returns True if changed."""
    changed = False
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    for name, expect in cron_entries.items():
        if name not in registry:
            registry[name] = {
                "type": "cron",
                "expect_every_minutes": expect,
                "registered_at": now_iso,
            }
            print(f"[healthcheck] auto-registered cron strategy: {name} (every {expect}m)")
            changed = True
    return changed


def _check_entry(name, entry, cron_entries, now):
    """Return a problem string, or None if the deployment looks healthy."""
    expect_min = entry.get("expect_every_minutes", 60)
    threshold_s = (2 * expect_min + GRACE_MINUTES) * 60
    hb_path = os.path.join(HEARTBEAT_DIR, name)

    # cron-type: the schedule entry itself must still exist
    if entry.get("type") == "cron" and cron_entries is not None and name not in cron_entries:
        return "registered as a deployment but no crontab entry found (schedule lost or never created)"

    if not os.path.exists(hb_path):
        try:
            registered = datetime.strptime(
                entry.get("registered_at", ""), "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            registered = 0
        if registered and now - registered > threshold_s:
            return "registered but never ran successfully (no heartbeat yet)"
        return None  # freshly registered — give it time

    age_s = now - os.path.getmtime(hb_path)
    if age_s > threshold_s:
        hours = age_s / 3600
        return f"no successful run for {hours:.1f}h (expected every {expect_min} min)"
    return None


def health_report():
    """Status of every registered deployment (also used by snapshot.py).

    Returns (lines, problems): lines are display strings for all entries,
    problems is {name: problem_string} for the unhealthy ones.
    """
    now = time.time()
    registry = _load_json(REGISTRY_PATH, {})
    crontab_text = _read_crontab()
    cron_entries = None if crontab_text is None else _cron_strategy_entries(crontab_text)

    if cron_entries and _autoregister(registry, cron_entries):
        _save_json(REGISTRY_PATH, registry)

    lines, problems = [], {}
    for name, entry in sorted(registry.items()):
        problem = _check_entry(name, entry, cron_entries, now)
        hb_path = os.path.join(HEARTBEAT_DIR, name)
        if problem:
            problems[name] = problem
            lines.append(f"  {name:<20} ⚠️ {problem}")
        elif os.path.exists(hb_path):
            age_min = (now - os.path.getmtime(hb_path)) / 60
            when = f"{age_min:.0f}m ago" if age_min < 90 else f"{age_min/60:.1f}h ago"
            lines.append(f"  {name:<20} ✅ last run {when}")
        else:
            lines.append(f"  {name:<20} 🕐 registered, waiting for first run")

    # orphan heartbeats: shown for information, never alerted
    if os.path.isdir(HEARTBEAT_DIR):
        for fn in sorted(os.listdir(HEARTBEAT_DIR)):
            if fn not in registry:
                lines.append(f"  {fn:<20} ▫️ runs but not registered as a deployment")
    return lines, problems


def main():
    alerts = _load_json(ALERTS_PATH, {})
    now = time.time()
    _, problems = health_report()

    to_send = []
    for name, problem in problems.items():
        last = alerts.get(name, 0)
        if now - last < ALERT_COOLDOWN_HOURS * 3600:
            continue
        to_send.append(f"⚠️ {name}: {problem}")
        alerts[name] = now

    recovered = [n for n in list(alerts) if n not in problems]
    for name in recovered:
        del alerts[name]
        to_send.append(f"✅ {name} is running again")

    if not to_send:
        print("[healthcheck] all deployments healthy")
        return

    msg = "🩺 Deployment Health\n" + "\n".join(to_send) + (
        "\n\nReply to this message and I can investigate." if problems else ""
    )
    print(msg)
    try:
        from lib.notify import send_text
        send_text(msg)
    except Exception as e:
        print(f"[healthcheck] telegram send failed: {e}")  # never crash the cron job
    _save_json(ALERTS_PATH, alerts)


if __name__ == "__main__":
    main()
