# Strategy: BTC Taker Intensity 5min
# Type:     A (single symbol, signal-based)
# Symbol:   BTCUSDT
# Interval: 5min
# Logic:    Long when TI 24h > ENTRY_TH, flat when < EXIT_TH (dead zone)

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
MODE          = "backtest"
STRATEGY_NAME = "btc_ti_5min"
SYMBOL        = "BTCUSDT"
INTERVAL      = "5min"
START         = "2023-01-01"
END           = None
FEE           = 0.0005

ENTRY_TH = 1.693
EXIT_TH  = -0.453


# ── indicators ────────────────────────────────────────────────────────────────
# TI 本身不隨 threshold 變動，所以 scan 時只需呼叫一次
def _add_indicators(df, hdrs):
    from lib.data import fetch_taker_intensity
    df = df.copy()
    ti = fetch_taker_intensity(SYMBOL, INTERVAL, START, END, hdrs, timeframe='24h')
    df = df.join(ti.rename(columns={'alpha': 'TI'}))
    df['TI'] = df['TI'].ffill()
    return df


# ── fetch_data ────────────────────────────────────────────────────────────────
def fetch_data(hdrs):
    from lib.data import fetch_kline
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    return _add_indicators(df, hdrs)


# ── compute_signals ───────────────────────────────────────────────────────────
def compute_signals(df, entry_th=ENTRY_TH, exit_th=EXIT_TH):
    import pandas as pd, numpy as np
    signal = pd.Series(np.nan, index=df.index)
    signal[df['TI'] > entry_th] = 1.0
    signal[df['TI'] < exit_th]  = 0.0
    return signal


if __name__ == '__main__':
    from lib.runner import run
    try:
        from lib.notify import make_sender
        sender = make_sender()
    except Exception:
        sender = None
    run(locals(), fetch_data, compute_signals, sender)
