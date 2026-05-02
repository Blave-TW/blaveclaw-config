# Strategy Code Structure (Type A — Signal Strategy)

NOTE: This guide is for **Type A (Signal Strategy)** only — single symbol, fixed interval, backtest-first.
For Type B (everything else), write from scratch — no template, no backtest, no upload_report().

CRITICAL: Every Type A strategy MUST be based on `strategies/TEMPLATE.py`. Copy the template and fill in the marked sections. Do NOT write a standalone backtest script from scratch — scripts that don't follow the template cannot be deployed live or update the website automatically.

## Steps

1. Copy `strategies/TEMPLATE.py` to `strategies/[strategy_name].py`
2. Fill in the three marked sections:
   - `compute_signal()` — pure signal logic, returns `"LONG"` or `"FLAT"`
   - `BlaveStrategy.init()` — precompute indicators with `self.I()`
   - `BlaveStrategy.next()` — call `compute_signal()` and execute
3. Set `STRATEGY_NAME`, `SYMBOL`, `INTERVAL`, `START`, `END` at the top
4. Implement `place_order()` using the correct exchange skill reference

## Key Rules

- `compute_signal` must be a **pure function** (no API calls, no I/O) — it is called identically in backtest (`BlaveStrategy.next`) and live (`main` loop)
- `compute_signal` returns `"LONG"`, `"SHORT"`, or `"FLAT"` — represents **desired position state**, not an event. The framework handles open/close/flip automatically
- Always precompute indicators in `BlaveStrategy.init()` using `self.I()` — never compute them inside `next()` to avoid look-ahead bias
- `END` is backtest only — live/paper always fetches to today; keep `START` as the full history start date
- Default is always `MODE = "backtest"` — only switch to `"live"` after user confirms
- Do NOT force-close open positions at end of backtest data
- `upload_report()` runs every time — do NOT add conditionals around it

## Backtest Output (mandatory)

After `bt.run()`, always print:
```python
print(stats[['Return [%]', 'Sharpe Ratio', 'Max. Drawdown [%]', 'Win Rate [%]', '# Trades']])
```

## Indicators with Extra Data (e.g. Blave alpha signals)

If the strategy uses Blave alpha signals (holder concentration, taker intensity, etc.) alongside klines:

1. Fetch the indicator data separately and merge into the DataFrame before passing to `Backtest()`
2. Pass merged columns into `BlaveStrategy` via `self.I(lambda x: x, self.data.<ColName>)`
3. In `compute_signal()`, accept the indicator value as a parameter

Example: see `strategies/btc_ti_strategy.py`

## Logging

Log to `/root/.openclaw/workspace/logs/[strategy_name].log`

## Arrays

klines, indicators, returns: NEVER truncate or cap any array (no `[:N]` slicing)
