# Strategy Code Structure (Type A — Signal Strategy)

NOTE: This guide is for **Type A (Signal Strategy)** only — single symbol, fixed interval, backtest-first.
For Type B (everything else), write from scratch — no template, no backtest.

CRITICAL: Every Type A strategy MUST be based on `strategies/TEMPLATE.py`. Copy the template and fill in `compute_signal`. Do NOT write a standalone backtest script from scratch — scripts that don't follow the template cannot be deployed live or update the website automatically.

## Steps
1. Copy `strategies/TEMPLATE.py` to `strategies/[strategy_name].py`
2. Fill in `compute_signal` — returns `"LONG"` or `"FLAT"` (desired position state, not an event)
3. Set `STRATEGY_NAME`, `SYMBOL`, `INTERVAL`, `START`, `END` at the top

## Key Rules
- `compute_signal` must be a pure function (no API calls, no I/O) — shared between backtest and live
- `END` is backtest only — live/paper always fetches up to today; keep `START` as the full history start date
- Log to `/root/.openclaw/workspace/logs/[strategy_name].log`
- Default is always `MODE = "backtest"` — only switch to `"live"` after user confirms
- Do NOT force-close open positions at the end of backtest data
- `upload_report()` runs every time — do NOT add conditionals around it
- klines, indicators, returns: NEVER truncate or cap any array (no [:N] slicing)
