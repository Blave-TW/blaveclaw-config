# Strategy: [strategy name]
# Type:     A (single symbol, signal-based)
# Symbol:   BTCUSDT
# Interval: 1h
# Logic:    [entry/exit rules]

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
MODE          = "backtest"        # "backtest" | "paper" | "live"
STRATEGY_NAME = "[strategy_name]"
SYMBOL        = "BTCUSDT"
INTERVAL      = "1h"
START         = "2024-01-01"
END           = None
FEE           = 0.0005

# PARAM1 = ...
# PARAM2 = ...
# WARMUP = PARAM1 + PARAM2   # bars to skip at start of backtest (sum of rolling windows)


# ── indicators ────────────────────────────────────────────────────────────────
# Called by fetch_data (normal run) and scan.py (param scan) with different params.
def _add_indicators(df, param1, param2):
    df = df.copy()
    # df['SMA_F'] = df['Close'].rolling(param1).mean()
    # df['SMA_S'] = df['Close'].rolling(param2).mean()
    return df


# ── fetch_data ────────────────────────────────────────────────────────────────
def fetch_data(hdrs):
    from lib.data import fetch_kline
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    return _add_indicators(df, param1=None, param2=None)


# ── compute_signals ───────────────────────────────────────────────────────────
# Returns pd.Series:  1.0=long  0.0=flat  nan=hold  -1.0=settlement
def compute_signals(df):
    import pandas as pd, numpy as np
    signal = pd.Series(np.nan, index=df.index)

    # signal[df['SMA_F'] > df['SMA_S']] = 1.0
    # signal[df['SMA_F'] < df['SMA_S']] = 0.0

    return signal


if __name__ == '__main__':
    from lib.runner import run
    from lib.notify import make_sender
    run(locals(), fetch_data, compute_signals, make_sender())
