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
INTERVAL      = "1d"
START         = "2015-01-01"
END           = None
FEE           = 0.005          # ~0.3% 證交稅 + 手續費

MOM_WINDOW     = 120           # 動能回望窗格（交易日）
TOP_N          = 30            # 每週持有前 N 名
REBALANCE_FREQ = 'W'
WARMUP         = MOM_WINDOW

UNIVERSE = [
    # 半導體 / 晶圓代工 / IC 設計
    '2330', '2303', '2454', '3711', '2379', '2344', '2337', '2408',
    '3008', '5347', '3034', '2358', '3533', '6488', '2449', '6415',
    '3690', '3596', '6531', '3088', '4966', '6770', '3714', '2361',
    '2363', '2399', '6719', '2404', '3706', '5483', '3529', '3443',
    '3081', '4967', '6489', '2385', '2347', '2436', '2443', '6669',
    # 電子製造 / EMS / OSAT / PCB
    '2317', '2382', '2357', '2308', '2353', '2354', '2356', '2312',
    '2351', '2376', '4938', '6239', '2395', '2390', '2397', '2387',
    '2393', '2329', '2392', '3006', '2388', '2377', '2360', '2313',
    '2339', '2345', '2368', '2371', '2375', '2380', '2381', '2301',
    '2384', '2386', '2389', '2394', '2396', '2398', '2400', '3037',
    # 伺服器 / 網通 / AI 基礎設施
    '6414', '3017', '3026', '3060', '3680', '3491', '6116',
    '2456', '2462', '2466', '5434', '3490', '6456', '3498',
    '6515', '6550', '6657', '6679', '6741', '4953', '4960',
    '6184', '6271', '3482', '3014', '3022', '6789', '4977',
    # 金融 / 銀行 / 保險 / 證券
    '2881', '2882', '2883', '2884', '2885', '2886', '2887', '2888',
    '2889', '2890', '2891', '2892', '5880', '2823', '2832', '5876',
    '5820', '5888', '2809', '6005', '2801', '2812', '2816', '2820',
    '2834', '2836', '2838', '2845', '2847', '2849',
    # 石化 / 塑膠 / 橡膠
    '1301', '1303', '1326', '6505', '1402', '1477', '1504',
    '1710', '1308', '1309', '2107', '1702', '1707', '2108', '2102',
    # 水泥 / 建材
    '1101', '1102', '1103', '1104', '1108',
    # 鋼鐵 / 金屬 / 線纜
    '2002', '2006', '2007', '2008', '2009', '9921', '2014', '2015',
    '2023', '2024', '2027', '2029', '2038', '2039',
    '1513', '1526', '1590', '1608', '1609', '1614',
    # 電信 / 有線電視
    '2412', '4904', '3045', '6177', '5007', '4999',
    # 航運 / 物流 / 空運
    '2603', '2609', '2615', '2617', '2618', '2610', '5608',
    '2614', '2616', '2606', '2605', '2613',
    # 消費 / 零售 / 百貨
    '2912', '2915', '2903', '9945', '2723', '2727', '2748',
    '2207', '6279', '2104', '2105', '9910', '9914',
    # 食品 / 飲料
    '1216', '1215', '1203', '1210', '1213', '1218', '1225',
    '1229', '1232', '1234', '1201', '1202', '1205', '1207',
    # 建設 / 不動產
    '2501', '2504', '2505', '2511', '2515', '2520', '5522',
    '2524', '2527', '2528', '5538', '5534', '2530', '2542',
    # 汽車 / 零件
    '2201', '2204', '2209', '2103',
    # 生技 / 製藥 / 醫療器材
    '4174', '4746', '4719', '4762', '6552', '4162', '4168',
    '1722', '4725', '4755', '4743', '4728', '4739', '6547', '4126',
    # 機械 / 工具機 / 工業
    '2049', '1538', '2059', '2316', '2332', '2340', '2355',
    '3016', '3024', '3189', '4536', '4958', '1560', '1537',
    # 觀光 / 餐飲
    '2701', '2702', '2704', '2706', '2707', '2712',
    # 其他科技 / 顯示器 / 光學
    '2409', '3481', '2498', '3006', '2474', '2475', '2478',
    '3373', '3374', '3576', '5243', '6510', '8101', '8103',
    '6230', '6235', '6277', '6285', '8086', '3703',
    # 補足至 300
    '2352', '8046', '3231', '9917', '2367',
]

# deduplicate while preserving order
UNIVERSE = list(dict.fromkeys(UNIVERSE))


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
    if close_df.empty:
        raise RuntimeError("fetch_data: no price data — check UNIVERSE, credentials, and START/END dates")
    open_df  = pd.DataFrame(opens).reindex(close_df.index)
    return close_df, open_df


# ── compute_signals ───────────────────────────────────────────────────────────
def compute_signals(data, mom_window=MOM_WINDOW, top_n=TOP_N, rebalance_freq=REBALANCE_FREQ):
    import numpy as np
    import pandas as pd

    close_df, open_df = data

    mom          = _momentum(close_df, mom_window)
    is_rebalance = _rebalance_mask(close_df.index, freq=rebalance_freq)
    weights      = _top_n_weights(mom, top_n, is_rebalance)

    price_df = pd.concat({'close': close_df, 'open': open_df}, axis=1)
    return weights.values, price_df


if __name__ == '__main__':
    from lib.runner import run
    from lib.notify import make_sender
    try:
        sender = make_sender()
    except FileNotFoundError:
        sender = None
    run(locals(), fetch_data, compute_signals, sender)
