# Strategy: BTC SMA Cross
# Type:     A (single symbol, signal-based)
# Symbol:   BTCUSDT
# Interval: 1h
# Logic:    Long on SMA20/SMA50 golden cross, flat on death cross

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
MODE          = "backtest"
STRATEGY_NAME = "btc_sma_cross"
SYMBOL        = "BTCUSDT"
INTERVAL      = "1h"
START         = "2022-01-01"
END           = None
FEE           = 0.0005

SMA_FAST = 45
SMA_SLOW = 100
WARMUP   = SMA_SLOW


# ── indicators ────────────────────────────────────────────────────────────────
def _add_indicators(df, fast=SMA_FAST, slow=SMA_SLOW):
    df = df.copy()
    df['SMA_F'] = df['Close'].rolling(fast).mean()
    df['SMA_S'] = df['Close'].rolling(slow).mean()
    return df


# ── fetch_data ────────────────────────────────────────────────────────────────
def fetch_data(hdrs):
    from lib.data import fetch_kline
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    return _add_indicators(df)


# ── compute_signals ───────────────────────────────────────────────────────────
def compute_signals(df):
    import pandas as pd, numpy as np
    signal = pd.Series(np.nan, index=df.index)
    golden = (df['SMA_F'] > df['SMA_S'])
    death  = (df['SMA_F'] < df['SMA_S']) 
    signal[golden] = 1.0
    signal[death]  = 0.0
    return signal


if __name__ == '__main__':
    from lib.runner import run
    from lib.notify import make_sender
    run(locals(), fetch_data, compute_signals, make_sender())
