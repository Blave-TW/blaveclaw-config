# Strategy: [strategy name]
# Symbol:   BTCUSDT
# Interval: 1h
# Logic:    [entry/exit rules]

import sys, numpy as np, pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))  # → blaveclaw-config/

# --- Config ---
MODE             = "backtest"        # "backtest" | "paper" | "live"
STRATEGY_NAME    = "[strategy_name]"
SYMBOL           = "BTCUSDT"
EXCHANGE         = "binance"
INTERVAL         = "1h"
START            = "2024-01-01"
END              = None
FEE              = 0.0005
VOL_TARGETING    = False             # True: size position by realized volatility
TARGET_VOL       = 0.10             # (VOL_TARGETING) annualized target vol, e.g. 0.10 = 10%
VOL_LOOKBACK     = 720              # (VOL_TARGETING) lookback candles, e.g. 720 = 30d on 1h
PERIODS_PER_YEAR = 8760             # (VOL_TARGETING) 1h=8760  4h=2190  1d=365
VOL_CAP          = 2.0              # (VOL_TARGETING) max scale factor


# ─────────────────────────────────────────────────────────────
# FILL IN: Add indicator columns to df
# For Blave alpha indicators (holder concentration, taker intensity,
# liquidation, whale hunter, etc.): read
# skills/blave-quant/examples/backtest-holder-concentration.md
# for the correct fetch pattern, then fetch and merge into df here.
# ─────────────────────────────────────────────────────────────
def add_indicators(df):
    # df['SMA20'] = df['Close'].rolling(20).mean()
    return df


# ─────────────────────────────────────────────────────────────
# FILL IN: Signal logic (vectorized)
# Receives the full df (including 'realized_vol' if VOL_TARGETING=True).
# Returns pd.Series: positive float=long, -1.0=short, 0.0=flat, nan=hold
# ─────────────────────────────────────────────────────────────
def compute_signals(df) -> pd.Series:
    signal = pd.Series(np.nan, index=df.index)

    # Example: long when Close > SMA20
    # long_mask  = df['Close'] > df['SMA20']
    # short_mask = df['Close'] < df['SMA20']
    # signal[long_mask]  = 1.0
    # signal[short_mask] = -1.0

    if not VOL_TARGETING:
        return signal

    # Scale by realized vol
    vol = df.get('realized_vol', pd.Series(np.nan, index=df.index))
    size = (TARGET_VOL / vol).clip(upper=VOL_CAP)
    signal[signal > 0]  = size[signal > 0]
    signal[signal < 0]  = -size[signal < 0]
    return signal


if __name__ == '__main__':
    from lib.runner import run
    from lib.notify import make_sender
    run(locals(), add_indicators, compute_signals, send_telegram_fn=make_sender())
