import sys, warnings
warnings.filterwarnings('ignore', category=FutureWarning)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np
from dotenv import dotenv_values
from lib.param_scan import scan_grid, find_plateau, plot_heatmap
import strategy as s

env  = dotenv_values()
hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

# ── Load data once ────────────────────────────────────────────────────────────
print("Loading data...")
base_df = s.fetch_data(hdrs)

# Build zscore with default params to determine scan range
z_series = s._add_indicators(base_df)['zscore'].dropna()
print(f"\nZ-score distribution (n={len(z_series):,}):  "
      f"p5={z_series.quantile(0.05):.2f}  p25={z_series.quantile(0.25):.2f}  "
      f"median={z_series.median():.2f}  p75={z_series.quantile(0.75):.2f}  "
      f"p95={z_series.quantile(0.95):.2f}")

# ── Scan ranges ───────────────────────────────────────────────────────────────
entry_vals = np.round(np.linspace(z_series.quantile(0.05), z_series.quantile(0.45), 7), 2)
exit_vals  = np.round(np.linspace(z_series.quantile(0.55), z_series.quantile(0.95), 7), 2)
print(f"\nENTRY_Z candidates: {list(entry_vals)}")
print(f"EXIT_Z  candidates: {list(exit_vals)}\n")

# ── Parameter scan ────────────────────────────────────────────────────────────
grid = scan_grid(
    base_df, s.compute_signals,
    row_vals=entry_vals, col_vals=exit_vals,
    row_param='entry_z', col_param='exit_z',
    fee=s.FEE, warmup=s.WARMUP,
    valid_fn=lambda entry, exit_: entry < exit_,
)

# ── Best plateau ──────────────────────────────────────────────────────────────
best_idx, _, best_entry, best_exit, best_sharpe = find_plateau(grid, entry_vals, exit_vals)
print(f"\nBest plateau → ENTRY_Z={best_entry}  EXIT_Z={best_exit}  Sharpe={best_sharpe:.3f}")
print(f"Point Sharpe  = {grid[best_idx]:.3f}")
print(f"\n→ Update strategy.py: ENTRY_Z = {best_entry}  EXIT_Z = {best_exit}")

plot_heatmap(
    grid, entry_vals, exit_vals,
    best_idx=best_idx,
    row_label='ENTRY_Z', col_label='EXIT_Z',
    title=f'{s.STRATEGY_NAME} — Sharpe Heatmap',
    output_path=str(Path(__file__).parent / 'heatmap.png'),
)
