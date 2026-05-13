# Strategy: [strategy name]
# Type:     A (single symbol, signal-based)
# Symbol:   BTCUSDT
# Interval: 1h
# Logic:    [entry/exit rules]

import sys, numpy as np, pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))  # → blaveclaw-config/

# ── Config ────────────────────────────────────────────────────────────────────
MODE             = "backtest"        # "backtest" | "paper" | "live"
STRATEGY_NAME    = "[strategy_name]"
SYMBOL           = "BTCUSDT"
EXCHANGE         = "binance"
INTERVAL         = "1h"
START            = "2024-01-01"
END              = None
FEE              = 0.0005
VOL_TARGETING    = False
TARGET_VOL       = 0.10
VOL_LOOKBACK     = 720              # bars
PERIODS_PER_YEAR = 8760             # 1h=8760  4h=2190  1d=365
VOL_CAP          = 2.0


# ── fetch_data ────────────────────────────────────────────────────────────────
# Fetches kline, adds indicators, adds realized_vol if VOL_TARGETING.
# Returns a single DataFrame — runner passes it directly to compute_signals.
def fetch_data(hdrs):
    from lib.data import fetch_kline
    today = __import__('datetime').datetime.utcnow().strftime('%Y-%m-%d')
    df = fetch_kline(SYMBOL, INTERVAL, START, END if MODE == 'backtest' else today, hdrs)

    # ── Add indicators ────────────────────────────────────────────────────────
    # df['SMA20'] = df['Close'].rolling(20).mean()

    # ── Vol targeting (optional) ──────────────────────────────────────────────
    if VOL_TARGETING:
        log_ret = np.log(df['Close'] / df['Close'].shift(1))
        df['realized_vol'] = log_ret.rolling(VOL_LOOKBACK).std() * np.sqrt(PERIODS_PER_YEAR)

    return df


# ── compute_signals ───────────────────────────────────────────────────────────
# Pure vectorized function — no I/O, no side effects.
# Returns pd.Series: positive float=long, 0.0=flat, nan=hold
def compute_signals(df) -> pd.Series:
    signal = pd.Series(np.nan, index=df.index)

    # Example: long when Close > SMA20, flat when below
    # long_mask  = df['Close'] > df['SMA20']
    # flat_mask  = df['Close'] < df['SMA20']
    # signal[long_mask] = 1.0
    # signal[flat_mask] = 0.0

    if not VOL_TARGETING:
        return signal

    vol  = df.get('realized_vol', pd.Series(np.nan, index=df.index))
    size = (TARGET_VOL / vol).clip(upper=VOL_CAP)
    signal[signal > 0] = size[signal > 0]
    return signal


if __name__ == '__main__':
    from lib.runner import run
    from lib.notify import make_sender
    run(locals(), fetch_data, compute_signals, send_telegram_fn=make_sender())
