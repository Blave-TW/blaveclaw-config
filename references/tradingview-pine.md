# TradingView Pine Script Export (v6)

Applies when the user asks to export / convert a Blave strategy to TradingView, Pine, Pine Script, 轉成 TradingView, 匯出 Pine, "give me the TradingView version". Scope: **Type A** strategies (`strategies/<name>/strategy.py` with `_add_indicators` / `fetch_data` / `compute_signals`, see `strategy-code.md`). Type C (portfolio) and Type B never export — one Pine `strategy()` script is one symbol on one chart.

This machine cannot compile Pine. Every export is a template adaptation plus a static lint, never a compiled artifact — say so at delivery (see step 5).

## Export flow — five steps, in order

1. **Verify the Python first.** The strategy MUST have a current `stats.json` from a backtest of the exact `strategy.py` being exported (run it if missing or stale, per `strategy-code.md`). Never export logic that has not been backtested on Blave. Read `compute_signals` and `_add_indicators` end to end and write down, in words, the entry rule, the exit rule, and the fill timing before touching Pine.
2. **Adapt a template — never write Pine from scratch.** Pick the closest file in `examples/exports/pine/` (see its `README.md`), copy it, and change only inputs, the `// --- signal ---` block and, if needed, the `// --- orders ---` block. Keep the header comment, the `strategy()` line shape, and the section markers. Combine two templates when the strategy needs both (e.g. session filter + trailing stop).
3. **Lint until clean:** `python lib/lint_export.py --target pine strategies/<name>/exports/pine.pine`. Fix every error and re-run; repeat until exit code 0. Lint output is for you — NEVER paste lint errors or "the linter said…" into the reply. If a warning flags a repaint/fill-model item you deliberately kept, mention that item in plain words at delivery.
4. **Save** to `strategies/<name>/exports/pine.pine` (create `exports/`). One file, UTF-8, `//@version=6` on line 1.
5. **Deliver.** Reply body: which template it was adapted from; the honesty clause verbatim in spirit — *generated from a template, not compiled here: add it to a chart, open the Strategy Tester and check the trade list before relying on it; Blave's backtest numbers will not match TradingView's (data source, fill model and cost assumptions differ)*; one line that a TradingView strategy does not trade by itself — execution needs TradingView alerts / a webhook to a broker (mention only, do not design it). Then the delivery marker on its own line, last:

   `<export target="pine" path="strategies/<name>/exports/pine.pine" />`

   **After the marker, call no tool** — the marker must sit in your final message, not in
   a paragraph followed by `ls`/`cat` verification (the runtime only reads the last
   segment; verify first, then reply). Write the attributes in this order: `target`, then
   `path`. **On Telegram there is no file delivery:** do not emit the marker; state the
   saved path and tell the user the web workspace's 轉出 button delivers a downloadable copy.

   No marker when nothing exportable was produced (see *Cannot export*).

## Pine v6 strategy essentials

- Line 1: `//@version=6`. Line 2 (first statement): `strategy(...)`. Nothing else before them except `//` comments. Wrong or missing version line is the #1 failure.
- `strategy()` args that matter — **all are `const`: literals only, never an `input.*` or a variable**:
  - `overlay = true` for price-pane plots (MAs, breakout levels); `false` for oscillators (RSI).
  - `initial_capital = 10000`, `default_qty_type = strategy.percent_of_equity`, `default_qty_value = 100` ⇒ Blave's "full position" (`signal = 1.0`). A fractional constant signal (`0.6`) ⇒ `default_qty_value = 60`. Alternatives: `strategy.fixed` (contracts), `strategy.cash` (currency amount).
  - `commission_type = strategy.commission.percent`, `commission_value = 0.05` ⇒ Blave `FEE = 0.0005` per side (percent, not fraction). Other types: `strategy.commission.cash_per_contract`, `strategy.commission.cash_per_order`.
  - `slippage = N` in **ticks** per fill (default 0). Blave models no slippage; keep 0 unless the user asks.
  - `pyramiding = 1` (default): at most one open entry per position; repeated `strategy.entry` in the same direction is ignored — this is what makes Blave's state-style signals (`1.0` every bar while long) safe. Raise only when the Python really adds to a position.
  - `process_orders_on_close = false` (default): orders created at bar close fill at the **next bar's open** = Blave's default `signal[t] → Open[t+1]`. `true` fills at the same bar's close = Blave `exec_at_close`; prefer `strategy.close(..., immediately = true)` for a single close-on-this-bar exit instead of flipping the global setting.
  - `calc_on_every_tick = false` (default): one execution per bar close. Leave it; `true` recalculates on every realtime tick, repaints, and cannot be reproduced on history.
  - `margin_long` / `margin_short` default 100 (no leverage) since v6.
- Inputs: `input.int(defval, "Title", minval = , maxval = , step = )`, `input.float`, `input.bool`, `input.string(defval, "Title", options = [...])`, `input.session("0900-1330", "Session")`, `input.timeframe`. `defval` and bounds are `const` literals. Declare with a type: `int fastLen = input.int(45, "Fast SMA", minval = 1)`.
- Indicators: `ta.sma(src, len)`, `ta.ema`, `ta.rsi`, `ta.atr(len)`, `ta.highest(src, len)` / `ta.lowest`, `ta.stdev`, `ta.change`, `ta.crossover(a, b)` / `ta.crossunder`, `[macdLine, signalLine, hist] = ta.macd(close, 12, 26, 9)`, `[mid, upper, lower] = ta.bb(close, 20, 2)`, `[diPlus, diMinus, adx] = ta.dmi(14, 14)` (there is no `ta.adx`). Length of `ta.ema`, `ta.rsi`, `ta.atr`, `ta.macd` is `simple int` — an input is fine, a series (e.g. a computed length) is a compile error; `ta.sma` / `ta.highest` / `ta.lowest` accept series lengths.
- History: `x[1]` = previous bar's value (Python `shift(1)`). `ta.highest(high, n)[1]` = prior-n-bar high excluding the current bar = `High.rolling(n).max().shift(1)`.
- Orders:
  - `strategy.entry("L", strategy.long)` / `strategy.entry("S", strategy.short)` — market order, id string, reverses an open opposite position automatically (flip long→short in one call). Optional `qty =`, `limit =`, `stop =` (absolute prices; both together = stop-limit).
  - `strategy.close("L")` — market close of that entry id; `strategy.close_all()`; `immediately = true` fills at this bar's close.
  - `strategy.exit("x", from_entry = "L", ...)` — attaches exit orders to an entry. **Units:** `stop` / `limit` / `trail_price` are **absolute prices**; `profit` / `loss` / `trail_points` / `trail_offset` are **ticks** (`price distance / syminfo.mintick`), never percent, never points. `stop` + `limit` in one call = two orders (bracket), unlike in `strategy.entry`. Needs `trail_offset` plus `trail_price` or `trail_points` for a trailing stop. Re-issue every bar while in position with the same id to keep it anchored to `strategy.position_avg_price`.
  - `when =` no longer exists (removed in v6): wrap the call in `if cond`.
- State: `strategy.position_size` (>0 long, <0 short, 0 flat), `strategy.position_avg_price` (na when flat), `strategy.opentrades`, `strategy.closedtrades`, `strategy.equity`.
- `barstate.isconfirmed` / `barstate.islast` / `barstate.isrealtime`: not needed under the default once-per-bar-close model; use only for display logic, never to gate orders.
- Syntax: 4-space indented blocks under `if` / `for`; `:=` reassigns, `=` declares; `var` initialises once (Python state variable across bars); `and` / `or` / `not`; ternary `c ? a : b`; `na` for missing, `nz(x)` → 0, `na(x)` test. Wrapped function arguments inside `(...)` may use any indentation; wrapped expressions outside parentheses must not be indented by a multiple of 4. Comments are `//` only. Strings use `"` or `'`.

## Blave → Pine mapping

| Blave (`strategy.py`) | Pine |
|---|---|
| `SYMBOL`, `INTERVAL`, `START` / `END` | Not in code — the chart's symbol and timeframe, and the visible history (see *TV UI*). Put them in the header comment. |
| `FEE = 0.0005` | `commission_type = strategy.commission.percent, commission_value = 0.05` |
| `WARMUP` | Implicit: `ta.*` return `na` until enough bars; `na` comparisons are `false`, so no orders fire. Nothing to write. |
| `_add_indicators`: `df['Close'].rolling(n).mean()` / `.ewm(span=n).mean()` / `rolling(n).max()` / `rolling(n).std()` / `.diff(n)` / `.pct_change(n)` | `ta.sma(close, n)` / `ta.ema(close, n)` / `ta.highest(close, n)` / `ta.stdev(close, n)` / `ta.change(close, n)` / `ta.roc(close, n) / 100` |
| `df['Open'] High Low Close Volume` | `open high low close volume` |
| `x.shift(k)` | `x[k]` |
| `compute_signals`: `signal[cond] = 1.0` (state, held) | `if cond and strategy.position_size == 0 → strategy.entry("L", strategy.long)`; the `1.0` while already long is a no-op under `pyramiding = 1` |
| `signal[cond] = 0.0` | `if cond and strategy.position_size > 0 → strategy.close("L")` (or `!= 0` + `strategy.close_all()` for both sides) |
| `signal[cond] = -1.0` | `strategy.entry("S", strategy.short)` (reverses a long automatically) |
| `nan` (hold) | no branch fires — do nothing |
| Four-threshold long/short with flat band (`strategy-code.md`) | four `if` branches: entry long / close long / entry short / close short, each gated on `strategy.position_size` |
| `(k > d) & (k.shift(1) <= d.shift(1))` (event) | `ta.crossover(k, d)` — only when the Python is written as an event; a level comparison (`SMA_F > SMA_S`) stays a level comparison, never `ta.crossover` (it would miss a trend already in progress at chart start) |
| `apply_vol_scaling`, fractional / varying size | `qty =` on `strategy.entry` computed from `strategy.equity` — only if the formula is simple; otherwise state it as a difference and keep 100 % |
| `txf_settlement_mask` / `exec_at_close` | last-bar-of-session detection + `strategy.close("L", immediately = true)` (template `session_filter`); contract roll itself has no equivalent — TradingView continuous contracts (`TXF1!`) handle it in the data |
| Next-bar-open fill (default) | default `strategy()` — identical semantics |

Long-only → `strategy.long` only; long/short → both ids; flat → `strategy.close`. Always `overlay = true` when plotting price-pane indicators.

## Version / trap list — check every one before lint

- `//@version=6` missing, or `=5` / `=4`: fails, or compiles into a different language. First line, exactly.
- v4 leftovers that do not exist in v6: `study()` → `strategy()`; bare `sma() ema() rsi() atr() highest() lowest() crossover() crossunder() change() cum() valuewhen() barssince() stoch() macd() tr vwap` → `ta.*`; `security()` → `request.security()`; `input(x, type = input.integer)` / `input.resolution` → `input.int` / `input.timeframe`; `tostring` `tonumber` `iff` → `str.tostring` `str.tonumber` / ternary; `abs round floor ceil max min sqrt pow log` → `math.*`; `color.new(c, transp)` yes, `transp =` argument no; `tickerid` → `syminfo.tickerid`.
- v5 → v6 breaks: `when =` on `strategy.entry/order/exit/close/cancel` removed; `int`/`float` no longer auto-cast to `bool` (`if bar_index` → `if bar_index != 0`); a `bool` cannot be `na` and `na()`/`nz()` reject bools; `strategy.opentrades.max_drawdown_percent` and `syminfo.country` removed; passing the same argument twice fails; `timeframe.period` now reads `"1D"` not `"D"`.
- Type system: `strategy()` and `input.*` args are `const` — no variables, no inputs, no `syminfo.mintick`; `ta.ema/rsi/atr/macd` length is `simple int`; `plot()` `offset` is not series. Declaring `float x = na` needs the type; reassigning uses `:=`; a variable declared inside an `if` block does not exist outside it.
- One order per bar per id: two `strategy.entry` calls with the same id on one bar → the last wins; entry and close of the same id on one bar → both queue, fills next bar. Keep branches mutually exclusive.
- Repaint / lookahead: never `request.security(..., lookahead = barmerge.lookahead_on)` without `[1]` on the expression; never `calc_on_every_tick = true`; never `varip` / `timenow` / `barstate.isrealtime` in order logic; never gate orders on `high`/`low` of the current bar as if known at open (Blave signals use bar close values only — keep it that way).
- `ta.crossover` on the first bar the series exists returns `false`; a state comparison on a chart that starts mid-trend enters at once — mirror what the Python does (see mapping).
- `strategy.exit` units: percent or price passed to `profit`/`loss`/`trail_*` is silently treated as ticks → convert (`math.round(avg * pct / 100 / syminfo.mintick)`), or use `stop`/`limit` as absolute prices.
- `strategy.exit` placed on the signal bar with `close`-based levels is active on the fill bar; placed from `strategy.position_avg_price` it is exact but starts one bar after the fill — templates do both.
- Session strings are in the **chart symbol's exchange timezone** (`time(timeframe.period, session)`); Blave TW data is Asia/Taipei, crypto is UTC. State the assumption in the header.
- Wrapped lines outside parentheses indented by 4/8 spaces become a block → syntax error. Keep calls on one line or wrap inside the parentheses.

## Lives in the TradingView UI, not in code

Symbol, exchange and timeframe (chart header); history depth (plan-dependent — Blave's `START` may reach further back than the chart does); every `strategy()` property can be overridden in *Strategy Tester → Settings → Properties* (capital, order size, commission, slippage, pyramiding, "Order execution delay", "Script execution" checkboxes); inputs in *Settings → Inputs*. The script's constants are defaults, not guarantees — say this at delivery when the user asks why results differ.

## Data and cost differences vs Blave

Blave backtests a fixed dataset (Binance USDT-M / TAIFEX / TWSE via `lib/data.py`), fills at next-bar open with a per-side fraction fee and no slippage, and marks PnL from `lib/analysis.py`. TradingView uses the chart feed of whichever exchange/symbol the user opened (spot vs perp, continuous-contract stitching, volume definitions all differ), fills with its broker emulator's intrabar OHLC path assumption, applies commission/slippage from the properties tab, and computes its own stats. Stop/limit fills on historical bars assume no intrabar gaps. Expect different trade counts and PnL; matching them is not a goal. If the user wants closer agreement: same exchange symbol, same timeframe, `slippage = 0`, `commission_value` = Blave `FEE × 100`, and compare the trade list dates, not the equity curve.

## Cannot export — the only legitimate refusal

No marker and no `pine.pine` when the strategy needs something the platform cannot express: Blave-only data (`fetch_taker_intensity`, liquidation, holder / on-chain, broker or institutional flows, `fetch_db_kline` settlement tables, any `lib.data` call other than plain kline); cross-market or multi-symbol logic (Type C, spread / pair signals, `request.security` on a second symbol is possible but out of scope for this flow); external APIs or files; execution-time state that Pine cannot hold (orders sized from account balance on another venue). Reply must (1) name which parts translate cleanly and which do not, (2) offer two paths — drop the unsupported part and export the rest as a simplified script, or keep the strategy running on Blave — and (3) stop there; never ship a script that silently approximates the missing data.
