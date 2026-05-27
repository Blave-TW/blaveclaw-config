import sys, time, warnings
warnings.filterwarnings('ignore', category=FutureWarning)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import dotenv_values
from lib.data import fetch_kline
from lib.param_scan import scan_grid, find_plateau, plot_heatmap
import strategy as s

env  = dotenv_values()
hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

# ── 資料一次 ──────────────────────────────────────────────────────────────────
t0 = time.time()
base_df = fetch_kline(s.SYMBOL, s.INTERVAL, s.START, s.END, hdrs)
print(f"資料載入: {time.time()-t0:.1f}s  ({len(base_df):,} bars)\n")

# ── 掃描範圍 ──────────────────────────────────────────────────────────────────
fast_vals = list(range(5,  105, 10))   # 5, 15, 25, ..., 95
slow_vals = list(range(20, 220, 20))   # 20, 40, 60, ..., 200
warmup    = max(slow_vals)

# ── 參數掃描 ──────────────────────────────────────────────────────────────────
t1   = time.time()
grid = scan_grid(
    base_df, s.compute_signals, fast_vals, slow_vals,
    row_param='fast', col_param='slow',
    fee=s.FEE, warmup=warmup,
    valid_fn=lambda f, sl: f < sl,
)
print(f"掃描耗時: {time.time()-t1:.1f}s")

# ── 最佳參數 ──────────────────────────────────────────────────────────────────
best_idx, _, best_fast, best_slow, best_sharpe = find_plateau(grid, fast_vals, slow_vals)
print(f"最佳參數: SMA_FAST={best_fast}, SMA_SLOW={best_slow}  Sharpe={grid[best_idx]:.3f}")

plot_heatmap(
    grid, fast_vals, slow_vals,
    best_idx=best_idx,
    row_label='SMA Fast', col_label='SMA Slow',
    title=f'{s.STRATEGY_NAME} — Sharpe Grid',
    output_path='strategies/btc_sma_cross/heatmap.png',
)
