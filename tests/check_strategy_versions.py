"""Minimal check for strategy versions (.claude/docs/strategy-versions.md) — no network.

Drives the real lib/runner.run() on a synthetic price series (MCPT off) inside a temp
workspace and asserts the rules that are expensive to get wrong:

  - a backtest mints v1; a live/cron tick (BLAVE_MODE) mints NOTHING — the whole point,
    a deployed 1h strategy would otherwise bury its real versions within a day
  - an unchanged VERSION_NOTE is stored empty, and stays empty on the third identical
    run (comparing against the STORED note would make it reappear every other version)
  - past 20 versions the oldest entry AND its blob are gone, and version numbers keep
    counting (v21 exists while v1 does not)
  - a live tick over an edited file writes the drift flag, and the next backtest clears it
  - restore() on a funded strategy raises and leaves the file byte-identical; on an
    unfunded one it puts the old code back

Run: cd blaveclaw-config && .venv/bin/python tests/check_strategy_versions.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
import pandas as pd

import lib.runner as runner
import lib.strategy as strategy

fails = 0


def check(cond, msg):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    fails += (not cond)


NAME = "vcheck"
WS = Path(tempfile.mkdtemp(prefix="versions-"))
os.chdir(WS)
runner._REPO_ROOT = WS          # stats.json / pnl.png
strategy._REPO_ROOT = WS        # strategies/<name>/versions/
SRC = WS / "strategies" / NAME / "strategy.py"
SRC.parent.mkdir(parents=True)
VDIR = WS / "strategies" / NAME / "versions"

n = 400
idx = pd.date_range("2024-01-01", periods=n, freq="h")
close = pd.Series(100 + np.cumsum(np.sin(np.arange(n) / 7.0)), index=idx)
DF = pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                   "Close": close, "Volume": 1.0}, index=idx)
SIGNALS = pd.Series(np.where(np.arange(n) % 40 < 20, 1.0, 0.0), index=idx)


def backtest(note="", code="# v\n", live=False):
    """One run() with `note` as VERSION_NOTE and `code` as the strategy file's bytes."""
    SRC.write_text(code, encoding="utf-8")
    config = {"MODE": "backtest", "STRATEGY_NAME": NAME, "SYMBOL": "BTCUSDT",
              "INTERVAL": "1h", "START": "2024-01-01", "FEE": 0.0005, "MCPT": False,
              "VERSION_NOTE": note, "__file__": str(SRC)}
    os.environ.pop("BLAVE_MODE", None)
    if live:
        os.environ["BLAVE_MODE"] = "live"
    try:
        runner.run(config, lambda hdrs: DF, lambda d: SIGNALS)
    finally:
        os.environ.pop("BLAVE_MODE", None)


def index():
    with open(VDIR / "index.json") as f:
        return json.load(f)


# ── minting: backtest yes, live tick never ───────────────────────────────────
backtest(note="first")
check((VDIR / "v1.json").exists() and index()["current"] == 1, "backtest mints v1")
check(index()["items"][0]["note"] == "first", "VERSION_NOTE stored on the first version")
blob = json.loads((VDIR / "v1.json").read_text())
stats = json.loads((WS / "strategies" / NAME / "stats.json").read_text())
check(blob["sharpe"] == stats["Sharpe Ratio"] and blob["ret"] == stats["Total Return [%]"]
      and blob["mdd"] == stats["Max Drawdown [%]"] and blob["trades"] == stats["Trades"],
      "the numbers are the stats.json values, not recomputed")
check(blob["mcpt_p"] is None, "a missing MCPT p-value is null, not 0")
check(blob["start"] == stats["start"] and blob["end"] == stats["end"],
      "the backtest window is stored")
check(bool(blob["daily_dates"]) and blob["code"] == SRC.read_text(),
      "the blob carries the code and the equity curve")

backtest(note="live tick", code="# live\n", live=True)
check(index()["current"] == 1 and not (VDIR / "v2.json").exists(),
      "a live / cron tick mints no version")

# ── unchanged note = empty, every time ───────────────────────────────────────
backtest(note="same", code="# a\n")
backtest(note="same", code="# b\n")
backtest(note="same", code="# c\n")
notes = [i["note"] for i in index()["items"]]
check(notes[-3:] == ["same", "", ""], f"an unchanged note stays empty (got {notes[-3:]})")

# ── retention: 20 kept, numbers never reused ─────────────────────────────────
while index()["current"] < 21:
    backtest(note=f"r{index()['current']}", code=f"# {index()['current']}\n")
ns = [i["n"] for i in index()["items"]]
check(len(ns) == 20 and ns[0] == 2 and ns[-1] == 21, f"20 kept, oldest dropped (got {ns})")
check(not (VDIR / "v1.json").exists() and (VDIR / "v21.json").exists(),
      "the pruned version's blob is deleted")
check([i["n"] for i in strategy.list_versions(NAME)] == ns, "list_versions returns the index")

# ── drift: live tick over an edited file flags, next backtest clears ─────────
backtest(note="drifted", code="# edited by hand\n", live=True)
check((VDIR / "drift.json").exists(), "a live tick over changed code writes the drift flag")
check(index()["current"] == 21, "…and still mints nothing")
backtest(note="rerun", code="# edited by hand\n")
check(not (VDIR / "drift.json").exists(), "a backtest clears the drift flag")
backtest(note="clean", code="# edited by hand\n", live=True)
check(not (VDIR / "drift.json").exists(), "matching code leaves no flag")

# ── restore: funded refuses before touching anything ─────────────────────────
(WS / "manager").mkdir(exist_ok=True)


def set_amount(amount):
    with open(WS / "manager" / "portfolio_config.json", "w") as f:
        json.dump({"amounts": {NAME: amount}, "exchanges": {NAME: "binance"}}, f)


before = SRC.read_bytes()
set_amount(500)
try:
    strategy.restore(NAME, 21)
    check(False, "restore refuses a funded strategy")
except ValueError as e:
    check("fork" in str(e) or "new strategy" in str(e), "restore refuses a funded strategy")
check(SRC.read_bytes() == before, "…and the file was never touched")

set_amount(500)
os.chdir(WS / "strategies")   # the gate reads manager/ relative to cwd — must refuse
try:
    strategy.restore(NAME, 21)
    check(False, "restore refuses to run from outside the workspace root")
except RuntimeError:
    check(True, "restore refuses to run from outside the workspace root")
check(SRC.read_bytes() == before, "…and that file was never touched either")
os.chdir(WS)

set_amount(0)
old_code = json.loads((VDIR / "v5.json").read_text())["code"]
out = strategy.restore(NAME, 5)
check(SRC.read_text() == old_code and out["version"] == 5, "restore writes the old code back")
try:
    strategy.restore(NAME, 1)
    check(False, "a pruned version raises")
except FileNotFoundError as e:
    check("kept" in str(e), "a pruned version raises, naming the retention limit")

print("FAILED" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
