# Strategy: [strategy name]
# Type:     C (multi-asset, weight-based, periodic rebalancing)
# Universe: [description of asset pool]
# Signal:   [what drives the weights — z-score, rank, momentum, etc.]
# Rebalance: [weekly / monthly / daily]

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
MODE          = "backtest"
STRATEGY_NAME = "[strategy_name]"
START         = "2015-01-01"
END           = None
FEE           = 0.003           # ~0.3% for Taiwan equities; adjust per asset class

PARAM1  = ...                   # primary signal parameter (e.g. lookback window)
PARAM2  = ...                   # secondary parameter (e.g. top-N, threshold)
WARMUP  = PARAM1                # bars to trim from backtest start (= longest rolling window)

UNIVERSE = [
    # list all stock/asset IDs here — must be fixed at backtest start, NO lookahead bias
    # '2330', '2317', ...
]


# ── helpers ───────────────────────────────────────────────────────────────────
# Each helper takes explicit params (not module-level constants) so scan.py can
# call compute_signals with different values without monkeypatching globals.

def _compute_signal(close_df, param1=PARAM1):
    """Compute per-asset signal DataFrame from price data.
    Returns a DataFrame with the same shape as close_df.
    Examples: pct_change(param1), rolling z-score, MA ratio, etc.
    """
    # return close_df.pct_change(param1, fill_method=None)
    raise NotImplementedError


def _compute_weights(signal_df, param2, is_rebalance):
    """Convert signal DataFrame → weight DataFrame.
    Apply rebalance mask (non-rebalance days → nan → ffill) inside this function.

    Two common patterns:
      A) Top-N equal weight:
            rank = signal_df.rank(axis=1, ascending=False, na_option='bottom')
            w = DataFrame(np.where(rank <= param2, 1/param2, 0), ...)
            w[signal_df.isna().all(axis=1)] = 0.0

      B) Proportional (z-score / score → normalize):
            pos = signal_df.clip(lower=0).fillna(0)
            total = pos.sum(axis=1)
            w = pos.div(total.where(total > 0), axis=0).fillna(0.0)

    Always finish with:
            w[~is_rebalance] = np.nan
            return w.ffill().fillna(0.0)
    """
    raise NotImplementedError


def _rebalance_mask(idx, freq='W'):
    """Return bool numpy array — True on rebalance bars (last bar of each period).
    freq: 'W' = weekly, 'M' = monthly, 'D' = daily (all bars).
    """
    import pandas as pd
    if freq == 'D':
        import numpy as np
        return np.ones(len(idx), dtype=bool)
    s = pd.Series(idx.to_period(freq), index=idx)
    return (s != s.shift(-1)).fillna(True).to_numpy()


# ── fetch_data ────────────────────────────────────────────────────────────────
# Returns a TUPLE — runner passes this directly to compute_signals as `data`.
def fetch_data(hdrs):
    import pandas as pd
    # from lib.data import fetch_twstock_price_adj, fetch_twstock_institutional

    closes, opens = {}, {}
    for sid in UNIVERSE:
        try:
            # price = fetch_twstock_price_adj(sid, START, END, hdrs)
            # closes[sid] = price['Close']
            # opens[sid]  = price['Open']
            pass
        except Exception as e:
            print(f"  skip {sid}: {e}")

    close_df = pd.DataFrame(closes).sort_index().dropna(how='all')
    open_df  = pd.DataFrame(opens).reindex(close_df.index)
    return close_df, open_df   # add more elements if needed (e.g. signal_df)


# ── compute_signals ───────────────────────────────────────────────────────────
# Accept params as kwargs with module-level defaults — this lets scan.py sweep
# values without touching globals.
#
#   weights_mat : numpy array  shape (n_days, n_stocks) — DO NOT pre-shift
#   price_df    : pd.concat({'close': close_df, 'open': open_df}, axis=1)
def compute_signals(data, param1=PARAM1, param2=PARAM2):
    import numpy as np
    import pandas as pd

    close_df, open_df = data   # unpack in the same order as fetch_data's return

    signal       = _compute_signal(close_df, param1)
    is_rebalance = _rebalance_mask(close_df.index, freq='W')
    weights      = _compute_weights(signal, param2, is_rebalance)

    price_df = pd.concat({'close': close_df, 'open': open_df}, axis=1)
    return weights.values, price_df   # weights MUST be numpy array


if __name__ == '__main__':
    from lib.runner import run
    try:
        from lib.notify import make_sender
        sender = make_sender()
    except Exception:
        sender = None
    run(locals(), fetch_data, compute_signals, sender)
