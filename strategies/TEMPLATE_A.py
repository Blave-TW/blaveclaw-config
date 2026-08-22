# Strategy: [strategy name]
# Type:     A (single symbol, signal-based)
# Symbol:   BTCUSDT
# Market:   swap | spot — ask the user at creation; fixed once deployed
# Interval: 1h
# Logic:    [entry/exit rules]

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
MODE          = "backtest"        # "backtest" | "live"
STRATEGY_NAME = "[strategy_name]"
DISPLAY_NAME  = "[human-facing name, user's language — what it trades + does]"
DESCRIPTION   = "[one plain sentence]"
SYMBOL        = "BTCUSDT"
MARKET        = "swap"            # "swap" | "spot" — part of the instrument's identity; ask the user, fixed once deployed
INTERVAL      = "1h"              # the platform's signal-refresh schedule follows this
START         = "2024-01-01"
END           = None
FEE           = 0.0005            # VERIFIED real rate for this symbol/venue — never 0, never copied unchecked

# PARAM1 = ...
# PARAM2 = ...
# WARMUP = PARAM1 + PARAM2   # bars to skip at start of backtest (sum of rolling windows)

# MUST when an indicator drives entries/exits: the 1–2 df columns (added by _add_indicators)
# that explain the trades — price-unit series overlay, oscillators sub-pane (references/plot-series.md)
PLOT_SERIES = {"SMA fast": ("SMA_F", {"overlay": True}), "SMA slow": ("SMA_S", {"overlay": True})}


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
# Returns pd.Series: positive=long, negative=short, 0=flat, nan=hold.
# Fractional values scale exposure (0.5 = half size, 1.8 = 1.8×, e.g. vol
# scaling). Settlement/rollover is NOT -1.0 (that opens a short) — use
# settlement_signals_from_db() / txf_settlement_mask() per references/lib.md.
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
