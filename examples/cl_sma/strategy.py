# Strategy: WTI Crude Oil SMA Cross
# Type:     A (single symbol, signal-based)
# Symbol:   CL (NYMEX front-month continuous)
# Interval: 1h
# Logic:    Long on SMA golden cross, flat on death cross
#           Settlement exit (0.0) on last bar before instrument_id rollover

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

    # 結算出場：換約前最後一根 bar 強制平倉（this-bar close），一般訊號為 next-bar open
    from lib.data import settlement_signals_from_db
    return settlement_signals_from_db(df, signal)


if __name__ == '__main__':
    from lib.runner import run
    from lib.notify import make_sender
    run(locals(), fetch_data, compute_signals, make_sender())
