"""
Parameter scan for tsmc_ma — sweeps SMA_FAST × SMA_SLOW.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import warnings
warnings.filterwarnings('ignore', category=FutureWarning)

from dotenv import dotenv_values
from strategy import fetch_data, compute_signals, FEE
from lib.param_scan import scan_grid, find_plateau, plot_heatmap

FAST_VALS = [5, 10, 20, 40, 60]
SLOW_VALS = [20, 40, 60, 120, 250]

env  = dotenv_values()
hdrs = {'api-key': env['blave_api_key'], 'secret-key': env['blave_secret_key']}
print("Fetching data...")
df = fetch_data(hdrs)
print(f"  {len(df)} bars")

grid = scan_grid(
    df, compute_signals,
    row_vals=FAST_VALS, col_vals=SLOW_VALS,
    row_param='fast', col_param='slow',
    fee=FEE, warmup=max(SLOW_VALS),
    valid_fn=lambda f, s: f < s,
)

best_idx, _, best_fast, best_slow = find_plateau(grid, FAST_VALS, SLOW_VALS)
print(f"\n最佳 plateau: SMA_FAST={best_fast}  SMA_SLOW={best_slow}  Sharpe={grid[best_idx]:.3f}")

plot_heatmap(
    grid, FAST_VALS, SLOW_VALS, best_idx,
    row_label="SMA_FAST", col_label="SMA_SLOW",
    title="tsmc_ma — Sharpe Scan",
    output_path="strategies/tsmc_ma/heatmap.png",
)
