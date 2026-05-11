# Strategy Code Structure (Type A — Signal Strategy)

NOTE: This guide is for **Type A (Signal Strategy)** only — single symbol, fixed interval, backtest-first.
For Type B (everything else), write from scratch — no template, no backtest.

CRITICAL: Every Type A strategy MUST be based on `strategies/TEMPLATE.py`. Copy the template and fill in the marked sections. Do NOT write a standalone backtest script from scratch.

## Steps

1. Copy `strategies/TEMPLATE.py` to `strategies/[strategy_name]/strategy.py`
2. Set config at the top: `STRATEGY_NAME`, `SYMBOL`, `EXCHANGE`, `INTERVAL`, `START`
3. Fill in the two marked sections:
   - `add_indicators(df)` — add indicator columns to df, return df
   - `compute_signal(row)` — pure signal logic, returns float
4. Run: `python3 strategies/[strategy_name]/strategy.py`

## Signal Contract

`compute_signal(row)` returns a **float**:
- `1.0` = full long
- `-1.0` = full short
- `0.0` = flat (close position)
- `float("nan")` = **hold** — keep current position unchanged (no trade)
- Fractions (e.g. `0.5`, `-0.3`) are valid — used with `VOL_TARGETING`

`nan` vs `0.0`: use `nan` when you want to stay in the current position (e.g. inside a trend, before a threshold is crossed). Use `0.0` only when you explicitly want to exit. `lib/runner.py` treats `nan` as "no action" and `0.0` as "flat".

## Three-Layer Architecture

Every Type A strategy is built from three conceptual layers:

```
add_indicators(df)   → df with indicator columns       (data layer)
compute_signal(row)  → 1.0 / 0.0 / nan                (signal layer, pure/stateless)
                       nan = hold current position
position tracking    → nan forwarded via ffill          (position layer)
```

`lib/runner.py` and `lib/execute.py` handle the position layer automatically.
You only write `add_indicators` and `compute_signal`.

**CRITICAL — do NOT put signal logic in `add_indicators()`:**
- `add_indicators(df)` is for raw indicator calculation only (fetch alpha data, compute rolling stats, add columns)
- All threshold logic, dead zone logic, and signal decisions belong in `compute_signal(row)`
- `compute_signal` does NOT receive `in_long` — nan handles "hold" regardless of current position

**Dead zone example (threshold strategy with hold zone):**
```python
def add_indicators(df):
    df["TI"] = ...  # fetch taker intensity, add to df — indicator only
    return df

def compute_signal(row) -> float:
    ti = float(row["TI"])
    if np.isnan(ti):    return float("nan")  # warmup / missing: hold
    if ti > ENTRY_TH:   return 1.0            # above entry: long
    if ti < EXIT_TH:    return 0.0            # below exit: flat
    return float("nan")                        # dead zone between thresholds: hold
```

**Crossover example (golden / death cross):**
```python
def add_indicators(df):
    df["K"]      = ...          # %K indicator
    df["D"]      = ...          # %D indicator
    df["K_prev"] = df["K"].shift(1)   # previous bar — still indicator data, not signal
    df["D_prev"] = df["D"].shift(1)
    return df

def compute_signal(row) -> float:
    k, d, kp, dp = row["K"], row["D"], row["K_prev"], row["D_prev"]
    if any(pd.isna(v) for v in (k, d, kp, dp)):
        return float("nan")            # warmup: hold
    if kp <= dp and k > d:             # golden cross: long
        return 1.0
    if kp >= dp and k < d:             # death cross: flat
        return 0.0
    return float("nan")                # between crossovers: hold
```

`compute_signal` must be a **pure function** (no API calls, no I/O, no side effects). It is called identically in backtest and live mode by `lib/runner.py`.

## What You Do NOT Need to Write

- `BlaveStrategy` class — handled by `lib/runner.py`
- `main()` function — handled by `lib/runner.py`
- `place_order()` — handled by `strategies/reconciler/reconciler.py`
- Logging setup — handled by `lib/runner.py`

## Backtest Output (mandatory)

`lib/runner.py` automatically prints stats and generates PnL chart after `bt.run()`. No extra code needed.

## Alpha Indicators (Blave alpha signals)

If the strategy uses Blave alpha signals (holder concentration, taker intensity, liquidation, whale hunter, etc.):

1. Read `skills/blave-quant/examples/backtest-holder-concentration.md` BEFORE writing any code — contains the correct fetch pattern (parallel arrays, annual chunking)
2. Implement the fetch inside `add_indicators(df)`: fetch alpha data, align to df index, add as new columns, return df
3. Use the new columns in `compute_signal(row)`

Do NOT put alpha fetch logic in `lib/runner.py`.

## Vol Targeting (optional)

Set `VOL_TARGETING = True` in config to size position by realized volatility:
- `realized_vol` column is automatically added to df by `lib/runner.py`
- `compute_signal` uses it to scale position: `direction × min(TARGET_VOL / realized_vol, VOL_CAP)`
- Adjust `TARGET_VOL`, `VOL_LOOKBACK`, `PERIODS_PER_YEAR`, `VOL_CAP` in config

## Key Rules

- `END` is backtest only — live/paper always fetches to today; keep `START` as the full history start date
- Default is always `MODE = "backtest"` — only switch to `"live"` after user confirms
- NEVER truncate or cap arrays (no `[:N]` slicing)
