"""
Sends a Telegram alert when a strategy's cron run crashes (non-zero exit).
Called only by manager/run_strategy.sh — not meant to be run directly.

Cooldown: at most one alert per strategy per COOLDOWN_HOURS, so a strategy
stuck crashing every cron tick doesn't spam Telegram forever. The failure is
still appended to strategies/<name>/strategy.log on every crash regardless.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COOLDOWN_HOURS = 24
MAX_OUTPUT_CHARS = 1500


def main():
    if len(sys.argv) < 4:
        return
    strategy_name, exit_code, output = sys.argv[1], sys.argv[2], sys.argv[3]

    state_path = f"strategies/{strategy_name}/failure_alert_state.json"
    now = time.time()
    if os.path.exists(state_path):
        try:
            last = json.load(open(state_path)).get("last_alert_ts", 0)
        except Exception:
            last = 0
        if now - last < COOLDOWN_HOURS * 3600:
            return

    tail = output[-MAX_OUTPUT_CHARS:]
    msg = (
        f"⚠️ Strategy {strategy_name} failed (exit={exit_code})\n"
        f"The schedule will keep firing, but this run did not complete — no orders "
        f"or signals were produced. The same error will likely repeat on every run "
        f"until it is fixed.\n\n{tail}"
    )

    try:
        from lib.notify import send_text
        send_text(msg)
    except Exception:
        pass  # best-effort — the alerter itself must never crash the cron job

    json.dump({"last_alert_ts": now}, open(state_path, "w"))


if __name__ == "__main__":
    main()
