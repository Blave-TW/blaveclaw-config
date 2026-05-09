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
- `0.0` = flat
- Fractions (e.g. `0.5`, `-0.3`) are valid — used with `VOL_TARGETING`

`compute_signal` must be a **pure function** (no API calls, no I/O). It is called identically in backtest and live mode by `lib/runner.py`.

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
