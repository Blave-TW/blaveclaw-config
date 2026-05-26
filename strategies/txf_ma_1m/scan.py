import sys, time, warnings
warnings.filterwarnings('ignore', category=FutureWarning)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np
from dotenv import dotenv_values
from lib.data import fetch_twfutures_ohlcv
from lib.strategy import add_realized_vol
from lib.analysis import precise_pnl, compute_stats
from lib.param_scan import find_plateau, plot_heatmap
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
grid      = np.full((len(fast_vals), len(slow_vals)), np.nan)
total     = len(fast_vals) * len(slow_vals)
warmup    = max(max(slow_vals), s.VOL_WINDOW)

# ── 掃描迴圈 ─────────────────────────────────────────────────────────────────
t1 = time.time()
done = 0
for i, fast in enumerate(fast_vals):
    for j, slow in enumerate(slow_vals):
        done += 1
        if fast >= slow:
            continue

        df               = s._add_indicators(base_df, fast, slow)
        signal_raw, settle_raw = s.compute_signals(df)

        df_t     = df.iloc[warmup:]
        signal_t = signal_raw.iloc[warmup:]
        settle_t = settle_raw.iloc[warmup:].values.astype(bool)

        n      = len(df_t)
        pos    = signal_t.ffill().fillna(0).values
        w_curr = np.empty(n); w_curr[0] = 0.0; w_curr[1:] = pos[:-1]
        w_prev = np.zeros(n)
        if n >= 2: w_prev[2:] = pos[:-2]

        exec_shifted     = np.zeros(n, dtype=bool)
        exec_shifted[1:] = settle_t[:-1]

        pf_ret, *_ = precise_pnl(df_t['Close'].values, df_t['Open'].values,
                                  w_curr, w_prev, exec_shifted, s.FEE)
        sharpe, *_ = compute_stats(pf_ret, df_t.index)
        if np.isfinite(sharpe):
            grid[i, j] = sharpe

        if done % 5 == 0 or done == total:
            print(f"  {done}/{total}  fast={fast} slow={slow}  sharpe={grid[i,j]:.3f}" if np.isfinite(grid[i,j]) else f"  {done}/{total}  skip")

scan_time = time.time() - t1
print(f"\n掃描耗時: {scan_time:.1f}s")

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
