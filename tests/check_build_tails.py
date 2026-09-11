"""Minimal check for the capped tails in lib/runner.py _build_candles / _build_panes.
The capped path selects the tail before formatting (a live 1m tick would otherwise format
years of bars to keep 20k); it must equal the uncapped output's last k, including when bad
bars are scattered or fill the whole tail, and with an object-dtype pane series.
Run: cd blaveclaw-config && .venv/bin/python tests/check_build_tails.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
import lib.runner as R

fails = 0
def check(cond, msg):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + msg); fails += (not cond)

rng = np.random.default_rng(0)
n = 30000
c = 100 + np.cumsum(rng.standard_normal(n) * 0.1) + 50
base = pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c.copy(),
                     "Volume": rng.random(n)}, index=pd.date_range("2025-01-01", periods=n, freq="1min"))
scattered = base.copy()
for j, bad in enumerate((np.nan, 0.0, -1.0, np.inf, np.nan)):
    scattered.iloc[rng.choice(n, 1500), j] = bad
tail_bad = base.copy()
tail_bad.iloc[-25000:, 3] = np.nan
obj = pd.Series(([None, "x", "1.5", np.nan, 3, np.inf] * (n // 6 + 1))[:n], index=base.index, dtype=object)
plot = {"close": "Close", "ovl": ("Open", {"overlay": True}), "obj": (obj, {"pane": "g"})}

for name, df in (("scattered", scattered), ("tail_bad", tail_bad)):
    full_c = R._build_candles(df, None)
    full_p = R._build_panes(plot, df, max_points=None)
    for k in (7, 20000, len(full_c) + 5):
        check(R._build_candles(df, k) == full_c[-k:], f"{name} candles k={k}")
        capped = R._build_panes(plot, df, max_points=k)
        check([p["points"] for p in capped] == [p["points"][-k:] for p in full_p], f"{name} panes k={k}")
    check(R._build_candles(df, 0) == full_c and len(full_c) > 7, f"{name} candles k=0 → full path")
    check(R._build_panes(plot, df, max_points=0) == full_p, f"{name} panes k=0 → full path")

print("all checks passed" if not fails else f"FAILED: {fails}"); sys.exit(1 if fails else 0)
