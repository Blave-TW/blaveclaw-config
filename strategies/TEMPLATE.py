# Strategy: [strategy name]
# Symbol:   BTCUSDT
# Interval: 1h
# Logic:    [entry/exit rules]

import sys, numpy as np, pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "skills" / "blave-quant"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from backtesting import Backtest, Strategy

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
# FILL IN: Signal logic
# Returns float: 1.0=long  -1.0=short  0.0=flat  (fractions ok)
# ─────────────────────────────────────────────────────────────
def compute_signal(row) -> float:
    direction = 0.0
    # if row['Close'] > row['SMA20']:
    #     direction = 1.0
    # elif row['Close'] < row['SMA20']:
    #     direction = -1.0

    if direction == 0.0 or not VOL_TARGETING:
        return direction

    vol = row.get('realized_vol')
    if not vol or np.isnan(vol) or vol <= 0:
        return 0.0
    return direction * min(TARGET_VOL / vol, VOL_CAP)


def send_telegram(msg):
    pass


if __name__ == '__main__':
    from lib.runner import run
    run(locals(), add_indicators, compute_signal, send_telegram_fn=send_telegram)
