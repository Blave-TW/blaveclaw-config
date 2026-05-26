#!/bin/bash
# Watchdog wrapper — restarts reconciler.py on crash and sends Telegram alert.
# Usage: cd /root/.openclaw/workspace && bash manager/start_reconciler.sh

WORKSPACE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$WORKSPACE"

notify() {
    python3 - "$1" <<'EOF'
import sys, json, os
sys.path.insert(0, '.')
from lib.notify import make_sender
from lib.portfolio import load_portfolio_config
cfg  = load_portfolio_config()
msgs = cfg.get('messages', {})
text = sys.argv[1]
send = make_sender()
send(text)
EOF
}

get_msg() {
    # get_msg <key> <default> [format_args...]
    python3 - "$1" "$2" "$3" <<'EOF'
import sys, json
sys.path.insert(0, '.')
from lib.portfolio import load_portfolio_config
cfg  = load_portfolio_config()
msgs = cfg.get('messages', {})
key, default, arg = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ''
tpl  = msgs.get(key, default)
print(tpl.format(code=arg) if arg else tpl)
EOF
}

notify "$(get_msg watchdog_started '✅ Auto-trading started')"

while true; do
    python3 manager/reconciler.py
    EXIT_CODE=$?
    MSG="$(get_msg watchdog_restart '⚠️ System restarted (code {code}), resuming in 10s' "$EXIT_CODE")"
    echo "$MSG"
    notify "$MSG"
    sleep 10
done
