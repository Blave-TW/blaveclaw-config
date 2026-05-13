# Strategy Code Structure (Type A — Signal Strategy)

NOTE: This guide is for **Type A (Signal Strategy)** only — single symbol, fixed interval, backtest-first.
For Type B (everything else), write from scratch — no template, no backtest.

CRITICAL: Every Type A strategy MUST be based on `strategies/TEMPLATE.py`. Copy the template and fill in the marked sections. Do NOT write a standalone backtest script from scratch.

## Steps

1. Copy `strategies/TEMPLATE.py` to `strategies/[strategy_name]/strategy.py`
2. Set config at the top: `STRATEGY_NAME`, `SYMBOL`, `EXCHANGE`, `INTERVAL`, `START`
3. Fill in the two marked sections:
   - `fetch_data(hdrs)` — fetch kline, add indicators, add realized_vol if needed; return df
   - `compute_signals(df)` — vectorized signal logic, returns pd.Series
4. Run: `python3 strategies/[strategy_name]/strategy.py`

## Signal Contract

`compute_signals(df)` receives the DataFrame returned by `fetch_data` and returns a **pd.Series** (same index as df):
- Positive float (e.g. `1.0`, `0.6`) = long (value = position size fraction with vol-targeting)
- `-1.0` = short
- `0.0` = flat (close position)
- `float("nan")` = **hold** — keep current position unchanged (no trade)

`nan` vs `0.0`: use `nan` when you want to stay in the current position (e.g. inside a trend, before a threshold is crossed). Use `0.0` only when you explicitly want to exit. `lib/runner.py` treats `nan` as "no action" via `ffill()`.

## Two-Layer Architecture

```
fetch_data(hdrs)      → df with OHLCV + indicators       (data layer)
compute_signals(df)   → pd.Series: 1.0 / 0.0 / nan      (signal layer, vectorized)
                        nan = hold current position
```

`lib/runner.py` handles everything else: backtest, chart, stats, notify, live execution.
You only write `fetch_data` and `compute_signals`.

**CRITICAL — do NOT put signal logic in `fetch_data()`:**
- `fetch_data(hdrs)` is for data fetching and indicator calculation only
- All threshold logic, dead zone logic, and signal decisions belong in `compute_signals(df)`

**Dead zone example (threshold strategy with hold zone):**
```python
def fetch_data(hdrs):
    from lib.data import fetch_kline, fetch_taker_intensity
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    ti = fetch_taker_intensity(SYMBOL, INTERVAL, START, END, hdrs)
    df = df.join(ti.rename(columns={"alpha": "TI"}))
    df["TI"] = df["TI"].ffill()
    return df

def compute_signals(df) -> pd.Series:
    ti     = df["TI"]
    signal = pd.Series(np.nan, index=df.index)
    signal[ti > ENTRY_TH] = 1.0   # above entry: long
    signal[ti < EXIT_TH]  = 0.0   # below exit: flat
    # dead zone between thresholds → nan → hold (ffill in runner)
    return signal
```

**Crossover example (golden / death cross):**
```python
def fetch_data(hdrs):
    from lib.data import fetch_kline
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    df["K"] = ...   # %K indicator
    df["D"] = ...   # %D indicator
    return df

def compute_signals(df) -> pd.Series:
    k, d   = df["K"], df["D"]
    golden = (k > d) & (k.shift(1) <= d.shift(1))
    death  = (k < d) & (k.shift(1) >= d.shift(1))
    signal = pd.Series(np.nan, index=df.index)
    signal[golden] = 1.0   # golden cross: long
    signal[death]  = 0.0   # death cross: flat
    return signal
```

**Vol-targeting example:**
```python
def fetch_data(hdrs):
    from lib.data import fetch_kline
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    df["K"] = ...
    df["D"] = ...
    if VOL_TARGETING:
        log_ret = np.log(df['Close'] / df['Close'].shift(1))
        df['realized_vol'] = log_ret.rolling(VOL_LOOKBACK).std() * np.sqrt(PERIODS_PER_YEAR)
    return df

def compute_signals(df) -> pd.Series:
    k, d   = df["K"], df["D"]
    golden = (k > d) & (k.shift(1) <= d.shift(1))
    death  = (k < d) & (k.shift(1) >= d.shift(1))
    signal = pd.Series(np.nan, index=df.index)
    if VOL_TARGETING and "realized_vol" in df.columns:
        vol  = df["realized_vol"]
        size = (TARGET_VOL / vol).clip(upper=VOL_CAP)
        signal[golden] = size[golden]
    else:
        signal[golden] = 1.0
    signal[death] = 0.0
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

Read `skills/blave-quant/examples/backtest-holder-concentration.md` for the canonical pattern.

## Vol Targeting (optional)

Set `VOL_TARGETING = True` in config, then add `realized_vol` inside `fetch_data`:

```python
if VOL_TARGETING:
    log_ret = np.log(df['Close'] / df['Close'].shift(1))
    df['realized_vol'] = log_ret.rolling(VOL_LOOKBACK).std() * np.sqrt(PERIODS_PER_YEAR)
```

Then use it in `compute_signals`: `(TARGET_VOL / df['realized_vol']).clip(upper=VOL_CAP)`.

## Key Rules

- `END` is backtest only — live/paper always fetches to today; handle in `fetch_data` via `MODE`
- Default is always `MODE = "backtest"` — only switch to `"live"` after user confirms
- NEVER truncate or cap arrays (no `[:N]` slicing)
- Execution timing: signal fires at bar i close → executed at bar i+1 open (handled by runner)
