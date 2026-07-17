# Shared Library (lib/)

The workspace has a shared library at `lib/`. Use it to avoid duplicating code across strategies.

**Always import these — never write them inline:**

`lib/data.py` — all data fetching (chunking + cache built-in):
- `fetch_db_kline(dataset, symbol, schema, start, end, headers)` → CME/NYMEX/ICE OHLCV + `instrument_id` column; datasets: `GLBX.MDP3` (CL, GC), `IFEU.IMPACT` (BRN); schemas: `ohlcv-1m` / `ohlcv-1h` / `ohlcv-1d`
- `settlement_signals_from_db(df, signal)` → returns `(signal, exec_at_close)`. For futures strategies using `fetch_db_kline`: call at the end of `compute_signals` and `return` the result directly. Forces `signal=0.0` on the last bar before each contract rollover, and marks those bars `exec_at_close=True` (executed at this-bar close, not next-bar open). Do NOT use `-1.0` for settlement — that opens a short position.
- `fetch_kline(symbol, interval, start, end, headers)` → OHLCV DataFrame (Open/High/Low/Close/Volume)
- OHLC fetchers (`fetch_kline`, `fetch_db_kline`, `fetch_twfutures_ohlcv`, `fetch_twstock_price`) drop bars with impossible values (high<low, non-positive or NaN price) at read time and print the dropped timestamps — a printed `⚠️ dropped N bar(s)` warning means upstream data was corrupt, not a fetch failure; the cache keeps the raw bars
- `fetch_holder_concentration(symbol, interval, start, end, headers)` → DataFrame with `alpha` column
- `fetch_funding_rate(symbol, interval, start, end, headers)` → DataFrame with `alpha` (Binance only; alpha = funding rate × 100)
- `fetch_taker_intensity(symbol, interval, start, end, headers, timeframe='24h')` → DataFrame with `alpha`
- `fetch_whale_hunter(symbol, interval, start, end, headers, timeframe='24h', score_type='score_oi')` → DataFrame with `alpha`
- `fetch_unusual_movement(symbol, interval, start, end, headers, timeframe='24h')` → DataFrame with `alpha`
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
- `fetch_twstock_quote(sid, headers)` → dict, real-time last-quote snapshot (~10s refresh); no history, "now" only
- `fetch_twstock_quote_batch(stock_ids, headers)` → dict `{stock_id: quote_dict}`; max 50 ids

Taiwan stock data (universe, batch functions, fundamental factors, lookahead-bias table) — **`references/twstock.md`**

- `fetch_twfutures_ohlcv(symbol, schema, start, end, headers)` → Taiwan futures OHLCV DataFrame (Open/High/Low/Close/Volume); symbol: `'TXF'` or an individual stock futures id (股票期貨, e.g. `'CDF'`) currently backfilled with minute-line data — most of the 231 are NOT, unsupported symbol → 400; check `fetch_stock_futures_ohlcv_symbols()` first; schema: `'1d'`/`'1m'`/`'5m'`/`'15m'`/`'30m'`/`'60m'`; Volume in contracts. Intraday spans >62 days automatically use the server's 1m-parquet bulk export (one request per calendar year, resampled locally — fast, no per-request server computation) and silently fall back to chunked JSON fetches if that endpoint is unavailable
- `fetch_twfutures_ohlcv_batch(symbols, schema, start, end, headers, max_workers=8)` → dict `{symbol: DataFrame}`; concurrent multi-symbol form of `fetch_twfutures_ohlcv` (same cache/export semantics per symbol); use for any universe-wide intraday fetch — serial per-symbol loops waste ~2s of auth overhead per request
- `fetch_stock_futures_ohlcv_symbols(headers)` → list of symbols currently allowed by `fetch_twfutures_ohlcv` (always includes `'TXF'` plus whatever stock futures ids currently have backfilled minute-line data); call this before relying on intraday data for a stock future
- `fetch_twfutures_bid_ask_vol(start, end, headers)` → TXF 1-min bid/ask volume DataFrame (bid_vol, ask_vol, total_vol); bid_vol = 內盤 (seller-initiated), ask_vol = 外盤 (buyer-initiated); includes day + night sessions; max 31 days per chunk (auto-chunked)
- `fetch_twfutures_pcr(start, end, headers)` → DataFrame with a single `pcr` column (daily, index `date`); official TAIFEX put/call ratio (OI-based, 買賣權未平倉量比率%); the official ratio — NOT the value derived from option institutional / large-trader data in `references/twfutures.md`.
- `fetch_stock_futures_batch_daily(futures_ids, start, end, headers)` → dict `{futures_id: DataFrame}`; same fields as `fetch_twfutures_daily`; max 250 ids per call; `futures_ids` must be valid stock futures ids (股票期貨, e.g. `'CDF'`) — see `references/twfutures.md` for the minute-line-coverage caveat
- `txf_settlement_mask(index)` → boolean Series, True on the last bar strictly before each TAIFEX monthly settlement (3rd Wednesday, 13:30 TWN); interval-agnostic (1m → 13:29 bar, 60m → 13:00 bar). **MANDATORY for every strategy on `fetch_twfutures_*` intraday data — TXF and stock futures alike.** The source is Shioaji's R1 continuous near-month series, which switches contracts at settlement without price adjustment; an unmasked position books the contract-basis gap as fake PnL (measured 2018-2026 across 10 stock futures: mean +0.36% per roll, std 3.9%, August dividend-season mean -1.9% — a fully-invested unmasked backtest fabricates roughly +4%/yr × gross exposure). Masking also charges the real monthly roll fees the naive backtest omits. Type A: `settle = txf_settlement_mask(df.index); signal[settle] = 0.0; return signal, settle`. Type C: `weights.loc[settle] = 0.0; return weights.values, price_df, settle.values`
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

`lib/quality_check.py` — CLI, not an import: `python3 lib/quality_check.py strategies/<file>.py`. Static scan for a broken/unfilled `compute_signals()` contract (CRITICAL, exit 2) and `FEE=0` / non-constant `FEE` (WARNING, exit 1). Run on any Type A/C strategy before its first backtest and before marketplace submission (full flow: `references/marketplace.md`). Companion to `lib/security_check.py` (malicious-code scan for downloaded strategies — same exit-code convention).

`lib/notify.py`:
- `from lib.notify import make_sender, send_text, send_photo`
- `make_sender()` → text sender function (broadcasts to all paired chat IDs)
- `make_sender(photo=True)` → photo sender function
- Senders raise `RuntimeError` if Telegram rejects the send (never fails silently) — do not report a notification as sent unless the call returned without raising
- Text messages longer than 4096 chars are split into multiple messages automatically (`TELEGRAM_TEXT_LIMIT`)
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

`lib/guard.py` — **kill switch + order audit log.** Enforced inside `lib/order_*.py`'s transport layer — no caller opts in, nothing to wire:
- If the file `state/HALT` exists, every ENTRY order raises `guard.Halted` before any network call. Reduce-only closes, SL/TP, and cancels still work (flattening must never be trapped). The file's existence is authoritative — malformed content still halts.
- `trip_halt(reason, source)` — set it (user says 停 / healthcheck anomaly). `clear_halt(source)` — **only on explicit user instruction; NEVER clear a halt on your own initiative.** `halted()` / `halt_info()` — check state.
- Every order attempt / outcome / denial is appended to `state/audit.jsonl` (fsynced). When the user asks "你到底下了什麼單", read this file — it is the record of what was actually sent, not what was intended.

`lib/order_bingx.py` — **BingX swap (USDT-M perp) order execution. Ships implemented — NEVER hand-write BingX order calls in a strategy; import from here.** All functions take `env` (dotenv dict) first. `direction` is always the POSITION's direction (`'long'`/`'short'`); position mode (one-way vs hedge) is auto-detected.
- `open_position(env, symbol, direction, qty, sl_price=, tp_price=, client_order_id=)` — **the recommended entry flow**: ONE atomic market order with SL/TP attached (no naked window), polled to FILLED, protection verified in open orders. Returns `{'entry': confirmed, 'protection': [...]}`. Raises `ProtectionFailed` if protection isn't visible after the fill — treat that as an ALERT-THE-USER-NOW event, never swallow it.
- `place_market_order(...)` — confirmed market order; returns exchange-reported `avg_price` / `executed_qty` / `commission` (never report the intent — report these)
- `place_limit_order(...)` — returns unconfirmed order with `order_id`; track via `confirm_order`
- `confirm_order(env, symbol, order_id, timeout=15)` — polls to terminal state; raises `BingXError` on CANCELED/FAILED, `OrderNotConfirmed` on timeout (order may still fill — re-query, do NOT blindly resubmit)
- `place_protective_orders(env, symbol, direction, qty=None, sl_price=, tp_price=)` — standalone SL/TP for an existing position; `qty=None` protects the whole position via `closePosition`; verified on-exchange, raises `ProtectionFailed` otherwise. Use for multi-level TPs or repairing protection.
- `close_position(env, symbol, direction, qty)` — confirmed reduce-only market close
- `get_order` / `get_open_orders` / `cancel_order` / `cancel_all_orders`
- `get_fills(env, symbol, start_ms, end_ms)` — fill history (≤30 days; handles BingX's startTs/endTs + fill_orders quirks)
- `format_qty` / `format_price` — precision flooring + min-qty/min-notional validation (raises instead of sending a doomed order)
- `get_leverage` / `set_leverage`, `get_position_mode`, `claim_demo_funds` (VST)
- Set `BINGX_DEMO=true` in `.env` to run the same code against VST paper trading
- Always pass `client_order_id` (alphanumeric, ≤40 chars, unique per signal — e.g. `f"{strategy}{signal_ts:%Y%m%d%H%M%S}"`) so a resubmit is rejected by the exchange instead of doubling the position
- Wire `manager/reconciler.py`'s `get_positions()` / `place_order()` through this lib + `lib/account_bingx.py` for BingX users

`lib/order_sinopac.py` — **SinoPac (永豐金) Taiwan stock odd-lot order execution (Shioaji). Ships implemented — NEVER hand-write Shioaji order calls; import from here.** All functions take `env` (dotenv dict) first. Harvested from the first live TW-stock deployment, which lost three rounds of orders to silent rejections (see `references/sinopac-broker.md` § Field-Verified Lessons — read it before any SinoPac work). Requires `SINOPAC_LIVE=true` in `.env` for real orders; without it Shioaji runs in simulation mode against a fake account.
- `place_order_sinopac(env, symbol, signed_diff, client_tag=)` — **the reconciler entry point**: signed_diff in TWD (>0 buy, <0 sell), converted to shares at last price and split into ≤999-share odd-lot MKT orders, each polled to acknowledgement. Returns `{'orders', 'filled_qty', 'target_qty', ...}`, or `False` if the diff is under one share (skip).
- `place_odd_lot_order(env, symbol, 'buy'|'sell', shares, client_tag=)` — one confirmed odd-lot order (1-999 shares). Returns exchange-reported `status` / `filled_qty` / `avg_fill_price` / `msg` — report those, never the intent. Raises `SinopacError` on rejection (with the broker's message), `OrderNotConfirmed` if never acknowledged (query again, do NOT blindly resubmit).
- `client_tag`: ≤6 alphanumeric chars (Shioaji `custom_field`), unique per intent — a same-day resubmit with the same tag raises `DuplicateOrder` locally (Shioaji has no exchange-side clientOrderId dedup).
- `get_sinopac_positions(env)` — `{symbol: {'side': 'long', 'size': TWD}}` (reconciler shape, odd-lot-aware via `unit=Share`); `get_account_balance(env)` — settlement balance in TWD, raises on failure.
- Buy orders pass through `lib/guard` (halt + audit) like BingX; sells are never trapped. Login + CA activation happen on first call and fail loudly if the CA certificate isn't activated.
- Futures (TXF) are NOT covered yet — raw-Shioaji pattern with mandatory confirmation is in `references/sinopac-broker.md`; harvest into this lib after the first live futures deployment.

**When writing new reusable logic** (new exchange order helper, new alpha data fetcher, etc.):
- Add it to the appropriate `lib/` file first (or create a new one, e.g. `lib/orders_binance.py`)
- Then import it in the strategy

**Strategy-specific logic** (signal computation, indicator setup, place_order for a specific exchange) stays in the strategy file.

Skill examples show data-fetch patterns only — integrate them into TEMPLATE_A.py using lib/ imports.

**Marketplace lib rule** — strategies shared via marketplace must follow this boundary strictly:
- `compute_signal`, indicator calculation, and all logic that affects trade decisions MUST stay in the strategy file — never in a custom lib
- Custom `lib/` files must only contain IO utilities (exchange order helpers, data fetchers) — logic that can be fully described by interface + behavior, not by implementation detail
- This allows recipients to reconstruct missing custom lib from the description without risking strategy inconsistency
