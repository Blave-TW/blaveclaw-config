import sys, time, warnings
warnings.filterwarnings('ignore', category=FutureWarning)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import dotenv_values
from lib.data import fetch_kline
from lib.param_scan import scan_grid, find_plateau, on_edge, extend_axis, write_scan, plot_heatmap
import strategy as s

env  = dotenv_values()
hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

# ── 資料一次 ──────────────────────────────────────────────────────────────────
t0 = time.time()
base_df = fetch_kline(s.SYMBOL, s.INTERVAL, s.START, s.END, hdrs)
print(f"資料載入: {time.time()-t0:.1f}s  ({len(base_df):,} bars)\n")

# ── 掃描範圍 ──────────────────────────────────────────────────────────────────
# 每軸 10 格、步長是有交易意義的 K 棒數(10 / 20 根),100 組合幾秒掃完;
# 步長再細,相鄰格只差雜訊、鄰域平均就退化成單格。
fast_vals = list(range(5,  105, 10))   # 5, 15, 25, ..., 95
slow_vals = list(range(20, 220, 20))   # 20, 40, 60, ..., 200

# ── 參數掃描 ──────────────────────────────────────────────────────────────────
def scan(fast_vals, slow_vals):
    t1   = time.time()
    grid = scan_grid(
        base_df, s.compute_signals, fast_vals, slow_vals,
        row_param='fast', col_param='slow',
        fee=s.FEE, warmup=max(slow_vals),      # 軸延伸後 warmup 跟著最長的窗
        valid_fn=lambda f, sl: f < sl,
    )
    print(f"掃描 {len(fast_vals)}×{len(slow_vals)} 組合,耗時 {time.time()-t1:.1f}s")
    return grid, find_plateau(grid, fast_vals, slow_vals)

grid, (best_idx, nbr_mean, best_fast, best_slow, best_sharpe) = scan(fast_vals, slow_vals)

# 穩健點落在網格邊緣 → 往那側沿同步長延伸 5 格、重掃一次(只延伸一次,仍算同一次迭代)。
# K 棒數軸給 floor=1,往下延伸不會產生 0 或負的均線長度。
edges = on_edge(best_idx, grid.shape)
for axis, side in edges:
    if axis == 0:
        fast_vals = extend_axis(fast_vals, side, floor=1)
    else:
        slow_vals = extend_axis(slow_vals, side, floor=1)
if edges and len(fast_vals) * len(slow_vals) > grid.size:   # 真的長大了才重掃(撞到 40 上限或 floor 就不重掃)
    grid, (best_idx, nbr_mean, best_fast, best_slow, best_sharpe) = scan(fast_vals, slow_vals)

print(f"穩健參數: SMA_FAST={best_fast}, SMA_SLOW={best_slow}  鄰域 Sharpe={best_sharpe:.3f}  單格 Sharpe={grid[best_idx]:.3f}")

# scan.json → web 穩健參數分頁(掃完必寫)
write_scan(grid, fast_vals, slow_vals, nbr_mean, best_idx,
           output_dir=str(Path(__file__).parent),
           row_param='SMA_FAST', col_param='SMA_SLOW', fee=s.FEE,
           start=s.START, end=base_df.index[-1].strftime('%Y-%m-%d'),
           current=(s.SMA_FAST, s.SMA_SLOW))

plot_heatmap(
    grid, fast_vals, slow_vals,
    best_idx=best_idx,
    row_label='SMA Fast', col_label='SMA Slow',
    title=f'{s.STRATEGY_NAME} — Sharpe Grid',
    output_path=str(Path(__file__).parent / 'heatmap.png'),
)
