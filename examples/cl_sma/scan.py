import sys, time, warnings
warnings.filterwarnings('ignore', category=FutureWarning)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np
from dotenv import dotenv_values
from lib.analysis import precise_pnl, compute_stats
from lib.param_scan import find_plateau, plot_heatmap
import strategy as s

env  = dotenv_values()
hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

# ── 資料一次 ──────────────────────────────────────────────────────────────────
t0 = time.time()
base_df = s.fetch_data(hdrs)
print(f"資料載入: {time.time()-t0:.1f}s  ({len(base_df):,} bars)\n")

# ── 掃描範圍 ──────────────────────────────────────────────────────────────────
fast_vals = list(range(4,  52, 4))    #  4, 8, 12, ..., 48
slow_vals = list(range(24, 240, 24))  # 24, 48, 72, ..., 216
grid      = np.full((len(fast_vals), len(slow_vals)), np.nan)
total     = len(fast_vals) * len(slow_vals)

# ── 掃描迴圈 ─────────────────────────────────────────────────────────────────
t1 = time.time()
for i, fast in enumerate(fast_vals):
    for j, slow in enumerate(slow_vals):
        if fast >= slow:
            continue

        df  = s._add_indicators(base_df, fast, slow)
        sig = s.compute_signals(df)
        if isinstance(sig, tuple):
            sig = sig[0]   # unwrap (signals, exec_at_close)

        df  = df.iloc[slow:]
        sig = sig.iloc[slow:]

        pos    = sig.ffill().fillna(0).values
        n      = len(df)
        w_curr = np.empty(n); w_curr[0] = 0.0; w_curr[1:] = pos[:-1]
        w_prev = np.zeros(n)
        if n >= 2: w_prev[2:] = pos[:-2]

        # settlement bars: exec_at_close（不算 gap，但這裡掃描只比 Sharpe 相對值，影響極小）
        exec_shifted = np.zeros(n, dtype=bool)
        if 'instrument_id' in df.columns:
            ea = (df['instrument_id'] != df['instrument_id'].shift(-1)).fillna(False).values
            exec_shifted[1:] = ea[:-1]

        pf_ret, *_ = precise_pnl(df['Close'].values, df['Open'].values,
                                  w_curr, w_prev, exec_shifted, s.FEE)
        sharpe, *_ = compute_stats(pf_ret, df.index)
        if np.isfinite(sharpe):
            grid[i, j] = sharpe

scan_time = time.time() - t1
print(f"掃描耗時: {scan_time:.1f}s  (平均 {scan_time/total*1000:.0f}ms / 組合)")

# ── 最佳參數 ──────────────────────────────────────────────────────────────────
best_idx, _, best_fast, best_slow = find_plateau(grid, fast_vals, slow_vals)
print(f"最佳參數: SMA_FAST={best_fast}, SMA_SLOW={best_slow}  Sharpe={grid[best_idx]:.3f}")

plot_heatmap(
    grid, fast_vals, slow_vals,
    best_idx=best_idx,
    row_label='SMA Fast', col_label='SMA Slow',
    title=f'{s.STRATEGY_NAME} — Sharpe Grid',
    output_path='strategies/cl_sma/heatmap.png',
)
