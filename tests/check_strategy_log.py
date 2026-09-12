"""Minimal check: a live tick writes strategies/<name>/strategy.log even when something logged first.

Without Telegram, make_sender() logs a warning before run() is called; that implicitly gives the
root logger a stderr handler, so run()'s basicConfig(filename=...) must still take effect.

Run: cd blaveclaw-config && .venv/bin/python tests/check_strategy_log.py
"""
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
import pandas as pd

import lib.runner as runner

fails = 0


def check(cond, msg):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    fails += (not cond)


def reset_root_logging():
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
        h.close()


NAME = "logcheck"
WS = Path(tempfile.mkdtemp(prefix="strategy-log-"))
os.chdir(WS)                    # load_state/save_state are cwd-relative
runner._REPO_ROOT = WS
LOG = WS / "strategies" / NAME / "strategy.log"

n = 50
idx = pd.date_range("2024-01-01", periods=n, freq="h")
close = pd.Series(100 + np.cumsum(np.sin(np.arange(n) / 7.0)), index=idx)
DF = pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                   "Close": close, "Volume": 1.0}, index=idx)
SIGNALS = pd.Series(0.0, index=idx)

reset_root_logging()
logging.warning("telegram notify unavailable — log-only sender")  # what make_sender() does unpaired
config = {"MODE": "backtest", "STRATEGY_NAME": NAME, "SYMBOL": "BTCUSDT",
          "INTERVAL": "1h", "START": "2024-01-01", "FEE": 0.0005, "MCPT": False}
os.environ["BLAVE_MODE"] = "live"
try:
    runner.run(config, lambda hdrs: DF, lambda d: SIGNALS, send_telegram_fn=None)
finally:
    os.environ.pop("BLAVE_MODE", None)
    reset_root_logging()

check(LOG.exists(), "strategy.log exists after a prior root-logger warning")
check(LOG.exists() and "signal=" in LOG.read_text(), "strategy.log contains the signal= line")

print("all checks passed" if not fails else f"FAILED: {fails}"); sys.exit(1 if fails else 0)
