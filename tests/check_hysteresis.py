"""Minimal check for lib/strategy.hysteresis / threshold_position and the scan_grid
valid_fn short-circuit — no network, no api. A reference per-bar loop (the one
strategy-code.md used to inline) is the oracle: hysteresis() per side must equal it with
the other side off, threshold_position() must equal it with both sides on AND with one
side pushed out of range (the scan idiom, where it must take the vectorized path), NaN
bars, values exactly on a threshold and gaps across the flat band included; scan_grid must
not call compute_signals on a combo an explicit valid_fn rejects.
Run: cd blaveclaw-config && .venv/bin/python tests/check_hysteresis.py
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
from lib.strategy import hysteresis, threshold_position
from lib.param_scan import scan_grid

fails = 0
def check(cond, msg):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + msg); fails += (not cond)


def reference(x, buy_th, sell_th, cover_th, short_th):
    x = x.to_numpy(); pos = 0; out = np.zeros(len(x))
    for i, xi in enumerate(x):
        if np.isnan(xi):
            out[i] = pos; continue
        if pos == 1 and xi < sell_th:    pos = 0
        elif pos == -1 and xi > cover_th: pos = 0
        if pos == 0:
            if   xi > buy_th:   pos = 1
            elif xi < short_th: pos = -1
        out[i] = pos
    return pd.Series(out)


rng = np.random.default_rng(7)
n = 20000
idx = pd.date_range("2024-01-01", periods=n, freq="5min")
raw = rng.standard_normal(n) * 1.5
raw[rng.random(n) < 0.02] = np.nan                     # unpublished bars
raw[rng.random(n) < 0.01] *= 4                          # gaps straight across the band
x = pd.Series(raw, index=idx)
x.iloc[0] = np.nan                                      # leading NaN → flat, not NaN

for buy, sell, cover, short in [(0.8, 0.2, -0.2, -0.8), (1.0, -0.5, 0.5, -1.0), (0.5, 0.6, -0.6, -0.5)]:
    ref_l = reference(x, buy, sell, -1e9, -1e9).to_numpy()
    ref_s = reference(x, 1e9, 1e9, cover, short).to_numpy()
    ref_b = reference(x, buy, sell, cover, short).to_numpy()
    hl = hysteresis(x, buy, sell, side=1)
    hs = hysteresis(x, short, cover, side=-1)
    tp = threshold_position(x, buy, sell, cover, short)
    tag = f"buy={buy} sell={sell} cover={cover} short={short}"
    check(np.array_equal(hl.to_numpy(), ref_l) and hl.index.equals(idx), f"hysteresis long == loop, other side off ({tag})")
    check(np.array_equal(hs.to_numpy(), ref_s), f"hysteresis short == loop, other side off ({tag})")
    check(np.array_equal(tp.to_numpy(), ref_b) and tp.index.equals(idx), f"threshold_position == loop, both sides ({tag})")
    tpl = threshold_position(x, buy, sell, -1e9, -1e9)          # scan idiom: short side off
    tps = threshold_position(x, 1e9, 1e9, cover, short)         # scan idiom: long side off
    check(np.array_equal(tpl.to_numpy(), ref_l) and np.array_equal(tps.to_numpy(), ref_s), f"threshold_position with one side out of range == loop ({tag})")
    check(not hl.isna().any() and not tp.isna().any(), f"no NaN positions ({tag})")

check(hysteresis(np.array([np.nan, 1.0, 0.5, 0.1]), 0.8, 0.2).tolist() == [0.0, 1.0, 1.0, 0.0], "plain ndarray input, leading NaN flat")
# values exactly on a threshold: strict >/< on both sides, so a touch holds, never enters or exits
edge = pd.Series([0.8, 0.9, 0.2, 0.19, -0.8, -0.9, -0.2, -0.19, 0.8, 0.81])
ref_e = reference(edge, 0.8, 0.2, -0.2, -0.8).to_numpy()
check(ref_e.tolist() == [0, 1, 1, 0, 0, -1, -1, 0, 0, 1], "oracle on threshold touches is what the doc says")
check(np.array_equal(threshold_position(edge, 0.8, 0.2, -0.2, -0.8).to_numpy(), ref_e), "threshold_position: touching a threshold holds")
check(np.array_equal(hysteresis(edge, 0.8, 0.2).to_numpy(), reference(edge, 0.8, 0.2, -1e9, -1e9).to_numpy()), "hysteresis: touching a threshold holds")

# timing on 200k bars, best of 3 — the vectorized path must be clearly cheaper than the
# loop (a 5× bar is far below the ~50× measured; a slow box shifts both sides alike)
big = pd.Series(rng.standard_normal(200_000) * 1.5)
def timed(fn, k=3):
    best = float("inf")
    for _ in range(k):
        t = time.perf_counter(); fn(); best = min(best, time.perf_counter() - t)
    return best
t_ref = timed(lambda: reference(big, 0.8, 0.2, -0.2, -0.8))
t_vec = timed(lambda: threshold_position(big, 0.8, 0.2, -1e9, -1e9))
t_tp  = timed(lambda: threshold_position(big, 0.8, 0.2, -0.2, -0.8))
check(t_vec * 5 < t_ref, f"one side out of range → vectorized, ≥5× faster than the loop ({t_ref*1e3:.0f} ms → {t_vec*1e3:.1f} ms, 200k bars)")
print(f"        threshold_position both sides {t_tp*1e3:.0f} ms vs inline loop {t_ref*1e3:.0f} ms")

# scan_grid: explicit valid_fn short-circuits before compute_signals
df = pd.DataFrame({"Open": 1 + rng.random(n), "Close": 1 + rng.random(n), "TI": x.to_numpy()}, index=idx)
calls = []
def fn(d, buy_th, sell_th):
    calls.append((buy_th, sell_th))
    return hysteresis(d["TI"], buy_th, sell_th)
rows, cols = [0.2, 0.5, 0.8], [0.1, 0.4, 0.7]
g = scan_grid(df, fn, rows, cols, row_param="buy_th", col_param="sell_th", fee=0.0005, valid_fn=lambda b, s: b > s)
valid = [(r, c) for r in rows for c in cols if r > c]
check(sorted(calls) == sorted(valid), f"scan_grid skips invalid combos before compute_signals ({len(calls)}/{len(rows)*len(cols)} computed)")
check(all(np.isnan(g[i, j]) for i, r in enumerate(rows) for j, c in enumerate(cols) if r <= c), "invalid cells stay NaN")
calls.clear()
scan_grid(df, fn, rows, cols, row_param="buy_th", col_param="sell_th", fee=0.0005)
check(len(calls) == 9, "no valid_fn → every combo still computed (type-dependent default)")

print("\nALL PASS" if not fails else f"\n{fails} FAILED"); sys.exit(1 if fails else 0)
