import sys, time, warnings
warnings.filterwarnings('ignore', category=FutureWarning)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import dotenv_values
from lib.param_scan import percentile_thresholds, scan_grid, find_plateau, plot_heatmap
import strategy as s

env  = dotenv_values()
hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

# ── 載入資料 ──────────────────────────────────────────────────────────────────
t0 = time.time()
df = s.fetch_data(hdrs)
print(f"資料載入: {time.time()-t0:.1f}s  ({len(df):,} bars)\n")

# ── 從 TI 分佈決定掃描範圍 ────────────────────────────────────────────────────
entry_vals, exit_vals = percentile_thresholds(df['TI'], n_parts=9)
print(f"掃描組合數: {len(entry_vals)} × {len(exit_vals)} = {len(entry_vals)*len(exit_vals)}\n")

# ── 參數掃描 ──────────────────────────────────────────────────────────────────
t1    = time.time()
grid  = scan_grid(df, s.compute_signals, entry_vals, exit_vals, fee=s.FEE, freq='5min')
total = len(entry_vals) * len(exit_vals)
print(f"掃描耗時: {time.time()-t1:.1f}s  (平均 {(time.time()-t1)/total*1000:.0f}ms / 組合)")

# ── 最佳參數 ──────────────────────────────────────────────────────────────────
best_idx, _, best_entry, best_exit, best_sharpe = find_plateau(grid, list(entry_vals), list(exit_vals))
print(f"最佳參數: ENTRY_TH={best_entry}, EXIT_TH={best_exit}  Sharpe={grid[best_idx]:.3f}")

plot_heatmap(
    grid, list(entry_vals), list(exit_vals),
    best_idx=best_idx,
    row_label='ENTRY_TH', col_label='EXIT_TH',
    title=f'{s.STRATEGY_NAME} — Sharpe Grid (TI 24h, {s.INTERVAL})',
    output_path=str(Path(__file__).parent / 'heatmap.png'),
)
