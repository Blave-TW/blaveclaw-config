# Manager & Reconciler

## Files

- `manager/management_backtest.py` — portfolio walk-forward backtest
- `manager/manager.py` — portfolio optimizer
- `manager/reconciler.py` — position reconciler (polling loop)
- `manager/portfolio_config.json` — gitignored; written by manager.py

**CRITICAL — never create files or subdirectories inside `manager/`.** All output (portfolio_config.json, pnl.png, stats.json) is written by the scripts themselves. Never create a `manager/manager/` or any nested folder — it breaks path resolution in all three scripts. Never delete any file in `manager/` when removing strategies.

## management_backtest.py

Simulates the manager's dynamic allocation day by day (strictly out-of-sample). Compares against random static portfolios as benchmark. Run BEFORE going live to validate combined portfolio performance.

```
python3 manager/management_backtest.py [--lookback 365] [--random-n 500]
```

When the user asks to backtest the portfolio / combined strategies, use THIS script — not individual strategy backtests.

## manager.py

Reads all `strategies/*/stats.json` (daily_returns). Maximizes `slope/std` of the combined portfolio equity curve (365-day lookback). Writes optimal weights + leverage to `manager/portfolio_config.json`.

```
python3 manager/manager.py [--lookback 365] [--account 10000] [--target-vol 0.30]
```

`--target-vol`: sets target annual volatility; computes `leverage = target_vol / ann_vol`

## reconciler.py

Polls every 5 seconds; only reconciles when a strategy's `state.json` mtime changes. `get_positions()` and `place_order()` are exchange-specific stubs to fill in once. `lib/portfolio.py` contains `reconcile()` logic; applies `leverage` from portfolio_config.

```
python3 manager/reconciler.py
```

**Before starting the reconciler (or triggering a manual reconcile):** always show the user the pending order summary from `aggregate_portfolio()` + `compute_diff()` and ask for explicit confirmation. Only proceed if the user confirms.
