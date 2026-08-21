# Strategy: 2317 Broker Flow Z-Score Entry/Exit
# Type:     A (single symbol, signal-based)
# Signal:   Rolling WINDOW-day per-branch net → count(net>0) − count(net<0)
#           Z-score normalised over ZSCORE_WIN rolling window (contrarian long only)

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
MODE          = "backtest"
STRATEGY_NAME = "tw2317_broker_zscore"
SYMBOL        = "2317"
INTERVAL      = "1d"
START         = "2022-01-01"
END           = "2026-05-21"
FEE           = 0.003

WINDOW     = 5
ZSCORE_WIN = 120
ENTRY_Z    = -0.77
EXIT_Z     =  0.29
WARMUP     = WINDOW + ZSCORE_WIN

# The one series that explains every entry/exit — shown as a sub-pane under the
# workspace trade chart (see references/plot-series.md)
PLOT_SERIES = {"Broker Flow Z-Score": "zscore"}


# ── indicators ────────────────────────────────────────────────────────────────
def _add_indicators(df, window=WINDOW, zscore_win=ZSCORE_WIN):
    import pandas as pd
    df = df.copy()
    branch_df = df.attrs.get('branch_df', pd.DataFrame())

    aligned     = branch_df.reindex(df.index).fillna(0.0)
    rolling_net = aligned.rolling(window, min_periods=1).sum()
    net_buyers  = (rolling_net > 0).sum(axis=1)
    net_sellers = (rolling_net < 0).sum(axis=1)
    raw         = (net_buyers - net_sellers).astype(float)

    roll_mean   = raw.rolling(zscore_win, min_periods=zscore_win // 2).mean()
    roll_std    = raw.rolling(zscore_win, min_periods=zscore_win // 2).std()
    df['zscore'] = (raw - roll_mean) / roll_std.replace(0.0, float('nan'))
    return df


# ── fetch_data ────────────────────────────────────────────────────────────────
def fetch_data(hdrs):
    from lib.data import fetch_twstock_price_adj_batch, fetch_twstock_branch_daily_net

    print(f"Fetching price data for {SYMBOL}...")
    price_all = fetch_twstock_price_adj_batch([SYMBOL], START, END, hdrs)
    price_df  = price_all.get(SYMBOL)
    if price_df is None or price_df.empty:
        raise RuntimeError(f"No price data for {SYMBOL}")

    print("Fetching branch data...")
    branch_df = fetch_twstock_branch_daily_net(SYMBOL, START, END, hdrs)
    price_df.attrs['branch_df'] = branch_df
    return price_df


# ── compute_signals ───────────────────────────────────────────────────────────
def compute_signals(df, entry_z=ENTRY_Z, exit_z=EXIT_Z):
    import numpy as np
    import pandas as pd

    df  = _add_indicators(df)
    z   = df['zscore'].values
    n   = len(z)
    pos = np.full(n, np.nan)

    in_long = False
    for i in range(n):
        if np.isnan(z[i]):
            continue
        if not in_long and z[i] < entry_z:
            in_long = True
        elif in_long and z[i] > exit_z:
            in_long = False
        pos[i] = 1.0 if in_long else 0.0

    return pd.Series(pos, index=df.index)


if __name__ == '__main__':
    from lib.runner import run
    run(locals(), fetch_data, compute_signals, send_telegram_fn=None)
