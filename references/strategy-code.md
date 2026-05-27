# Strategy Code Structure (Type A & C)

NOTE: This guide covers **Type A (Signal Strategy)** and **Type C (Portfolio Strategy)**. For Type B (everything else), write from scratch — no template, no backtest.

CRITICAL: Every Type A strategy MUST be based on `strategies/TEMPLATE_A.py`. Copy the template and fill in the marked sections. Do NOT write a standalone backtest script from scratch.

## Steps

1. Copy `strategies/TEMPLATE_A.py` to `strategies/[strategy_name]/strategy.py`
2. Set config at the top: `STRATEGY_NAME`, `SYMBOL`, `EXCHANGE`, `INTERVAL`, `START`
3. Fill in the three marked sections:
   - `_add_indicators(df, p1, p2)` — add indicator columns, parameterized for scan
   - `fetch_data(hdrs)` — fetch kline, call `_add_indicators` with module params
   - `compute_signals(df)` — vectorized signal logic, returns pd.Series
4. Run: `python3 strategies/[strategy_name]/strategy.py`

## Signal Contract

`compute_signals(df)` receives the DataFrame returned by `fetch_data` and returns either a **pd.Series** or a **(pd.Series, exec_at_close)** tuple:

| Value | Meaning | Execution |
|-------|---------|-----------|
| positive float (`1.0`, `0.6`, …) | long, value = position size fraction | next bar **Open** |
| `0.0` | flat — exit | next bar **Open** |
| `nan` | hold — keep current position unchanged | no trade |

**Execution model — two types, must be explicit:**
- **next-bar open** (default): return plain `pd.Series` — signal at close[t] executes at open[t+1]
- **this-bar close**: return `(signals, exec_at_close)` where `exec_at_close` is a bool `pd.Series` (True on bars that execute at close). For futures settlement use `settlement_signals_from_db()` which handles this automatically.

`nan` vs `0.0`: use `nan` to hold the current position (e.g. inside a trend). Use `0.0` to exit at next open.

## Three-Layer Architecture

```
_add_indicators(df, p1, p2)   → df with indicators       (indicator layer, parameterized)
fetch_data(hdrs)               → fetch_kline + _add_indicators(module params)
compute_signals(df)            → pd.Series: 1.0 / 0.0 / nan
```

`lib/runner.py` handles everything else: backtest, chart, stats, notify, live execution.
You write `_add_indicators`, `fetch_data`, and `compute_signals`.

Separating `_add_indicators` allows `scan.py` to fetch data once and sweep parameters cheaply without re-hitting the API.

**CRITICAL — signal logic belongs only in `compute_signals()`:**
- `_add_indicators` computes indicator columns only
- `fetch_data` fetches kline and calls `_add_indicators` with module-level params
- All threshold logic, dead zone logic, signal decisions → `compute_signals`

**Dead zone example (threshold strategy with hold zone):**
```python
ENTRY_TH = 0.5
EXIT_TH  = -0.5

def _add_indicators(df, hdrs, entry_th=ENTRY_TH, exit_th=EXIT_TH):
    from lib.data import fetch_taker_intensity
    df = df.copy()
    ti = fetch_taker_intensity(SYMBOL, INTERVAL, START, END, hdrs)
    df = df.join(ti.rename(columns={"alpha": "TI"}))
    df["TI"] = df["TI"].ffill()
    return df

def fetch_data(hdrs):
    from lib.data import fetch_kline
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    return _add_indicators(df, hdrs)

def compute_signals(df):
    import pandas as pd, numpy as np
    ti     = df["TI"]
    signal = pd.Series(np.nan, index=df.index)
    signal[ti > ENTRY_TH] = 1.0
    signal[ti < EXIT_TH]  = 0.0
    return signal
```

**Crossover example (golden / death cross):**
```python
SMA_FAST = 20
SMA_SLOW = 50

def _add_indicators(df, fast=SMA_FAST, slow=SMA_SLOW):
    df = df.copy()
    df['SMA_F'] = df['Close'].rolling(fast).mean()
    df['SMA_S'] = df['Close'].rolling(slow).mean()
    return df

def fetch_data(hdrs):
    from lib.data import fetch_kline
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    return _add_indicators(df)

def compute_signals(df):
    import pandas as pd, numpy as np
    k, d   = df['SMA_F'], df['SMA_S']
    golden = (k > d) & (k.shift(1) <= d.shift(1))
    death  = (k < d) & (k.shift(1) >= d.shift(1))
    signal = pd.Series(np.nan, index=df.index)
    signal[golden] = 1.0
    signal[death]  = 0.0
    return signal
```

`compute_signals` must be a **pure function** (no API calls, no I/O, no side effects).

## What You Do NOT Need to Write

- Backtest loop — handled by `lib/runner.py`
- `main()` function — handled by `lib/runner.py`
- `place_order()` — handled by `strategies/reconciler/reconciler.py`
- Logging setup — handled by `lib/runner.py`
- Chart / stats / notify — handled by `lib/runner.py`

## Alpha Indicators (Blave alpha signals)

Use `lib/data` fetch functions inside `fetch_data(hdrs)`:

```python
def fetch_data(hdrs):
    from lib.data import fetch_kline, fetch_holder_concentration
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    hc = fetch_holder_concentration(SYMBOL, INTERVAL, START, END, hdrs)
    df = df.join(hc.rename(columns={"alpha": "HC"}))
    df["HC"] = df["HC"].ffill()
    return df
```


## Vol Targeting (optional)

Add `VOL_TARGETING = True` and `TARGET_VOL`, `VOL_LOOKBACK`, `PERIODS_PER_YEAR`, `VOL_CAP` to config. Then use `lib/strategy` helpers:

```python
# fetch_data — add one line after building df:
def fetch_data(hdrs):
    from lib.data import fetch_kline
    from lib.strategy import add_realized_vol
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    if VOL_TARGETING:
        add_realized_vol(df, VOL_LOOKBACK, PERIODS_PER_YEAR)
    return df

# compute_signals — scale signal before returning:
def compute_signals(df):
    import pandas as pd, numpy as np
    from lib.strategy import apply_vol_scaling
    signal = pd.Series(np.nan, index=df.index)
    # ... signal logic ...
    return apply_vol_scaling(signal, df, TARGET_VOL, VOL_CAP) if VOL_TARGETING else signal
```

Default values: `TARGET_VOL=0.10`, `VOL_CAP=2.0`. For 1h bars: `VOL_LOOKBACK=720`, `PERIODS_PER_YEAR=8760`.

## Futures Strategies (CME / NYMEX / ICE)

For futures contracts, use `fetch_db_kline(dataset, symbol, schema, start, end, headers)`.

**CRITICAL — always include settlement exit.** Futures contracts expire monthly. Holding through expiry risks forced settlement at an unfavorable price. Always force-exit on the last trading day using `settlement_signals_from_db()`:

```python
def compute_signals(df):
    import pandas as pd, numpy as np
    signal = pd.Series(np.nan, index=df.index)
    # ... signal logic ...

    # settlement exit: forces signal=0.0 on last bar before each contract rollover,
    # marks those bars exec_at_close=True (executes at this-bar close, not next open)
    from lib.data import settlement_signals_from_db
    return settlement_signals_from_db(df, signal)
```

`settlement_signals_from_db(df, signal)` returns `(signal, exec_at_close)` — return it directly from `compute_signals`. It detects rollover dates automatically from the `instrument_id` column in the df returned by `fetch_db_kline`. See `examples/cl_sma/strategy.py` for a complete working example.

## Key Rules

- `END = None` always fetches to today — `fetch_kline` handles this automatically
- Default is always `MODE = "backtest"` — only switch to `"live"` after user confirms
- NEVER truncate or cap arrays (no `[:N]` slicing)
- Execution timing: signal fires at Close[t] → executes at Open[t+1] by default (next-bar open); use `exec_at_close` for this-bar close execution (futures settlement only)

---

## Type C — Portfolio Strategy

CRITICAL: Every Type C strategy MUST be based on `strategies/TEMPLATE_C.py`. Copy it and fill in the marked sections. Read `examples/tw100_foreign_zscore/strategy.py` as a complete working reference.

### Steps

1. Copy `strategies/TEMPLATE_C.py` to `strategies/[strategy_name]/strategy.py`
2. Set config: `STRATEGY_NAME`, `UNIVERSE`, `SIGNAL_WINDOW`, `WARMUP`, `FEE`, `START`
3. Fill in three sections:
   - `fetch_data(hdrs)` — fetches close/open/signal data for all UNIVERSE symbols; returns a **tuple**
   - helpers (e.g. `_compute_weights`, `_rebalance_mask`) — signal → weight conversion
   - `compute_signals(data)` — unpacks tuple, builds weight matrix, returns `(weights.values, price_df)`
4. Run: `python3 strategies/[strategy_name]/strategy.py`

### Interface Contract

```
fetch_data(hdrs)         → tuple of DataFrames   (close_df, open_df, ...)   # open_df always second; aux data (e.g. foreign_df) appended after
compute_signals(data)    → (weights_mat, price_df)
```

| Return element | Type | Shape | Notes |
|---|---|---|---|
| `weights_mat` | `np.ndarray` | `(n_days, n_stocks)` | target weight at each close; rows sum to ≤ 1 |
| `price_df` | `pd.DataFrame` (MultiIndex) | `(n_days, 2×n_stocks)` | `pd.concat({'close': close_df, 'open': open_df}, axis=1)` |
| `exec_at_close` *(optional)* | `np.ndarray[bool]` | `(n_days,)` | bars that execute at this-bar close (rare) |

**DO NOT pre-shift weights.** Runner shifts by 1 bar automatically: `w_curr[t] = weights[t-1]`.

### Weight Matrix Pattern

Use helpers for signal computation, weight building, and rebalance mask — this keeps `compute_signals` clean and lets `scan.py` sweep params without touching globals.

```python
def _compute_signal(close_df, param1=PARAM1):
    """Per-asset signal DataFrame. Examples: pct_change, rolling z-score, MA ratio."""
    # return close_df.pct_change(param1, fill_method=None)
    raise NotImplementedError

def _compute_weights(signal_df, param2, is_rebalance):
    """Signal → weight DataFrame. Two common patterns:
      A) Top-N equal weight:
            rank = signal_df.rank(axis=1, ascending=False, na_option='bottom')
            w = DataFrame(np.where(rank <= param2, 1/param2, 0), ...)
            w[signal_df.isna().all(axis=1)] = 0.0
      B) Proportional (z-score → normalize):
            pos = signal_df.clip(lower=0).fillna(0)
            w = pos.div(pos.sum(axis=1).where(pos.sum(axis=1) > 0), axis=0).fillna(0.0)
    Always finish with rebalance mask + ffill:
    """
    # ... build w ...
    w[~is_rebalance] = float('nan')
    return w.ffill().fillna(0.0)

def _rebalance_mask(idx, freq='W'):
    """Bool numpy array — True on last bar of each period. freq: 'W', 'M', 'D'."""
    import pandas as pd, numpy as np
    if freq == 'D':
        return np.ones(len(idx), dtype=bool)
    s = pd.Series(idx.to_period(freq), index=idx)
    return (s != s.shift(-1)).fillna(True).to_numpy()

def compute_signals(data, param1=PARAM1, param2=PARAM2):
    import pandas as pd
    close_df, open_df = data          # unpack in same order as fetch_data return

    signal       = _compute_signal(close_df, param1)
    is_rebalance = _rebalance_mask(close_df.index, freq='W')
    weights      = _compute_weights(signal, param2, is_rebalance)

    price_df = pd.concat({'close': close_df, 'open': open_df}, axis=1)
    return weights.values, price_df   # numpy array required — runner detects Type C by this
```

See `examples/tw100_foreign_zscore/strategy.py` (z-score proportional) and `examples/twstock_momentum/strategy.py` (top-N equal weight) for complete working implementations.

### Lookahead Bias Warning

`UNIVERSE` must be **fixed at backtest start** — determined only from information available before `START`.

- **WRONG:** `universe = all_stocks.nlargest(100, 'cumulative_net_buy')` — uses full-period data
- **RIGHT:** hardcode the universe from a historical constituent list, or use all IDs that ever appear in the data source

Let `compute_signals` do per-rebalance ranking using only the lookback window available at each date.

### What You Do NOT Need to Write

- Backtest loop — handled by `lib/runner.py`
- Weight shifting — runner shifts by 1 bar
- PnL computation — runner calls `precise_pnl` with the weight matrix
- Chart / stats / notify — runner handles all output
