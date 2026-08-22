# Strategy: Taiwan Index Futures SMA Cross (1m)
# Type:     A (single symbol, signal-based)
# Symbol:   TXF (台指期)
# Interval: 1m
# Logic:    Long on golden cross, flat on death cross

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
MODE          = "backtest"        # "backtest" | "live"
STRATEGY_NAME = "txf_ma_1m"
SYMBOL        = "TXF"
INTERVAL      = "1m"
START         = "2023-01-01"
END           = None
FEE           = 0.001             # NT$200 commission + slippage 1-2 ticks on NT$4M contract

SMA_FAST   = 1500   # ~1 週 (300 min/day × 5 days)
SMA_SLOW   = 8000   # ~3.7 週 (300 min/day × 26 days)
VOL_WINDOW = 21600  # 30 天 (720 bars/day × 30)
WARMUP     = max(SMA_SLOW, VOL_WINDOW)

PLOT_SERIES = {"SMA 1500": ("SMA_F", {"overlay": True}), "SMA 8000": ("SMA_S", {"overlay": True})}


# ── indicators ────────────────────────────────────────────────────────────────
def _add_indicators(df, fast=SMA_FAST, slow=SMA_SLOW):
    df = df.copy()
    df['SMA_F'] = df['Close'].rolling(fast).mean()
    df['SMA_S'] = df['Close'].rolling(slow).mean()
    return df


# ── fetch_data ────────────────────────────────────────────────────────────────
def fetch_data(hdrs):
    from lib.data import fetch_twfutures_ohlcv
    from lib.strategy import add_realized_vol
    df = fetch_twfutures_ohlcv(SYMBOL, '1m', START, END, hdrs)
    add_realized_vol(df, lookback=VOL_WINDOW, periods_per_year=252000)
    return _add_indicators(df)   # PLOT_SERIES reads the columns off this df


# ── compute_signals ───────────────────────────────────────────────────────────
def compute_signals(df, fast=SMA_FAST, slow=SMA_SLOW):
    import pandas as pd, numpy as np
    from lib.data import txf_settlement_mask
    from lib.strategy import apply_vol_scaling

    df     = _add_indicators(df, fast, slow)
    signal = pd.Series(np.nan, index=df.index)
    signal[df['SMA_F'] > df['SMA_S']] = 1.0
    signal[df['SMA_F'] < df['SMA_S']] = 0.0

    settle = txf_settlement_mask(df.index)
    signal[settle] = 0.0
    signal = apply_vol_scaling(signal, df)

    return signal, settle


if __name__ == '__main__':
    from lib.runner import run
    from lib.notify import make_sender
    run(locals(), fetch_data, compute_signals, make_sender())
