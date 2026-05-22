#!/bin/bash
# Watchdog wrapper — restarts reconciler.py on crash and sends Telegram alert.
# Usage: cd /root/.openclaw/workspace && bash manager/start_reconciler.sh

WORKSPACE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$WORKSPACE"

notify() {
    python3 - <<EOF
import sys, os
sys.path.insert(0, '.')
from lib.notify import make_sender
make_sender()('$1')
EOF
}

notify "[reconciler] watchdog started"

while true; do
    python3 manager/reconciler.py
    EXIT_CODE=$?
    MSG="[reconciler] process exited (code $EXIT_CODE) — restarting in 10s"
    echo "$MSG"
    notify "$MSG"
    sleep 10
done
