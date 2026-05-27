"""
Parameter scan for twstock_momentum — sweeps MOM_WINDOW × TOP_N.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import warnings
warnings.filterwarnings('ignore', category=FutureWarning)

from dotenv import dotenv_values
from strategy import fetch_data, compute_signals, FEE, WARMUP
from lib.param_scan import scan_grid, find_plateau, plot_heatmap

MOM_VALS = [20, 40, 60, 80, 120]
TOP_VALS  = [3, 5, 7, 10]

env  = dotenv_values()
hdrs = {'api-key': env['blave_api_key'], 'secret-key': env['blave_secret_key']}
print("Fetching data...")
data = fetch_data(hdrs)
print(f"  {len(data[0])} bars, {data[0].shape[1]} stocks")

grid = scan_grid(
    data, compute_signals,
    row_vals=MOM_VALS, col_vals=TOP_VALS,
    row_param='mom_window', col_param='top_n',
    fee=FEE, warmup=max(MOM_VALS),  # trim to the longest window for fair comparison
)

best_idx, _, best_mw, best_tn, best_sharpe = find_plateau(grid, MOM_VALS, TOP_VALS)
print(f"\n最佳 plateau: MOM_WINDOW={best_mw}  TOP_N={best_tn}  Sharpe={grid[best_idx]:.3f}")

plot_heatmap(
    grid, MOM_VALS, TOP_VALS, best_idx,
    row_label="MOM_WINDOW", col_label="TOP_N",
    title="twstock_momentum — Sharpe Scan",
    output_path="strategies/twstock_momentum/heatmap.png",
)
