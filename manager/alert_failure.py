"""
Sends a Telegram alert when a strategy's cron run crashes (non-zero exit).
CLI form is called only by manager/run_strategy.sh; alert() is also called
directly by manager/wait_for_bar.py's cross-platform launcher (no bash/shell
in between there, so it calls the Python function instead of the CLI).

Cooldown: at most one alert per strategy per COOLDOWN_HOURS, so a strategy
stuck crashing every cron tick doesn't spam Telegram forever. The failure is
still appended to strategies/<name>/strategy.log on every crash regardless
(by the caller — run_strategy.sh does its own append; wait_for_bar.py does
its own too).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COOLDOWN_HOURS = 24
MAX_OUTPUT_CHARS = 1500


def alert(strategy_name, exit_code, output):
    state_path = f"strategies/{strategy_name}/failure_alert_state.json"
    now = time.time()
    if os.path.exists(state_path):
        try:
            last = json.load(open(state_path)).get("last_alert_ts", 0)
        except Exception:
            last = 0
        if now - last < COOLDOWN_HOURS * 3600:
            return

    tail = str(output)[-MAX_OUTPUT_CHARS:]
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


def main():
    if len(sys.argv) < 4:
        return
    strategy_name, exit_code, output = sys.argv[1], sys.argv[2], sys.argv[3]
    alert(strategy_name, exit_code, output)


if __name__ == "__main__":
    main()
