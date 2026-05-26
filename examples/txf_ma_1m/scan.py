import sys, time, warnings
warnings.filterwarnings('ignore', category=FutureWarning)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import dotenv_values
from lib.data import fetch_twfutures_ohlcv
from lib.strategy import add_realized_vol
from lib.param_scan import scan_grid, find_plateau, plot_heatmap
import strategy as s

env  = dotenv_values()
hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

# ── 資料一次，realized_vol 一次 ───────────────────────────────────────────────
t0 = time.time()
base_df = fetch_twfutures_ohlcv(s.SYMBOL, '1m', s.START, s.END, hdrs)
add_realized_vol(base_df, lookback=s.VOL_WINDOW, periods_per_year=252000)
print(f"資料載入: {time.time()-t0:.1f}s  ({len(base_df):,} bars)\n")

# ── 掃描範圍 ──────────────────────────────────────────────────────────────────
fast_vals = list(range(500,  3001, 500))
slow_vals = list(range(2000, 12001, 2000))
warmup    = max(max(slow_vals), s.VOL_WINDOW)

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
best_idx, _, best_fast, best_slow = find_plateau(grid, fast_vals, slow_vals)
print(f"最佳參數: SMA_FAST={best_fast}, SMA_SLOW={best_slow}  Sharpe={grid[best_idx]:.3f}")

plot_heatmap(
    grid, fast_vals, slow_vals,
    best_idx=best_idx,
    row_label='SMA Fast (1m bars)', col_label='SMA Slow (1m bars)',
    title=f'{s.STRATEGY_NAME} — Sharpe Heatmap (with vol scaling)',
    output_path='strategies/txf_ma_1m/heatmap.png',
)
