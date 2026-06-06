"""
Parameter scan for tw100_foreign_zscore — sweeps ACCUM_WINDOW × ZSCORE_WINDOW.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import warnings
warnings.filterwarnings('ignore', category=FutureWarning)

from dotenv import dotenv_values
from strategy import fetch_data, compute_signals, FEE, WARMUP
from lib.param_scan import scan_grid, find_plateau, plot_heatmap

ACCUM_VALS  = [5, 10, 20, 40]
ZSCORE_VALS = [60, 120, 252]

env  = dotenv_values()
hdrs = {'api-key': env['blave_api_key'], 'secret-key': env['blave_secret_key']}
print("Fetching data (100 stocks, may take a while)...")
data = fetch_data(hdrs)
print(f"  {len(data[0])} bars, {data[0].shape[1]} stocks")

grid = scan_grid(
    data, compute_signals,
    row_vals=ACCUM_VALS, col_vals=ZSCORE_VALS,
    row_param='accum_window', col_param='zscore_window',
    fee=FEE, warmup=max(ACCUM_VALS) + max(ZSCORE_VALS),
)

best_idx, _, best_accum, best_zscore, best_sharpe = find_plateau(grid, ACCUM_VALS, ZSCORE_VALS)
print(f"\n最佳 plateau: ACCUM_WINDOW={best_accum}  ZSCORE_WINDOW={best_zscore}  Sharpe={grid[best_idx]:.3f}")

plot_heatmap(
    grid, ACCUM_VALS, ZSCORE_VALS, best_idx,
    row_label="ACCUM_WINDOW", col_label="ZSCORE_WINDOW",
    title="tw100_foreign_zscore — Sharpe Scan",
    output_path=str(Path(__file__).parent / 'heatmap.png'),
)
