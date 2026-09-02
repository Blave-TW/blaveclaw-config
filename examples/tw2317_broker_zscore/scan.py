"""
Parameter scan for tw2317_broker_zscore — FOUR constants (ENTRY_Z, EXIT_Z, WINDOW,
ZSCORE_WIN), so this is the canonical "three or more parameters" scan: pairwise
coordinate descent, at most two rounds, ONE scan.json.

    round 1  ENTRY_Z × EXIT_Z   (the thresholds — the most sensitive pair; the two
                                  windows pinned at the values strategy.py holds now)
             → plateau → pin the thresholds there
    round 2  WINDOW × ZSCORE_WIN (thresholds pinned at the round-1 plateau)
             → plateau → stop (two rounds is the cap: each round is one iteration
                          under AGENTS.md › Iteration Brakes)

Every round plots its own heatmap (PNG → workspace chat / Telegram). Only round 1
is written to scan.json — see the comment at write_scan below for why.

Needs the Blave API key in .env (fetch_data hits the twstock price + branch
endpoints); run from the strategy folder: `.venv/bin/python scan.py`.
"""
import sys, time, warnings
warnings.filterwarnings('ignore', category=FutureWarning)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np
from dotenv import dotenv_values
from lib.param_scan import nice_grid, nice_step, scan_grid, find_plateau, write_scan, plot_heatmap
import strategy as s

env  = dotenv_values()
hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}
OUT  = Path(__file__).parent

# ── 資料一次 ──────────────────────────────────────────────────────────────────
t0 = time.time()
base_df = s.fetch_data(hdrs)
print(f"資料載入: {time.time()-t0:.1f}s  ({len(base_df):,} bars)\n")

# ════════════════════════════════════════════════════════════════════════════
# Round 1 — ENTRY_Z × EXIT_Z, windows pinned at the file's current values
# ════════════════════════════════════════════════════════════════════════════
# Range from the indicator's own distribution (p5–p95), computed with the CURRENT
# windows (a different window would shift the distribution). Contrarian: entry is
# on the negative side, exit sweeps the upper part — do not use percentile_thresholds
# (it assumes momentum entry-above / exit-below; see references/strategy-code.md).
z      = s._add_indicators(base_df)['zscore'].dropna()
p5, p95 = np.percentile(z, [5, 95])
print(f"z-score 分佈 (n={len(z):,}): p5={p5:.2f}  median={z.median():.2f}  p95={p95:.2f}")

# One nice step for both axes; each axis is anchored on the constant strategy.py holds
# now, so the current cell is always on the grid (the web marks it) and every
# neighbour is a clean multiple of the step away.
step       = nice_step(p95 - p5, 13)
entry_vals = nice_grid(p5, 0.0, current=s.ENTRY_Z, step=step)   # entry: p5 … 0
exit_vals  = nice_grid(0.0, p95, current=s.EXIT_Z,  step=step)   # exit:  0 … p95
print(f"step={step}  ENTRY_Z 候選 ({len(entry_vals)}): {entry_vals}")
print(f"          EXIT_Z  候選 ({len(exit_vals)}): {exit_vals}\n")

t1 = time.time()
grid1 = scan_grid(
    base_df, s.compute_signals, entry_vals, exit_vals,
    row_param='entry_z', col_param='exit_z',        # window / zscore_win stay at defaults = file values
    fee=s.FEE, warmup=s.WARMUP,
    valid_fn=lambda entry, exit_: entry < exit_,
)
print(f"round 1 掃描耗時: {time.time()-t1:.1f}s")

best1, nbr1, best_entry, best_exit, sharpe1 = find_plateau(grid1, entry_vals, exit_vals)
print(f"round 1 穩健參數: ENTRY_Z={best_entry}, EXIT_Z={best_exit}  "
      f"鄰域 Sharpe={sharpe1:.3f}  單格 Sharpe={grid1[best1]:.3f}")

# scan.json holds ONE 2D grid (the web 穩健參數 tab draws one heatmap and its adopt
# prompt only changes these two constants). Write the round-1 pair — the most
# sensitive one — because it is the only grid computed with every OTHER constant
# at the file's current value: its `current` mark is honest and the web's
# 「把 ENTRY_Z 改成 …」 lands on a cell that was actually scanned. Round 2 pins the
# thresholds at the plateau, not the file values, so its grid would mislabel the
# current cell. The round-2 result is reported in the conversation instead.
write_scan(grid1, entry_vals, exit_vals, nbr1, best1,
           output_dir=str(OUT),
           row_param='ENTRY_Z', col_param='EXIT_Z', fee=s.FEE,
           start=s.START, end=base_df.index[-1].strftime('%Y-%m-%d'),
           current=(s.ENTRY_Z, s.EXIT_Z))
plot_heatmap(grid1, entry_vals, exit_vals, best1,
             row_label='ENTRY_Z', col_label='EXIT_Z',
             title=f'{s.STRATEGY_NAME} — round 1 (WINDOW={s.WINDOW}, ZSCORE_WIN={s.ZSCORE_WIN})',
             output_path=str(OUT / 'heatmap.png'))

# ════════════════════════════════════════════════════════════════════════════
# Round 2 — WINDOW × ZSCORE_WIN, thresholds pinned at the round-1 plateau
# ════════════════════════════════════════════════════════════════════════════
# Integer axes (bar counts): step ≥ 1, cells are int, anchored on the file's values.
window_vals = nice_grid(3, 15, n=7, current=s.WINDOW, integer=True)          # 3, 5, …, 15
zwin_vals   = nice_grid(60, 240, n=7, current=s.ZSCORE_WIN, integer=True)    # 45, 70, …, 245
print(f"\nWINDOW 候選 ({len(window_vals)}): {window_vals}")
print(f"ZSCORE_WIN 候選 ({len(zwin_vals)}): {zwin_vals}")

round2_fn = lambda df, window, zscore_win: s.compute_signals(
    df, entry_z=best_entry, exit_z=best_exit, window=window, zscore_win=zscore_win)

t2 = time.time()
grid2 = scan_grid(
    base_df, round2_fn, window_vals, zwin_vals,
    row_param='window', col_param='zscore_win',
    fee=s.FEE, warmup=max(window_vals) + max(zwin_vals),   # longest windows in the grid
    valid_fn=lambda w, zw: w < zw,
)
print(f"round 2 掃描耗時: {time.time()-t2:.1f}s")

best2, _, best_window, best_zwin, sharpe2 = find_plateau(grid2, window_vals, zwin_vals)
print(f"round 2 穩健參數: WINDOW={best_window}, ZSCORE_WIN={best_zwin}  "
      f"鄰域 Sharpe={sharpe2:.3f}  單格 Sharpe={grid2[best2]:.3f}")

# Heatmap only — no write_scan for round 2 (see above).
plot_heatmap(grid2, window_vals, zwin_vals, best2,
             row_label='WINDOW', col_label='ZSCORE_WIN',
             title=f'{s.STRATEGY_NAME} — round 2 (ENTRY_Z={best_entry}, EXIT_Z={best_exit})',
             output_path=str(OUT / 'heatmap_round2.png'))

# Two rounds → stop. Report both plateaus; the user adopts the thresholds from the web
# (scan.json) and the windows from this message — never edit strategy.py here.
print(f"\n建議: ENTRY_Z={best_entry}, EXIT_Z={best_exit} (web 穩健參數分頁)；"
      f"WINDOW={best_window}, ZSCORE_WIN={best_zwin} (round 2, 見 heatmap_round2.png)")
