# Shared Library (lib/)

The workspace has a shared library at `lib/`. Use it to avoid duplicating code across strategies.

**Always import these — never write them inline:**

Every `fetch_*` function takes `headers` as its auth argument. Build it once from the workspace `.env` — the keys are lowercase, and the header names are `api-key` / `secret-key` (not `X-API-KEY`, and there is no request signing):

```python
from dotenv import dotenv_values
env  = dotenv_values()   # run from the workspace root, or pass the .env path
hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}
```

`lib/data.py` — all data fetching (chunking + cache built-in):
- `fetch_db_kline(dataset, symbol, schema, start, end, headers)` → CME/NYMEX/ICE OHLCV + `instrument_id` column; datasets: `GLBX.MDP3` (CL, GC), `IFEU.IMPACT` (BRN); schemas: `ohlcv-1m` / `ohlcv-1h` / `ohlcv-1d`
- `settlement_signals_from_db(df, signal)` → returns `(signal, exec_at_close)`. For futures strategies using `fetch_db_kline`: call at the end of `compute_signals` and `return` the result directly. Forces `signal=0.0` on the last bar before each contract rollover, and marks those bars `exec_at_close=True` (executed at this-bar close, not next-bar open). Do NOT use `-1.0` for settlement — that opens a short position.
- `fetch_kline(symbol, interval, start, end, headers)` → OHLCV DataFrame (Open/High/Low/Close/Volume). **Binance USDT-M perps only** — a contract listed elsewhere is simply absent, so a symbol that "works" is not proof it is the user's instrument
- `fetch_bingx_kline(symbol, interval, start, end)` → OHLCV DataFrame for a BingX perpetual, straight from BingX's public API (no `headers` — no key needed). Use for contracts `fetch_kline` does not carry. `symbol` is the BingX **API** symbol, not the chart's display name: GOLD(XAU)-USDT is `NCCOGOLD2USD-USDT` — resolve it via `GET https://open-api.bingx.com/openApi/swap/v3/quote/contracts` (match `displayName`, use `symbol`). Intervals: `1min`/`3min`/`5min`/`15min`/`30min`/`1h`/`2h`/`4h`/`6h`/`8h`/`12h`/`1d`/`3d`/`1w`
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
**Macro economic calendar — the only source for macro events and their numbers:**
- `fetch_economic_calendar(headers, start=None, end=None, countries=None, max_priority=None, limit=None, lang='zh')` → DataFrame, one row per event, sorted by event time. Columns: `datetime` (Taipei time; events with no published time get 00:00), `date`, `time` (`'HH:MM'` Taipei, may be `None`), `country` (ISO-2) / `country_name` (Chinese), `subject` (indicator name) / `subject_title` (period, e.g. `'<7月>'`, `'<2季>'`), `predict` (market consensus), `last` (prior), `real` (actual; `None` until released), `unit`, `priority`.
- **`priority` runs 1 → 3 with 1 the MOST important** (1 = non-farm payrolls, rate decisions; 3 = rig counts, used-car indices). `max_priority` keeps only `priority <=` it, so "just the big events" is `max_priority=1`, not 3. Getting this backwards silently returns ~979 rows of noise instead of the 85 that matter.
- Other filters: `start`/`end` are `'YYYY-MM-DD'` Taipei dates (inclusive); `countries` is a list of ISO-2 codes, e.g. `['US', 'CN', 'TW']`; `limit` truncates after sorting; `lang` (`'zh'` default / `'en'`) picks the display language for indicator and country names — the server only swaps names it has in its lookup table, so `'en'` returns a zh/en mix. Unfiltered the feed is ~1,400 rows — always filter.
- **Coverage is a rolling ~5-week window (roughly the past month plus the next few weeks), not a history archive.** A range outside it returns an empty DataFrame rather than an error — do not read that as "no events scheduled". For anything older, there is no endpoint; say so instead of substituting a remembered figure.
- **Never answer a macro-event question from a web search or from memory.** Search results land on calendar-aggregator content farms whose own tables are wrong (measured: one listed a published actual as the consensus), and any field the page misses gets filled in from training data — that is how a digest ended up with China's manufacturing PMI prior at 49.5 when the published figure was 50.3, and a Q2 GDP consensus 0.7pp off. If this call cannot answer it, say so rather than substituting a number.
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

**Exchange account libraries** (`lib/account_{exchange}.py` — equity + positions readers, discovered by filename):
- `lib/account_bingx.py` already ships implemented (swap equity + spot/fund wallet breakdown) — do NOT rewrite it, extend it in place
- `lib/account_okx.py` already ships implemented — unified TRADING account (`totalEq`) as equity + separate `funding` wallet in the breakdown; `OKX_DEMO=true` in `.env` routes to OKX simulated trading. Position sizes are ctVal-exact (pos × ctVal from the public instruments endpoint, cached — the positions response's own `ctVal` field is null, and notional/markPx reads back imprecise: measured 0.001 ETH as 0.00099911, which floors to 0 contracts and becomes unclosable dust)
- `lib/account_binance.py` already ships implemented — USDⓈ-M futures `totalMarginBalance` as equity + ALL ten wallets in the breakdown (spot/funding/cross+isolated margin/UM+CM futures/earn/options/bots/copy) via `/sapi/v1/asset/wallet/balance` (BTC-denominated, converted; every row measured accurate on a live account, unlike BingX's overview endpoint)
- `lib/account_gateio.py` already ships implemented — USDT futures account (`total` + `unrealised_pnl`) as equity (the wallet orders draw on); every other wallet (spot/margin/delivery/options/earn…) via `/wallet/total_balance` in the breakdown (needs wallet permission on the key — best-effort, and the transfer endpoint `/wallet/transfers` needs it too). A futures account that was never funded raises `USER_NOT_FOUND` — handled as equity 0 (working link), not an error. Position sizes are quanto_multiplier-exact (signed contracts × multiplier from the public contract detail, cached — the OKX ctVal lesson); dual (hedge) mode rows carry `mode=dual_long/dual_short`. Accepts `GATEIO_*` (web 下單設定) or `GATE_*` (skill convention) key names
- All four also implement `get_holdings(env)` — coin holdings in spot/funding wallets (`[{asset, amount, usdt_value, wallet}]`, display-only): the web lists them under 持幣 next to the positions table. **Holdings are NEVER positions and never reach the reconciler** — a spot coin fed to it would net futures orders against spot inventory. Unpriceable coins (delisted, e.g. ETHW) keep `usdt_value=None` but are still listed; dust <0.1 USDT dropped.
- For any other exchange, copy `lib/account_TEMPLATE.py` — see `references/manager.md` § Account library

`lib/venue_wiring.py` — **reconciler auto-wiring for official venues. The template `manager/reconciler.py` already routes through it — when the bound venue ships both official libs (Binance/BingX/OKX/Gate.io), there is NOTHING to wire, and rebinding to another official venue needs no reconciler change.** Detects the venue from `.env` keys + lib files, maps the reconciler's USD-diff contract onto the venue's order lib (swap via `get_mark_price`, spot via quote-sized buys / inventory-capped sells, `spot_scope` incl. exit-on-removal), and bakes in the harvested fixes (reduce legs ceiled to a whole lot and capped at the position; the older bingx below-min ValueError mapped to the False-skip contract). Hand-wire the two reconciler functions ONLY for venues without official libs — the template docstrings carry the contract.

`lib/guard.py` — **kill switch + order audit log.** Enforced inside `lib/order_*.py`'s transport layer and again in `lib/portfolio.reconcile()` — no caller opts in, nothing to wire:
- If the file `state/HALT` exists, every ENTRY order raises `guard.Halted` before any network call. Reduce-only closes, SL/TP, and cancels still work (flattening must never be trapped). The file's existence is authoritative — malformed content still halts.
- `reconcile()` denies exposure-adding legs itself, before calling `place_order_fn`. That is the layer that covers a reconciler whose exchange has no official `lib/order_*.py` and whose `place_order` was hand-written — the transport-layer check alone would miss it. Denials are audited and logged, but deliberately NOT sent to Telegram: the user tripped the halt to stop the noise.
- `trip_halt(reason, source)` — set it (user says 停 / healthcheck anomaly). `clear_halt(source)` — **only on explicit user instruction; NEVER clear a halt on your own initiative.** `halted()` / `halt_info()` — check state.
- Every order attempt / outcome / denial is appended to `state/audit.jsonl` (fsynced). When the user asks "你到底下了什麼單", read this file — it is the record of what was actually sent, not what was intended.

`lib/order_TEMPLATE.py` — **contract for writing a new perp venue's order lib** (Taiwan brokers follow `lib/order_sinopac.py` instead). Copy it, keep the filename convention (`lib/order_{exchange}.py`). The reconciler calls four FIXED names — `get_contract_rules` / `format_qty` / `place_market_order` / `close_position_partial` — a lib missing any of them crashes at reconcile time, mid-trade. `lib/order_bingx.py` is the reference implementation.

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
- **Spot layer** (strategies declaring `MARKET = "spot"`): `place_spot_market_order(env, symbol, 'buy'|'sell', base_qty=, quote_qty=, client_order_id=)` — BUYs sized in QUOTE currency (`quoteOrderQty`), SELLs in base qty floored to the spot step (`get_spot_rules`/`format_spot_qty` — spot rules differ from swap: sizes not digit counts, and minimums are ASYMMETRIC: BTC sell-min ≈ $12 vs buy-min $0.5, a small buy can be unsellable dust → returns False); polls to terminal (placement can report PENDING); guard buy=entry / sell=reduce; spot client-id param is `newClientOrderId` (swap uses `clientOrderID`). Plus `get_spot_balances` / `get_spot_price` / `get_spot_order` for reconciler wirings.
- Always pass `client_order_id` (alphanumeric, ≤40 chars, unique per signal — e.g. `f"{strategy}{signal_ts:%Y%m%d%H%M%S}"`) so a resubmit is rejected by the exchange instead of doubling the position
- Wire `manager/reconciler.py`'s `get_positions()` / `place_order()` through this lib + `lib/account_bingx.py` for BingX users

`lib/order_binance.py` — **Binance USDⓈ-M futures order execution. Ships implemented — NEVER hand-write Binance order calls in a strategy; import from here.** Same contract and surface as `lib/order_bingx.py` (all functions take `env` first; `direction` is the POSITION's direction; four reconciler names + limit/cancel/protective/leverage helpers; `lib/guard` halt + audit built in). Binance-specific facts, all measured live 2026-08-04:
- Broker attribution is per order: every order carries `newClientOrderId` prefixed `x-52DDFAFN` (built inside the lib — pass a plain ≤26-char `client_order_id` suffix; over-long raises instead of truncating)
- qty is base units natively (contract_value always 1.0 — no conversion anywhere)
- **Conditional orders (SL/TP) live on the separate Algo Order API**: `/fapi/v1/order` rejects `STOP_MARKET` with `-4120`; the algo endpoints use `triggerPrice` (not stopPrice), `clientAlgoId` (broker prefix rides it), return `algoId`/`algoStatus`, and have their own open/cancel calls — `get_open_orders` does NOT show them, use `get_open_algo_orders` / `cancel_algo_order`; `cancel_all_orders` clears both sides
- No atomic SL/TP on entry (unlike BingX): `open_position` = entry then protective orders, verified on-exchange, `ProtectionFailed` if not visible
- hedge mode rejects the `reduceOnly` param (-1106) — the lib expresses closes via positionSide there; one-way verified live, hedge path code-reviewed only (account was one-way)
- `commission` comes from `userTrades` (order query has none, lags fill ~1s — retried) and is NOT always USDT: BNB-discount accounts pay BNB, so read `commission_asset` alongside
- `BINANCE_DEMO=true` routes to the futures testnet (needs separate testnet keys, unlike BingX VST)
- **Spot layer** (strategies declaring `MARKET = "spot"`): `place_spot_market_order(env, symbol, 'buy'|'sell', base_qty=, quote_qty=, client_order_id=)` — BUYs are sized in QUOTE currency (`quote_qty` → Binance `quoteOrderQty`, no price conversion), SELLs in base qty floored to the SPOT step (separate rules from futures — `get_spot_rules`/`format_spot_qty`); response carries fills inline (`FULL`), no polling; broker prefix `x-GBN6HWR2`; below min → `False`. Guard: buy=entry (halt blocks), sell=reduce (never trapped). Spot cannot short — `lib/portfolio` clamps net-negative spot targets to 0, loudly. Plus `get_spot_balances(env)` / `get_spot_price(env, symbol)` for reconciler wirings.

**Market dimension (spot vs swap) in the reconcile pipeline** — `lib/portfolio.py`:
- A strategy declares `MARKET = "spot"` in strategy.py (undeclared = swap). Spot and swap flow through the SAME pipeline, distinguished by the target/actual KEY: swap keys stay the plain canonical symbol (`BTCUSDT`), spot keys carry `@spot` (`BTCUSDT@spot`) — same symbol, different inventory, never netted against each other.
- Helpers: `market_key(symbol, market)` / `split_key(key)` / `strategy_market(name)` / `spot_symbols()` / `spot_scope(inventory_value_fn)`. Reconciler wirings: `get_positions()` adds `market_key(sym,'spot')` rows for every symbol `spot_scope()` returns (inventory value = base balance × price), and `place_order()` routes on `split_key(symbol)[1]`.
- **Exit-on-removal (futures parity)**: `spot_scope` persists managed symbols in `manager/spot_scope.json` — removing a spot strategy keeps its symbol in `actual` until the inventory sells below the reconcile threshold, so removal EXITS the position instead of orphaning coins (measured 2026-08-04: removal before this existed left inventory stranded with no history entry). Personal coins in symbols no strategy targets never enter the scope; a targeted symbol's inventory is ONE pool — same-symbol personal coins are co-managed (incl. the removal sell-down).

`lib/order_okx.py` — **OKX order execution (USDT perp swap + spot). Ships implemented — NEVER hand-write OKX order calls; import from here.** Same contract and surface as the other order libs (four reconciler names + protective/cancel helpers, `lib/guard` built in). OKX facts, all measured live 2026-08-05 on a hedge-mode account:
- Broker attribution is the body field `"tag": "96ee7de3fd4bBCDE"` — injected by the transport on every order POST
- **swap `sz` is CONTRACTS**: base qty ÷ ctVal, computed in Decimal end-to-end (float division under-sizes by a whole lot at exact multiples — measured: 0.01 ETH became 0.009); `format_qty` returns contracts and is a min-gate ONLY
- The real error is `data[].sCode/sMsg` — top-level code/msg often just says "Operation failed"; the lib surfaces the inner layer
- net mode has native `reduceOnly`; hedge (long_short) mode does NOT — closing = `posSide` addressed with the opposite order side (auto-detected from /api/v5/account/config)
- SL/TP live on `/api/v5/trade/order-algo` (`ordType=conditional`, market exec via `slOrdPx=-1`, whole-position via `closeFraction=1`), verified in `orders-algo-pending`, cancelled via `cancel-algos`
- **Spot layer**: BUYs sized in QUOTE currency (`tgtCcy=quote_ccy`, `sz`=USDT), SELLs in base (`tgtCcy=base_ccy`); `tdMode=cash`; guard buy=entry / sell=reduce; 51020 (below minimum) → `False`
- `OKX_DEMO=true` routes via the `x-simulated-trading` header (demo keys)

`lib/order_gateio.py` — **Gate.io order execution (USDT perp futures + spot). Ships implemented — NEVER hand-write Gate.io order calls; import from here.** Same contract and surface as the other order libs (four reconciler names + protective/cancel helpers, `lib/guard` built in). Gate.io facts, all measured live 2026-08-05 (single AND dual mode, futures + spot):
- Broker attribution is the header `X-Gate-Channel-Id: blave` — injected by the transport on EVERY request; verified landing: fills carry `biz_info "ch:blave"`, price orders `broker "blave"`
- **futures `size` is signed CONTRACTS** (positive=long, negative=short): base qty ÷ `quanto_multiplier`, computed in Decimal (the OKX ctVal pitfall); `format_qty` returns contracts and is a min-gate ONLY; market order = `price "0"` + `tif ioc`
- **`text` custom ids are labels, NOT idempotency — measured: two orders with the identical text both filled** (no dedup at all, not even Binance/OKX's un-filled window) — on `OrderNotConfirmed` always re-query, a blind resubmit doubles the position; for the same reason the transport does NOT auto-retry POSTs on connection errors (a reset after the request went out may have filled — the reconciler re-derives the diff from live positions next round), GET/DELETE stay retryable
- dual (hedge) mode needs no posSide field: `reduce_only` + the order's sign uniquely addresses the side; whole-position close = `size 0` + `close true` (single) / `auto_size close_long|close_short` (dual) — all verified with both sides open simultaneously
- SL/TP live on `/futures/usdt/price_orders` (initial market order + mark-price trigger, `rule` 1=≥ / 2=≤), own id namespace + open/cancel endpoints; futures fees come from `my_trades` (order responses carry none), spot fees ride the order itself
- **Spot layer**: market BUYs are natively QUOTE-sized (`amount` = USDT spend — Gate.io's own semantics), SELLs in base floored to `amount_precision`; mins are explicit per pair (`min_quote_amount` — 3 USDT on BTC_USDT / `min_base_amount`), below → `False`; guard buy=entry / sell=reduce
- **Spot BUY fees are deducted from the received BASE asset** (bought 0.000093 BTC, fee 9.3e-8 BTC): a buy's `executed_qty` is NOT what landed in the wallet — sell-downs must size from `get_spot_balances` (the venue_wiring sell path already caps at held inventory; selling `executed_qty` raises `BALANCE_NOT_ENOUGH`)
- **A futures account does not exist until the first transfer into it** (`USER_NOT_FOUND: please transfer funds first` — `lib/account_gateio` treats it as equity 0, not a broken key; a fresh binding before any transfer is a working link); an untouched contract has no position record (`POSITION_NOT_FOUND` → `get_leverage` returns 0)
- Errors are `{"label", "message"}` — the machine-readable `label` is surfaced first
- Accepts `GATEIO_*` (web 下單設定) or `GATE_*` (skill convention) key names; no testnet/demo routing (futures testnet needs separate registration — minimal live orders instead)

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
