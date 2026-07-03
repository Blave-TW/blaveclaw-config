# Shared Library (lib/)

The workspace has a shared library at `lib/`. Use it to avoid duplicating code across strategies.

**Always import these — never write them inline:**

`lib/data.py` — all data fetching (chunking + cache built-in):
- `fetch_db_kline(dataset, symbol, schema, start, end, headers)` → CME/NYMEX/ICE OHLCV + `instrument_id` column; datasets: `GLBX.MDP3` (CL, GC), `IFEU.IMPACT` (BRN); schemas: `ohlcv-1m` / `ohlcv-1h` / `ohlcv-1d`
- `settlement_signals_from_db(df, signal)` → returns `(signal, exec_at_close)`. For futures strategies using `fetch_db_kline`: call at the end of `compute_signals` and `return` the result directly. Forces `signal=0.0` on the last bar before each contract rollover, and marks those bars `exec_at_close=True` (executed at this-bar close, not next-bar open). Do NOT use `-1.0` for settlement — that opens a short position.
- `fetch_kline(symbol, interval, start, end, headers)` → OHLCV DataFrame (Open/High/Low/Close/Volume)
- `fetch_holder_concentration(symbol, interval, start, end, headers)` → DataFrame with `alpha` column
- `fetch_funding_rate(symbol, interval, start, end, headers)` → DataFrame with `alpha` (Binance only; alpha = funding rate × 100)
- `fetch_taker_intensity(symbol, interval, start, end, headers, timeframe='24h')` → DataFrame with `alpha`
- `fetch_whale_hunter(symbol, interval, start, end, headers, timeframe='24h', score_type='score_oi')` → DataFrame with `alpha`
- `fetch_squeeze_momentum(symbol, start, end, headers)` → DataFrame with `alpha` (period fixed to 1d)
- `fetch_liquidation(symbol, interval, start, end, headers, timeframe='24h')` → DataFrame with `alpha`
- `fetch_market_direction(interval, start, end, headers)` → DataFrame with `alpha` (no symbol)
- `fetch_capital_shortage(interval, start, end, headers)` → DataFrame with `alpha` (no symbol)
- `fetch_market_sentiment(symbol, interval, start, end, headers)` → DataFrame with `alpha`
- `fetch_top_trader_exposure(interval, start, end, headers)` → DataFrame with `alpha` (BTC only, no symbol)

**Taiwan stock price — raw vs adjusted (critical distinction):**
- `fetch_twstock_price(sid, start, end, headers)` → Open/High/Low/Close/Volume, **actual market prices**. Use when the user asks to plot or view a stock's trend — matches what they see on broker apps.
- `fetch_twstock_price_adj(sid, start, end, headers)` → Open/Close, **dividend-adjusted backward prices**. Use for backtesting only — ensures returns are comparable across ex-dividend dates.
- Never use `fetch_twstock_price_adj` just to draw a chart; the adjusted prices look different from real market prices and will confuse users.

Taiwan stock data (universe, batch functions, fundamental factors, lookahead-bias table) — **`references/twstock.md`**

- `fetch_twfutures_ohlcv(symbol, schema, start, end, headers)` → Taiwan futures OHLCV DataFrame (Open/High/Low/Close/Volume); symbol: `'TXF'`; schema: `'1d'`/`'1m'`/`'5m'`/`'15m'`/`'30m'`/`'60m'`; Volume in contracts
- `fetch_twfutures_bid_ask_vol(start, end, headers)` → TXF 1-min bid/ask volume DataFrame (bid_vol, ask_vol, total_vol); bid_vol = 內盤 (seller-initiated), ask_vol = 外盤 (buyer-initiated); includes day + night sessions; max 31 days per chunk (auto-chunked)
- `fetch_twfutures_pcr(start, end, headers)` → DataFrame with a single `pcr` column (daily, index `date`); official TAIFEX put/call ratio (OI-based, 買賣權未平倉量比率%); the official ratio — NOT the value derived from option institutional / large-trader data in `references/twfutures.md`.
- `txf_settlement_mask(index)` → boolean Series, True on the last 1-min bar before TXF monthly settlement (3rd Wednesday of each month, 13:30 TWN). Use with intraday TXF strategies: `settle = txf_settlement_mask(df.index); signal[settle] = 0.0; return signal, settle`
- **Data history ranges** (earliest available date per endpoint) are NOT listed here — they live in the `blave-quant` skill / Notion API doc, which auto-update on each box. This config is baked in at provision time (no live-update path), so a start date copied here would silently go stale. Check the skill before assuming an endpoint's earliest date.

`lib/execute.py` — all order execution logic (state management + algo orders):
- `from lib.execute import update_state, load_state, save_state` — state management
- `state.json` schema: `{"position": float, "symbol": str}` — `position` is the current signal value (positive=long, negative=short, 0=flat); all deployment config (exchange, asset_spec) lives in `portfolio_config.json`, not in state
- `from lib.execute import run_twap` — TWAP execution engine (exchange-agnostic). Use for any strategy type (A/B/C) when the user wants to split a large order over time instead of a single market order.
  - `run_twap(symbol, side, total_qty, duration_min, n_slices, place_slice_fn, twap_key, signal_price=None, send_telegram_fn=None)`
  - `place_slice_fn(symbol, side, qty) → {'fill_price': float, 'fill_qty': float}` — implement this per exchange (e.g. Binance, Bybit); raise on failure
  - `twap_key`: **symbol+direction** key like `'btcusdt_long'` / `'btcusdt_short'` — NOT a strategy name. Reconciler nets orders per symbol, so there is no single strategy name at execution time.
  - Logs every slice + summary to `manager/twap/{twap_key}.jsonl` (under `manager/`, never `strategies/`); sends Telegram per slice and on completion
  - `signal_price` (optional): price at signal time — used to compute `slippage_bps` per slice and in the summary
- `from lib.execute import load_twap_log` — `load_twap_log(twap_key)` returns `(slices, summaries)` from `manager/twap/{twap_key}.jsonl`; use to analyze how TWAP parameters (duration, n_slices) affect execution quality vs strategy signal price
- **TWAP wiring pattern** — when the user asks to use TWAP, modify `reconciler.py`. **Key insight: `reconcile()` nets orders per symbol** (see `lib/portfolio.py` — one order per symbol, multiple `contributors`, flips split into reduce-only + open legs). There is NO single strategy name at order-placement time, so TWAP is keyed by symbol+direction, not by strategy:
  1. Add a key generator so config, lookup, and log path always agree:
     ```python
     def _twap_key(symbol, signed_diff):
         return f"{symbol.lower()}_{'long' if signed_diff > 0 else 'short'}"
     ```
  2. Add `TWAP_CONFIG = {"btcusdt_long": {"duration_min": 30, "n_slices": 10}, "btcusdt_short": {...}, ...}` at the top — keys are `symbol_direction`, NOT strategy names.
  3. Implement `_place_slice(symbol, side, qty)` for the exchange (read the relevant `skills/blave-quant/references/` file first).
  4. In `place_order(symbol, signed_diff, ...)`, compute `key = _twap_key(symbol, signed_diff)`, check `TWAP_CONFIG.get(key)`, and call `run_twap(..., twap_key=key, ...)` if present, else fall back to a direct market order.
  - `place_order` does NOT need a `strategy_name` parameter — the key is derived from `symbol + signed_diff`.
  - Symbol/directions not in `TWAP_CONFIG` continue using market orders unchanged.
  - Never write TWAP logs under `strategies/` — that folder is for `strategy.py` files; doing so creates orphan folders with no strategy.

`lib/analysis.py`:
- `from lib.analysis import regime_analysis, plot_regime` — regime breakdown and regime chart

`lib/param_scan.py`:
- `from lib.param_scan import percentile_thresholds` — use p5/p95 as bounds, linspace n_parts values → returns (entry_vals, exit_vals); prints distribution stats
- `from lib.param_scan import scan_grid` — run 2D param scan, returns Sharpe grid. Supports all strategy types:
  - `row_param`/`col_param`: kwarg names forwarded to `compute_signals_fn` (default `'entry_th'`/`'exit_th'`)
  - `warmup`: leading bars to skip from PnL (rolling window warm-up); `compute_signals_fn` receives the FULL df for accurate rolling, PnL computed on `df.iloc[warmup:]`
  - `valid_fn`: `(row_val, col_val) → bool` to skip invalid combos (default: `row > col`; for SMA use `lambda f, sl: f < sl`)
  - If `compute_signals_fn` returns a tuple `(signal, settle)`, `settle` is automatically used as `exec_shifted`
  - **All strategy types use the same `scan_grid` call** — see `examples/btc_sma_cross/scan.py` for the canonical SMA-scan pattern and `examples/btc_ti_5min/scan.py` for the threshold-scan pattern
- `from lib.param_scan import find_plateau, plot_heatmap` — plateau detection and heatmap chart
  - `find_plateau` returns 5 values: `best_idx, nbr_mean, best_row, best_col, best_sharpe` — use `best_row`, `best_col`, `best_sharpe` directly; `nbr_mean` is a **2D array** (do NOT format it as a scalar)
  - Canonical usage: `best_idx, _, best_row, best_col, best_sharpe = find_plateau(grid, ROW_VALS, COL_VALS)`
  - `plot_heatmap` `output_path` is **required** — always pass `output_path='strategies/{strategy_name}/heatmap.png'`, never `/tmp/`
  - `plot_heatmap` **auto-sends the heatmap to Telegram** (via `send_photo`) — you do NOT need a manual `send_photo` after it. Pass `send_telegram=False` only if you explicitly want to suppress it.
- `from lib.analysis import precise_pnl, compute_stats` — available if you need a custom loop (rare)

**Parameter scan workflow:**
1. Run `scan.py` to find the best parameters
2. **Update the params directly in the existing `strategy.py`** — do NOT create a new strategy folder
3. Run backtest in the same `strategy.py` to verify
Never create a duplicate strategy folder just because you ran a scan.

`lib/validation.py`:
- `from lib.validation import mcpt, plot_mcpt` — Monte Carlo Permutation Test; call `mcpt(close, position, n=2000, fee=..., target_vol=..., ...)` → `(actual_sharpe, p_value, dist)`
- **All validation (MCPT, walk-forward, etc.) goes in a separate `validate.py` in the strategy folder** — never inside `strategy.py`.
- Daily stock params: `periods_per_year=252`, `vol_window=60`, `max_lev=1.0`

`lib/notify.py`:
- `from lib.notify import make_sender, send_text, send_photo`
- `make_sender()` → text sender function (broadcasts to all paired chat IDs)
- `make_sender(photo=True)` → photo sender function
- Use `send_telegram_fn=make_sender()` when calling `run()`
- **CRITICAL — Pairing check (run at session start, before any other action):** Check pairing status first (see Telegram Pairing Check in `references/strategy-code.md`). If not paired: tell the user "Telegram is not paired yet. Please complete the pairing flow via the bot." Do not proceed with any strategy run or notification until pairing is confirmed.

`lib/strategy.py`:
- `from lib.strategy import add_realized_vol` — computes realized_vol in-place. **Standard window is 30 days** — convert to bars based on strategy interval (e.g. 1d→30, 1h→720, 5min→8640)
- `from lib.strategy import apply_vol_scaling` — scales signal by `(target_vol / realized_vol).clip(vol_cap)`; works for both long and short; call at the end of `compute_signals`
  - Standard defaults: `target_vol=0.30`, `vol_cap=2.0`
  - Must call `add_realized_vol` first so df has a `realized_vol` column
- **All risk-parity strategies must use these two functions — do NOT compute vol inline in the strategy file**

`lib/pnl.py`:
- `from lib.pnl import daily_returns_typeA, daily_returns_typeC` — extracts daily returns from pf_series (called automatically by runner, no manual use needed)
- `from lib.pnl import load_all_stats` — reads all `strategies/*/stats.json` (including daily_returns) for use by manager

**Exchange account libraries** (`lib/account_{exchange}.py` — equity + positions for `manager/snapshot.py`):
- `lib/account_bingx.py` already ships implemented (swap/futures account) — do NOT rewrite it, extend it if spot/fund balance is needed
- For any other exchange, copy `lib/account_TEMPLATE.py` — see `references/manager.md` § Account library

**When writing new reusable logic** (new exchange order helper, new alpha data fetcher, etc.):
- Add it to the appropriate `lib/` file first (or create a new one, e.g. `lib/orders_binance.py`)
- Then import it in the strategy

**Strategy-specific logic** (signal computation, indicator setup, place_order for a specific exchange) stays in the strategy file.

Skill examples show data-fetch patterns only — integrate them into TEMPLATE_A.py using lib/ imports.

**Marketplace lib rule** — strategies shared via marketplace must follow this boundary strictly:
- `compute_signal`, indicator calculation, and all logic that affects trade decisions MUST stay in the strategy file — never in a custom lib
- Custom `lib/` files must only contain IO utilities (exchange order helpers, data fetchers) — logic that can be fully described by interface + behavior, not by implementation detail
- This allows recipients to reconstruct missing custom lib from the description without risking strategy inconsistency
