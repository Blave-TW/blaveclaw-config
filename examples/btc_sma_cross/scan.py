import sys, time, warnings
warnings.filterwarnings('ignore', category=FutureWarning)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np
from dotenv import dotenv_values
from lib.data import fetch_kline
from lib.runner import backtest_signals
from lib.param_scan import find_plateau, plot_heatmap
import strategy as s

env  = dotenv_values()
hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

# ── 資料一次 ──────────────────────────────────────────────────────────────────
t0 = time.time()
base_df = fetch_kline(s.SYMBOL, s.INTERVAL, s.START, s.END, hdrs)
print(f"資料載入: {time.time()-t0:.1f}s  ({len(base_df):,} bars)\n")

# ── 掃描範圍（線性均分）───────────────────────────────────────────────────────
fast_vals = list(range(5,  105, 10))   # 5, 15, 25, ..., 95
slow_vals = list(range(20, 220, 20))   # 20, 40, 60, ..., 200
grid      = np.full((len(fast_vals), len(slow_vals)), np.nan)
total     = len(fast_vals) * len(slow_vals)

# ── 掃描迴圈 ─────────────────────────────────────────────────────────────────
t1 = time.time()
for i, fast in enumerate(fast_vals):
    for j, slow in enumerate(slow_vals):
        if fast >= slow:
            continue

        df      = s._add_indicators(base_df, fast, slow)
        signals = s.compute_signals(df)

        # warmup = slow（最長的 rolling window）
        df      = df.iloc[slow:]
        signals = signals.iloc[slow:]

        pf = backtest_signals(df['Close'], signals, fee=s.FEE, freq='1h')
        sharpe = pf.stats()['Sharpe Ratio']
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
    output_path='strategies/btc_sma_cross/heatmap.png',
)
