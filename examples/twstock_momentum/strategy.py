# Strategy: 台股動能輪動
# Type:     C (multi-asset, weight-based, weekly rebalancing)
# Universe: 台股藍籌 20 支（跨產業）
# Signal:   60 日價格動能排名，每週選前 5 名等權配置
# Rebalance: 每週最後一個交易日

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
MODE          = "backtest"
STRATEGY_NAME = "twstock_momentum"
START         = "2015-01-01"
END           = None
FEE           = 0.003          # ~0.3% 證交稅 + 手續費

MOM_WINDOW  = 80               # 動能回望窗格（交易日）
TOP_N       = 7                # 每週持有前 N 名
WARMUP      = MOM_WINDOW

UNIVERSE = [
    # 半導體
    '2330', '2303', '2454', '3711',
    # 電子製造
    '2317', '2382', '2357', '2308',
    # 金融
    '2881', '2882', '2886', '2891',
    # 石化 / 鋼鐵
    '1301', '1303', '2002',
    # 電信 / 航運 / 消費
    '2412', '2603', '2609', '1216', '2912',
]


# ── helpers ───────────────────────────────────────────────────────────────────
def _momentum(close_df, window=MOM_WINDOW):
    """N 日報酬率（不含複利）"""
    return close_df.pct_change(window, fill_method=None)


def _top_n_weights(signal_df, n, is_rebalance):
    """signal_df 排名前 n 的欄位等權配置；非調倉日 ffill，資料不足全 0。"""
    import numpy as np
    rank = signal_df.rank(axis=1, ascending=False, na_option='bottom')
    w = signal_df.__class__(
        np.where(rank <= n, 1.0 / n, 0.0),
        index=signal_df.index, columns=signal_df.columns
    )
    w[signal_df.isna().all(axis=1)] = 0.0
    w[~is_rebalance] = np.nan
    return w.ffill().fillna(0.0)


def _rebalance_mask(idx, freq='W'):
    """Return bool numpy array — True on rebalance bars (last bar of each period)."""
    import pandas as pd
    import numpy as np
    if freq == 'D':
        return np.ones(len(idx), dtype=bool)
    s = pd.Series(idx.to_period(freq), index=idx)
    return (s != s.shift(-1)).fillna(True).to_numpy()


# ── fetch_data ────────────────────────────────────────────────────────────────
def fetch_data(hdrs):
    import pandas as pd
    from lib.data import fetch_twstock_price_adj

    closes, opens = {}, {}
    for sid in UNIVERSE:
        try:
            price = fetch_twstock_price_adj(sid, START, END, hdrs)
            closes[sid] = price['Close']
            opens[sid]  = price['Open']
        except Exception as e:
            print(f"  skip {sid}: {e}")

    close_df = pd.DataFrame(closes).sort_index().dropna(how='all')
    open_df  = pd.DataFrame(opens).reindex(close_df.index)
    return close_df, open_df


# ── compute_signals ───────────────────────────────────────────────────────────
def compute_signals(data, mom_window=MOM_WINDOW, top_n=TOP_N):
    import numpy as np
    import pandas as pd

    close_df, open_df = data

    mom          = _momentum(close_df, mom_window)
    is_rebalance = _rebalance_mask(close_df.index, freq='W')
    weights      = _top_n_weights(mom, top_n, is_rebalance)

    price_df = pd.concat({'close': close_df, 'open': open_df}, axis=1)
    return weights.values, price_df


if __name__ == '__main__':
    from lib.runner import run
    from lib.notify import make_sender
    run(locals(), fetch_data, compute_signals, make_sender())
