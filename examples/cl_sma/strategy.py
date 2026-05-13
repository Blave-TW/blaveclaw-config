# Strategy: WTI Crude Oil SMA Cross
# Type:     A (single symbol, signal-based)
# Symbol:   CL (NYMEX front-month continuous)
# Interval: 1h
# Logic:    Long on SMA golden cross, flat on death cross
#           Settlement exit (-1.0) on last bar of NYMEX CL expiry day

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
MODE          = "backtest"        # "backtest" | "paper" | "live"
STRATEGY_NAME = "cl_sma"
SYMBOL        = "CL"
EXCHANGE      = "nymex"
INTERVAL      = "1h"
START         = "2015-01-01"
END           = None
FEE           = 0.0003            # ~0.03% per side（CME 手續費 + 點差）
DATASET       = "GLBX.MDP3"
SCHEMA        = "ohlcv-1h"

SMA_FAST = 24        # 1 天
SMA_SLOW = 24 * 7   # 1 週
WARMUP   = SMA_SLOW


# ── NYMEX WTI CL 結算日 ───────────────────────────────────────────────────────
# 規則：每月 25 日（或前一交易日）往前第 3 個交易日
def _cl_settlement_dates(start, end):
    from datetime import date, timedelta
    import pandas as pd
    s_year = pd.Timestamp(start).year
    e_year = (pd.Timestamp(end) if end else pd.Timestamp.now()).year
    dates = set()
    for year in range(s_year - 1, e_year + 2):
        for month in range(1, 13):
            anchor = date(year, month, 25)
            d = anchor
            while d.weekday() >= 5:
                d -= timedelta(days=1)
            count = 0
            while count < 3:
                d -= timedelta(days=1)
                if d.weekday() < 5:
                    count += 1
            dates.add(d)
    return dates


# ── indicators ────────────────────────────────────────────────────────────────
def _add_indicators(df, fast=SMA_FAST, slow=SMA_SLOW):
    df = df.copy()
    df['SMA_F'] = df['Close'].rolling(fast).mean()
    df['SMA_S'] = df['Close'].rolling(slow).mean()
    return df


# ── fetch_data ────────────────────────────────────────────────────────────────
def fetch_data(hdrs):
    from lib.data import fetch_db_kline
    df = fetch_db_kline(DATASET, SYMBOL, SCHEMA, START, END, hdrs)
    return _add_indicators(df, SMA_FAST, SMA_SLOW)


# ── compute_signals ───────────────────────────────────────────────────────────
def compute_signals(df):
    import pandas as pd, numpy as np

    signal = pd.Series(np.nan, index=df.index)

    golden = (df['SMA_F'] > df['SMA_S']) 
    death  = (df['SMA_F'] < df['SMA_S']) 
    signal[golden] = 1.0
    signal[death]  = 0.0

    # 結算出場：結算日最後一根 bar 強制平倉
    settle_dates = _cl_settlement_dates(START, END)
    idx_dates    = pd.to_datetime(df.index.date)
    for d in settle_dates:
        mask = idx_dates == pd.Timestamp(d)
        if mask.any():
            signal.loc[df.index[mask][-1]] = -1.0

    return signal


if __name__ == '__main__':
    from lib.runner import run
    from lib.notify import make_sender
    run(locals(), fetch_data, compute_signals, make_sender())
