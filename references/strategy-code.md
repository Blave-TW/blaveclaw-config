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

## Futures Strategies (CME / NYMEX / ICE)

For futures contracts, use `fetch_db_kline(dataset, symbol, schema, start, end, headers)`.

**CRITICAL — always include settlement exit.** Futures contracts expire monthly. Holding through expiry risks forced settlement at an unfavorable price. Always force-exit on the last trading day:

```python
# settlement exit: -1.0 on last bar of expiry day → executes at that bar's close
def _settlement_dates(start, end):
    """NYMEX WTI CL: 3rd business day before 25th of preceding month."""
    from datetime import date, timedelta
    import pandas as pd
    s_year = pd.Timestamp(start).year
    e_year = (pd.Timestamp(end) if end else pd.Timestamp.now()).year
    dates = set()
    for year in range(s_year - 1, e_year + 2):
        for month in range(1, 13):
            anchor = date(year, month, 25)
            d = anchor
            while d.weekday() >= 5:
                d -= timedelta(days=1)
            count = 0
            while count < 3:
                d -= timedelta(days=1)
                if d.weekday() < 5:
                    count += 1
            dates.add(d)
    return dates

def compute_signals(df):
    import pandas as pd, numpy as np
    signal = pd.Series(np.nan, index=df.index)
    # ... signal logic ...

    # settlement exit
    settle_dates = _settlement_dates(START, END)
    idx_dates    = pd.to_datetime(df.index.date)
    for d in settle_dates:
        mask = idx_dates == pd.Timestamp(d)
        if mask.any():
            signal.loc[df.index[mask][-1]] = -1.0
    return signal
```

Each exchange has its own settlement rule — adjust `_settlement_dates` accordingly. See `examples/cl_sma_1h/strategy.py` for a complete working example.

## Key Rules

- `END = None` always fetches to today — `fetch_kline` handles this automatically
- Default is always `MODE = "backtest"` — only switch to `"live"` after user confirms
- NEVER truncate or cap arrays (no `[:N]` slicing)
- Execution timing: signal fires at Close[t] → executes at Close[t] (MOC)
