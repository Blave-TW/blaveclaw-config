"""Minimal check for the progress lines in lib/param_scan.scan_grid and lib/validation.mcpt
(lib/progress.Progress) — no network, no api. A synthetic random-walk OHLCV frame and a
threshold compute_signals go through scan_grid twice: once with the lines forced on
(MIN_SECONDS = 0), once silenced; the grids must be identical and the forced run must
print the `[scan] done/total cells` lines (10 % steps + the final one) and nothing else.
mcpt with a seeded rng must be reproducible run to run and match the pre-progress
list-comprehension formula exactly (same rng draw order).
Run: cd blaveclaw-config && .venv/bin/python tests/check_progress.py
"""
import contextlib, io, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
import lib.progress as P
from lib.param_scan import scan_grid
from lib.validation import mcpt

fails = 0
def check(cond, msg):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + msg); fails += (not cond)

# ── synthetic data + a Type A threshold signal ────────────────────────────────
rng = np.random.default_rng(7)
n = 3000
close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
df = pd.DataFrame({"Open": np.r_[close[0], close[:-1]], "High": close * 1.01,
                   "Low": close * 0.99, "Close": close, "Volume": 1.0}, index=idx)

def compute_signals(data, entry_th=1.0, exit_th=0.0):
    z = (data["Close"] - data["Close"].rolling(50).mean()) / data["Close"].rolling(50).std()
    sig = pd.Series(np.nan, index=data.index)
    sig[z > entry_th] = 1.0
    sig[z < exit_th] = 0.0
    return sig

rows = [0.5, 1.0, 1.5, 2.0]
cols = [-1.0, -0.5, 0.0, 0.5]
valid = lambda r, c: r > c          # 0.5 > 0.5 fails → 15 of 16 cells run

def run_scan():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        g = scan_grid(df, compute_signals, rows, cols, fee=0.0005, warmup=50, valid_fn=valid)
    return g, buf.getvalue()

P.MIN_SECONDS = 0.0                  # force the lines regardless of speed
grid_loud, out_loud = run_scan()
P.MIN_SECONDS = 1e9                  # silence them
grid_quiet, out_quiet = run_scan()

lines = [l for l in out_loud.splitlines() if l.strip()]
check(np.array_equal(grid_loud, grid_quiet, equal_nan=True), "progress on/off → identical grid")
check(np.isfinite(grid_loud).sum() >= 8, f"grid has real Sharpes ({int(np.isfinite(grid_loud).sum())}/15 finite)")
check(all(l.startswith("[scan] ") for l in lines), f"only [scan] lines on stdout → {lines[:2]}")
# 15 cells, ceil(15/10)=2 → ticks at 2,4,…,14 + the final 15 = 8 lines, the last one the 'done in' form
check(len(lines) == 8, f"8 progress lines for 15 cells → got {len(lines)}")
check(bool(re.fullmatch(r"\[scan\] \d+/15 cells, \d+[smh].* elapsed, ~\d+[smh].* left", lines[0])),
      f"first line shape → {lines[0]!r}")
check(bool(re.fullmatch(r"\[scan\] 15/15 cells done in \d+[smh].*", lines[-1])), f"last line shape → {lines[-1]!r}")
check(out_quiet == "", "silenced run prints nothing")

# ── mcpt: reproducible, and identical to the old list-comprehension formula ───
pos = compute_signals(df).ffill().fillna(0).values
def run_mcpt(seed):
    with contextlib.redirect_stdout(io.StringIO()):
        return mcpt(close, pos, n=300, periods_per_year=8760, vol_window=100,
                    rng=np.random.default_rng(seed))
a1, p1, d1 = run_mcpt(3)
a2, p2, d2 = run_mcpt(3)
check(a1 == a2 and p1 == p2 and np.array_equal(d1, d2, equal_nan=True), "mcpt seeded → identical run to run")
check(d1.shape == (300,) and d1.dtype == np.float64, "dist is a float64 array of length n")
# old formula, inline: same rng, same draw order
r = np.random.default_rng(3)
fwd = np.concatenate([np.diff(close) / close[:-1], [0.0]])
lr = np.concatenate([[0.0], np.log(close[1:] / close[:-1])])
rv = pd.Series(lr).rolling(100).std().values * np.sqrt(8760)
vs = np.where((rv > 0) & ~np.isnan(rv), np.clip(0.30 / rv, 0, 2.0), 1.0)
sized = pos * vs; fc = np.abs(np.diff(sized, prepend=0)) * 0.0005
def sh(ret):
    s = sized * ret - fc; s = s[~np.isnan(s)]
    return (s.mean() / s.std()) * np.sqrt(8760) if s.std() > 0 else np.nan
old = np.array([sh(r.permutation(fwd)) for _ in range(300)])
check(np.array_equal(old, d1, equal_nan=True), "mcpt dist == pre-progress formula (bit-exact)")

# ── Progress itself: 10 % cadence and duration format ────────────────────────
P.MIN_SECONDS = 0.0
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    pr = P.Progress("x", 120, "cells")
    for _ in range(120): pr.tick()
xs = buf.getvalue().splitlines()
check(len(xs) == 10 and xs[0].startswith("[x] 12/120 cells,") and xs[-1].startswith("[x] 120/120 cells done in"),
      f"120 items → 10 lines at 12,24,… → {len(xs)}: {xs[0]!r} … {xs[-1]!r}")
check(P.fmt_duration(42) == "42s" and P.fmt_duration(460) == "7m40s" and P.fmt_duration(4320) == "1h12m",
      "fmt_duration 42s / 7m40s / 1h12m")

# ── Progress edge cases + the fail-open stub when lib/progress.py is missing ──
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    pr = P.Progress("e", 3, "cells"); pr.tick(0); pr.tick(); pr.tick(5); pr.tick(); pr.tick()
    P.Progress("z", 0, "cells").tick()
es = buf.getvalue().splitlines()
check(es == ["[e] 3/3 cells done in 0s"] or (len(es) == 2 and es[-1] == "[e] 3/3 cells done in 0s"),
      f"tick(0) / overshoot / total=0 → no crash, final line once → {es}")
import importlib, lib.validation, lib.param_scan, lib.data
sys.modules["lib.progress"] = None                       # simulates a workspace without the new file
try:
    for m in (lib.validation, lib.param_scan, lib.data):
        importlib.reload(m)
    with contextlib.redirect_stdout(io.StringIO()):
        a3, p3, d3 = lib.validation.mcpt(close, pos, n=300, periods_per_year=8760, vol_window=100,
                                         rng=np.random.default_rng(3))
        g3 = lib.param_scan.scan_grid(df, compute_signals, rows, cols, fee=0.0005, warmup=50, valid_fn=valid)
    check(np.array_equal(d3, d1, equal_nan=True) and np.array_equal(g3, grid_loud, equal_nan=True),
          "lib/progress.py missing → stub, same results, no ImportError")
finally:
    del sys.modules["lib.progress"]
    for m in (lib.validation, lib.param_scan, lib.data):
        importlib.reload(m)

print("\nALL PASS" if not fails else f"\n{fails} FAIL")
sys.exit(1 if fails else 0)
