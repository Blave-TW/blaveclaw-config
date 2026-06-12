# Manager & Reconciler

## Files

- `manager/management_backtest.py` — portfolio walk-forward backtest
- `manager/manager.py` — portfolio optimizer
- `manager/reconciler.py` — position reconciler (polling loop)
- `manager/portfolio_config.json` — gitignored; written by manager.py; also contains `"exchanges"` dict (see below)

**CRITICAL — never create files or subdirectories inside `manager/`.** All output (portfolio_config.json, pnl.png, stats.json) is written by the scripts themselves. Never create a `manager/manager/` or any nested folder — it breaks path resolution in all three scripts. Never delete any file in `manager/` when removing strategies.

## management_backtest.py

Simulates the manager's dynamic allocation day by day (strictly out-of-sample). Compares against random static portfolios as benchmark. Run BEFORE going live to validate combined portfolio performance.

```
python3 manager/management_backtest.py [--lookback 365] [--random-n 500]
```

When the user asks to backtest the portfolio / combined strategies, use THIS script — not individual strategy backtests.

## manager.py

Reads all `strategies/*/stats.json` (daily_returns). Maximizes `slope/std` of the combined portfolio equity curve (365-day lookback). Writes optimal weights + leverage to `manager/portfolio_config.json` — **only with `--apply`**.

```
python3 manager/manager.py [--lookback 365] [--account 10000] [--target-vol 0.30]            # dry-run
python3 manager/manager.py [--lookback 365] [--account 10000] [--target-vol 0.30] --apply    # write config
```

`--target-vol`: sets target annual volatility; computes `leverage = target_vol / ann_vol`

**`--apply` flag — protects live trading.** Default is dry-run: weights are computed and printed but `portfolio_config.json` is untouched, so a research run can never silently change the weights the live reconciler is trading on. The optimiser is seeded (`np.random.seed(42)`), so the `--apply` re-run produces exactly the weights shown in the dry-run (same stats.json inputs).

Required workflow:
1. Run `python3 manager/manager.py` (no `--apply`) and show the user the proposed weights.
2. Ask for explicit confirmation that these weights should go live.
3. Only after the user confirms, re-run the SAME command with `--apply` appended.
4. Never pass `--apply` on the first run, and never assume confirmation from context.

## portfolio_config.json — Exchange Routing

`manager.py` writes `weights` and `leverage`. You must manually add (or update) the `"exchanges"` dict whenever a strategy is deployed or moved to a different exchange:

```json
{
  "account_value": 10000,
  "leverage": 1.2,
  "weights":   { "btc_ti_long": 0.5, "btc_ti_short": 0.5 },
  "exchanges": { "btc_ti_long": "okx", "btc_ti_short": "okx" }
}
```

- Exchange routing is **not** in the strategy file — strategy files have no `EXCHANGE` field.
- Strategies missing from `"exchanges"` are silently skipped by the reconciler.
- The same strategy can be pointed at a different exchange by changing only this file.
- Valid values: any string the `place_order()` implementation in `reconciler.py` recognises (e.g. `"okx"`, `"taifex"`).

## reconciler.py

Polls every 5 seconds; only reconciles when a strategy's `state.json` mtime changes. `get_positions()` and `place_order()` are exchange-specific stubs to fill in once. `lib/portfolio.py` contains `reconcile()` logic; applies `leverage` from portfolio_config.

**Wiring exchange order libraries (required, not optional):** `get_positions()` and `place_order()` must call into a `lib/order_*.py` helper — never remain as `raise NotImplementedError`. When writing a new order library, immediately update `reconciler.py` to import and call it in the same session.

**Qty precision (most common cause of rejected orders):** before placing any order, the order library must know the symbol's qty step, min qty, and min notional — fetch them from the exchange and cache at startup:

| Exchange | Endpoint | Fields |
|---|---|---|
| Binance futures | `GET /fapi/v1/exchangeInfo` | `LOT_SIZE.stepSize`, `LOT_SIZE.minQty`, `MIN_NOTIONAL` |
| Binance spot | `GET /api/v3/exchangeInfo` | same filter names |
| OKX | `GET /api/v5/public/instruments?instType=SWAP` | `lotSz`, `minSz`, `ctVal` |
| Bybit | `GET /v5/market/instruments-info?category=linear` | `lotSizeFilter.qtyStep`, `lotSizeFilter.minOrderQty` |

Floor the qty to the step with `Decimal` — never float arithmetic:

```python
from decimal import Decimal, ROUND_DOWN
qty = Decimal(str(raw_qty)).quantize(Decimal(step_str), rounding=ROUND_DOWN)
params['quantity'] = format(qty, 'f')   # plain string — never 1e-05 notation
```

After flooring: if qty < min qty or `qty * price` < min notional → `return False` (skip, no phantom-trade notification). Never guess precision from memory — read it from the exchange API or the relevant `skills/blave-quant/references/` file.

```
bash manager/start_reconciler.sh
```

Run via `start_reconciler.sh`, not `reconciler.py` directly — the wrapper restarts on crash and sends a Telegram alert on each exit.

**Before starting the reconciler (or triggering a manual reconcile):** always show the user the pending order summary from `aggregate_portfolio()` + `compute_diff()` and ask for explicit confirmation. Only proceed if the user confirms.
