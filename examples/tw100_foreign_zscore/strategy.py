# Strategy: Taiwan 100 Foreign Institutional Z-Score Portfolio
# Type:     C (multi-asset, weight-based)
# Universe: Taiwan 100 stocks across sectors
# Signal:   外資買超 time-series z-score → positive z → long weight
# Rebalance: weekly (last trading day of each ISO week)

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
MODE          = "backtest"
STRATEGY_NAME = "tw100_foreign_zscore"
START         = "2010-01-01"
END           = None
FEE           = 0.003        # ~0.3% 證交稅 + 手續費（賣方含稅）

ACCUM_WINDOW  = 20           # 累積買賣超：1 個月（交易日）
ZSCORE_WINDOW = 252          # 標準化視窗：1 年
WARMUP        = ACCUM_WINDOW + ZSCORE_WINDOW

UNIVERSE = [
    # 半導體
    '2330', '2303', '2454', '3711', '2337', '2408', '2379', '3034', '3008', '2344',
    # 電子製造 / EMS
    '2317', '2308', '2382', '2357', '4938', '2356', '2376', '2474', '2345', '2353',
    # 零組件 / 面板 / PCB
    '2409', '3481', '3037', '3044', '2395', '2498', '2049', '4536', '6669', '3673',
    # 金融 — 銀行
    '2881', '2882', '2886', '2891', '2884', '5876', '2880', '2885', '2887', '2888',
    # 金融 — 保險 / 證券
    '2892', '2823', '5880', '2834', '2838', '2820', '5871', '2801', '2812', '2855',
    # 石化 / 化工
    '1301', '1303', '1326', '6505', '1402', '1704', '1710', '2104', '1802', '1714',
    # 鋼鐵 / 基礎材料
    '2002', '9910', '1605', '1504', '2006', '2014', '1519', '2034', '2027', '1603',
    # 消費 / 食品 / 零售
    '1216', '2912', '1101', '1102', '9904', '1207', '1210', '2727', '1231', '9921',
    # 電信 / 傳媒 / 公用
    '2412', '4904', '3045', '4406', '9940', '6176', '1590', '2313', '9914', '3533',
    # 航運 / 汽車 / 其他
    '2603', '2609', '2610', '2618', '2615', '2630', '2207', '2201', '4713', '2385',
]


# ── fetch_data ────────────────────────────────────────────────────────────────
def fetch_data(hdrs):
    import pandas as pd
    from lib.data import fetch_twstock_price_adj, fetch_twstock_institutional

    closes, opens, foreign_nets = {}, {}, {}
    for sid in UNIVERSE:
        try:
            price = fetch_twstock_price_adj(sid, START, END, hdrs)
            closes[sid]       = price['Close']
            opens[sid]        = price['Open']
            foreign_nets[sid] = fetch_twstock_institutional(sid, START, END, hdrs)['foreign_net']
        except Exception as e:
            print(f"  skip {sid}: {e}")

    close_df   = pd.DataFrame(closes).sort_index().dropna(how='all')
    open_df    = pd.DataFrame(opens).reindex(close_df.index)
    foreign_df = pd.DataFrame(foreign_nets).reindex(close_df.index).fillna(0)
    return close_df, foreign_df, open_df


# ── compute_signals ───────────────────────────────────────────────────────────
def compute_signals(data):
    import numpy as np
    import pandas as pd

    close_df, foreign_df, open_df = data
    idx = close_df.index

    # ── 累積 1 個月買賣超，再以 1 年視窗 time-series 標準化 ──────────────────
    accum  = foreign_df.rolling(ACCUM_WINDOW, min_periods=1).sum()
    roll   = accum.rolling(ZSCORE_WINDOW, min_periods=ZSCORE_WINDOW // 2)
    z_scores = (accum - roll.mean()) / roll.std().replace(0, float('nan'))

    # ── 每月最後一個交易日為調倉日 ─────────────────────────────────────────────
    is_rebalance = np.zeros(len(idx), dtype=bool)
    for k in range(len(idx) - 1):
        if idx[k].month != idx[k + 1].month:
            is_rebalance[k] = True
    is_rebalance[-1] = True

    # ── 建立權重矩陣（調倉日以正 z-score 等比分配，其餘持倉不動）──────────────
    weights = pd.DataFrame(np.nan, index=idx, columns=close_df.columns)
    for k, t in enumerate(idx):
        if not is_rebalance[k]:
            continue
        z   = z_scores.loc[t].clip(lower=0).fillna(0)
        tot = z.sum()
        weights.loc[t] = z / tot if tot > 0 else 0.0

    weights = weights.ffill().fillna(0.0)
    return weights.values, close_df, open_df


if __name__ == '__main__':
    from lib.runner import run
    from lib.notify import make_sender
    run(locals(), fetch_data, compute_signals, make_sender())
