# Strategy Code Structure (Type A — Signal Strategy)

NOTE: This guide is for **Type A (Signal Strategy)** only — single symbol, fixed interval, backtest-first.
For Type B (everything else), write from scratch — no template, no backtest.

CRITICAL: Every Type A strategy MUST be based on `strategies/TEMPLATE.py`. Copy the template and fill in the marked sections. Do NOT write a standalone backtest script from scratch.

## Steps

1. Copy `strategies/TEMPLATE.py` to `strategies/[strategy_name]/strategy.py`
2. Set config at the top: `STRATEGY_NAME`, `SYMBOL`, `EXCHANGE`, `INTERVAL`, `START`
3. Fill in the two marked sections:
   - `add_indicators(df)` — add indicator columns to df, return df
   - `compute_signals(df)` — vectorized signal logic, returns pd.Series
4. Run: `python3 strategies/[strategy_name]/strategy.py`

## Signal Contract

`compute_signals(df)` receives the full DataFrame and returns a **pd.Series** (same index as df):
- Positive float (e.g. `1.0`, `0.6`) = long (value = position size fraction with vol-targeting)
- `-1.0` = short
- `0.0` = flat (close position)
- `float("nan")` = **hold** — keep current position unchanged (no trade)

`nan` vs `0.0`: use `nan` when you want to stay in the current position (e.g. inside a trend, before a threshold is crossed). Use `0.0` only when you explicitly want to exit. `lib/runner.py` treats `nan` as "no action" via `ffill()`.

## Three-Layer Architecture

```
add_indicators(df)    → df with indicator columns        (data layer)
compute_signals(df)   → pd.Series: 1.0 / 0.0 / nan      (signal layer, vectorized)
                        nan = hold current position
position tracking     → nan forwarded via ffill           (position layer)
```

`lib/runner.py` and `lib/execute.py` handle the position layer automatically.
You only write `add_indicators` and `compute_signals`.

**CRITICAL — do NOT put signal logic in `add_indicators()`:**
- `add_indicators(df)` is for raw indicator calculation only (fetch alpha data, compute rolling stats, add columns)
- All threshold logic, dead zone logic, and signal decisions belong in `compute_signals(df)`

**Dead zone example (threshold strategy with hold zone):**
```python
def add_indicators(df):
    from lib.data import fetch_taker_intensity
    ti = fetch_taker_intensity(SYMBOL, INTERVAL, START, END, HDRS)
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
def add_indicators(df):
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
    # between crossovers → nan → hold
    return signal
```

**Vol-targeting example:**
```python
def compute_signals(df) -> pd.Series:
    k, d   = df["K"], df["D"]
    golden = (k > d) & (k.shift(1) <= d.shift(1))
    death  = (k < d) & (k.shift(1) >= d.shift(1))
    signal = pd.Series(np.nan, index=df.index)
    if VOL_TARGETING and "realized_vol" in df.columns:
        vol  = df["realized_vol"]
        size = (TARGET_VOL / vol).clip(upper=VOL_CAP)
        signal[golden] = size[golden]   # vol-scaled size at each entry
    else:
        signal[golden] = 1.0
    signal[death] = 0.0
    return signal
```

`compute_signals` must be a **pure function** (no API calls, no I/O, no side effects). It is called identically in backtest (`compute_signals(df)`) and live mode (`compute_signals(df).iloc[-1]`) by `lib/runner.py`.

## What You Do NOT Need to Write

- Backtest loop — handled by `lib/runner.py` (vectorbt)
- `main()` function — handled by `lib/runner.py`
- `place_order()` — handled by `strategies/reconciler/reconciler.py`
- Logging setup — handled by `lib/runner.py`

## Backtest Output (mandatory)

`lib/runner.py` automatically prints stats and generates PnL chart after the backtest. No extra code needed.

## Alpha Indicators (Blave alpha signals)

If the strategy uses Blave alpha signals (holder concentration, taker intensity, liquidation, whale hunter, etc.):

1. Read `skills/blave-quant/examples/backtest-holder-concentration.md` BEFORE writing any code
2. Use `lib/data` fetch functions inside `add_indicators(df)` — they handle annual chunking automatically:
   ```python
   from lib.data import fetch_holder_concentration
   hc = fetch_holder_concentration(SYMBOL, INTERVAL, START, END, HDRS)
   df = df.join(hc.rename(columns={"alpha": "HC"}))
   df["HC"] = df["HC"].ffill()
   ```
3. Use the new columns in `compute_signals(df)`

Do NOT put alpha fetch logic in `lib/runner.py`.

## Vol Targeting (optional)

Set `VOL_TARGETING = True` in config to size position by realized volatility:
- `realized_vol` column is automatically added to df by `lib/runner.py`
- `compute_signals` uses it to scale position: `(TARGET_VOL / vol).clip(upper=VOL_CAP)`
- Adjust `TARGET_VOL`, `VOL_LOOKBACK`, `PERIODS_PER_YEAR`, `VOL_CAP` in config

## Key Rules

- `END` is backtest only — live/paper always fetches to today; keep `START` as the full history start date
- Default is always `MODE = "backtest"` — only switch to `"live"` after user confirms
- NEVER truncate or cap arrays (no `[:N]` slicing)
- Execution timing: signal fires at bar i close → executed at bar i+1 open (handled by runner)
