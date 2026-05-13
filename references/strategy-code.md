# Strategy Code Structure (Type A — Signal Strategy)

NOTE: This guide is for **Type A (Signal Strategy)** only — single symbol, fixed interval, backtest-first.
For Type B (everything else), write from scratch — no template, no backtest.

CRITICAL: Every Type A strategy MUST be based on `strategies/TEMPLATE.py`. Copy the template and fill in the marked sections. Do NOT write a standalone backtest script from scratch.

## Steps

1. Copy `strategies/TEMPLATE.py` to `strategies/[strategy_name]/strategy.py`
2. Set config at the top: `STRATEGY_NAME`, `SYMBOL`, `EXCHANGE`, `INTERVAL`, `START`
3. Fill in the three marked sections:
   - `_add_indicators(df, p1, p2)` — add indicator columns, parameterized for scan
   - `fetch_data(hdrs)` — fetch kline, call `_add_indicators` with module params
   - `compute_signals(df)` — vectorized signal logic, returns pd.Series
4. Run: `python3 strategies/[strategy_name]/strategy.py`

## Signal Contract

`compute_signals(df)` receives the DataFrame returned by `fetch_data` and returns a **pd.Series** (same index as df):

| Value | Meaning | Execution |
|-------|---------|-----------|
| positive float (`1.0`, `0.6`, …) | long, value = position size fraction | next bar **Open** |
| `0.0` | flat — normal exit | next bar **Open** |
| `-1.0` | settlement exit — close at expiry | **this bar Close** |
| `nan` | hold — keep current position unchanged | no trade |

`nan` vs `0.0`: use `nan` when you want to stay in the current position (e.g. inside a trend). Use `0.0` only when you explicitly want to exit at next open. Use `-1.0` only for futures settlement or forced close-at-close scenarios.

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

- Backtest loop — handled by `lib/runner.py` (vectorbt)
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

## Key Rules

- `END = None` always fetches to today — `fetch_kline` handles this automatically
- Default is always `MODE = "backtest"` — only switch to `"live"` after user confirms
- NEVER truncate or cap arrays (no `[:N]` slicing)
- Execution timing: signal fires at bar i close → executed at bar i+1 open (handled by runner)
