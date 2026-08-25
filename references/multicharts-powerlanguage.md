# MultiCharts PowerLanguage Export

Applies when the user asks for their strategy as MultiCharts / PowerLanguage /
「MC 訊號」/「MultiCharts 策略」code. Target is **classic MultiCharts (PowerLanguage)**,
the broker-distributed build Taiwan users run — NOT MultiCharts .NET, NOT TradeStation
OOEL. This machine has no MultiCharts: nothing can be compiled here. Every rule below
exists because the only check we have is a static one.

## Export flow — five steps, in order

1. **Logic verified first.** The Python strategy must have a current `stats.json` from a
   run you did or confirmed this session (`references/strategy-code.md`). No backtest → no
   export; run it first. Only Type A exports. Type C (portfolio) and Type B never do.
2. **Adapt a template, never write from scratch.** Pick the closest file in
   `examples/exports/mc/` (see its `README.md`), copy it, change Inputs / indicator lines /
   conditions. Keep the section markers `// --- indicators ---`, `// --- signal ---`,
   `// --- orders ---`. Header comment: what it does, which Blave strategy it came from,
   symbol / interval, the Blave→MC caveats that apply.
3. **Lint until clean:** `python lib/lint_export.py --target mc strategies/<name>/exports/mc.txt`.
   Fix and rerun until it exits 0. Lint output is for you only — NEVER paste lint errors,
   warnings, or "the linter said…" to the user.
4. **Save** to `strategies/<name>/exports/mc.txt` (plain text; the user pastes it into
   PowerLanguage Editor › File › New › Signal).
5. **End the reply with the delivery marker on its own line, nothing after it:**
   `<export target="mc" path="strategies/<name>/exports/mc.txt" />`

   **After the marker, call no tool** — the marker must sit in your final message, not in
   a paragraph followed by `ls`/`cat` verification (the runtime only reads the last
   segment; verify first, then reply). Write the attributes in this order: `target`, then
   `path`. **On Telegram there is no file delivery:** do not emit the marker; state the
   saved path and tell the user the web workspace's 轉出 button delivers a downloadable copy.

Delivery text (user's language, 2–4 sentences, before the marker): generated from a
template, **not compiled here** — open it in PowerLanguage Editor, compile, and run
MC's own backtest before using it; Blave's backtest numbers **will not match** MC's (data,
cost model, sizing differ — see *Data / cost differences*); list what the user must set in
the MC UI (resolution, MaxBarsBack, commission/slippage, trade size).

## Signal script essentials

Structure — always this order: attributes → `Inputs:` → `Variables:` → indicator
assignments → conditions → order statements. Every statement ends with `;`. Case-insensitive.
Comments: `// line` and `{ block }`. Strings use double quotes only.

```
Inputs:    FastLen(20), SlowLen(50), Qty(1);
Variables: FastMA(0), SlowMA(0);
FastMA = Average(Close, FastLen);
SlowMA = Average(Close, SlowLen);
if FastMA > SlowMA and MarketPosition <= 0 then Buy ("LE") Qty contracts next bar at market;
if FastMA < SlowMA and MarketPosition = 1  then Sell ("LX") next bar at market;
```

- **Bar indexing:** `Close[1]` = previous bar (`Close 1 bar ago`). `Close[0]` = current.
  `Highest(High, N)[1]` = highest of the N bars *before* this one. Script runs top-to-bottom
  once per completed bar, oldest bar first.
- **Declarations:** `Inputs: Name(default)` — type from the default (number / "string" /
  true|false); cannot be assigned. `Variables: Name(init)` (aliases `Vars`, `Var`,
  `Variable`). `Value1..Value99`, `Condition1..Condition99` exist without declaration.
- **Conditions:** `if … then <stmt>;` / `if … then begin … end;` / `else`. Operators
  `and or not`, `= <> < > <= >=`. `A crosses over B` / `A crosses under B` (also
  `cross above|below`) are event tests; `A > B` is state.
- **Order grammar (verified):**
  `Buy ["label"] [N contracts] next bar at market;` — enter long; reverses a short.
  `SellShort …` — enter short; reverses a long. `Sell …` — exit long. `BuyToCover …` — exit short.
  Optional `from entry ("label")` on exits ties them to one entry.
  `next bar at market` ≡ `next bar at open`. `next bar at <price> stop` / `… limit` — the
  order lives for the next bar only, cancelled if unfilled.
  `this bar on close` exists but is **backtest-only** semantics — in live trading the bar is
  already closed when the order fires (fills at next open, or is rejected). Use it ONLY for
  Blave `exec_at_close=True` bars (futures settlement) and say so in the header.
- **Execution model, default:** signal at bar close → fill at next bar open. This is exactly
  Blave's `compute_signals` default. Never enable IOG to "match" anything.
- **Position state:** `MarketPosition` = 1 long / -1 short / 0 flat (`MarketPosition(1)` =
  last closed position). `EntryPrice` = fill price of the open position. `BarsSinceEntry` =
  bars since that entry. `CurrentContracts` = absolute size. All signal-only.
- **Built-in exits:** `SetStopLoss(amt)`, `SetProfitTarget(amt)`, `SetDollarTrailing(amt)`,
  `SetPercentTrailing(profit, pct)`, `SetBreakEven(profit)` — **currency amounts**, evaluated
  intra-bar (can exit on the entry bar). Basis defaults to **whole position**
  (`SetStopPosition`); `SetStopContract` / `SetStopShare` switch to per-contract. The last
  `SetStop*` basis statement in ANY signal on the chart wins. `_pt` variants (`SetStopLoss_pt`
  etc.) take ticks. Blave stops are percent-of-price → prefer explicit price orders:
  `Sell ("SL") next bar at EntryPrice * (1 - StopPct/100) stop;`.
- **Same-direction entries while in position** are ignored unless pyramiding is on in
  Strategy Properties — gating with `MarketPosition` keeps intent explicit anyway.
- **Time/date:** `Time` = bar **close** time HHmm in the chart's exchange time (1330 =
  13:30). `Date` = YYYMMDD (1260825 = 2026-08-25). `SessionEndTime`/`SessionStartTime`
  exist but depend on QuoteManager session settings — templates use `Time` compares.

## Blave → PowerLanguage mapping

| Blave (`strategy.py`) | PowerLanguage |
|---|---|
| `df['Close'].rolling(n).mean()` | `Average(Close, n)` (or `AverageFC`) |
| `.ewm(span=n).mean()` | `XAverage(Close, n)` (EL seed differs from pandas — first bars diverge) |
| RSI column (Wilder) | `RSI(Close, n)` |
| `rolling(n).max()` of High / `min()` of Low | `Highest(High, n)` / `Lowest(Low, n)` (`[1]` to exclude the current bar) |
| `rolling(n).std()` | `StdDev(Close, n)` (population; pandas default is sample, ddof=1) |
| ATR column | `AvgTrueRange(n)` |
| `signal = 1.0` while condition true | `if cond and MarketPosition <= 0 then Buy … next bar at market;` |
| `signal = 0.0` | `Sell … next bar at market;` (long) / `BuyToCover …` (short) |
| `signal = -1.0` | `SellShort … next bar at market;` |
| `nan` (hold) | no statement — position persists by itself |
| `exec_at_close=True` bars | `… this bar on close;` (backtest-only, say so) |
| four-threshold long/short loop | `MarketPosition`-gated `if` chain; exits before entries (`threshold_long_short.txt`) |
| `FEE` | not in code — Strategy Properties › Commission / Slippage |
| position fraction (`0.6`), `apply_vol_scaling` | not expressible — fixed `N contracts`; state the drop |
| `WARMUP` | MaxBarsBack (UI), set ≥ longest lookback |
| `txf_settlement_mask` / `settlement_signals_from_db` | no contract-roll data in a single-symbol chart — see *Cannot export* |

`compute_signals` is vectorised; PL is a per-bar loop with state in `MarketPosition` and
`Variables`. Translate the *decision at bar t*, not the pandas idiom. A `.shift(1)` becomes
`[1]`. A rolling window that Blave forward-fills is just the function called every bar.

## PL ≠ EL — trap list

Compiles-and-differs traps are the dangerous ones; the linter only catches identifiers.

- **No OOEL.** Anything with `method`, `using`, `elsystem`, `tsdata`, `Vector`,
  `Dictionary`, `TokenList`, `class`, `new`, `override`, `namespace`, `Print` to
  `elsystem.io`, `.Value`/`.Add()` member calls, `#region` → rewrite. MC rejects them.
- **Not .NET, not Pine.** `Orders.CreateMarket`, `CalcBar()`, `Bars.Close[…]`,
  `strategy.entry`, `ta.sma`, `:=` — wrong language entirely.
- **TS-only words MC never had** (forum list, 2008; still absent from the MC keyword
  categories): `Commentary*`, `AtCommentaryBar`, `CheckCommentary`, `AB_*`,
  `DailyVolume*`, `DailyTrades*`, `High52Wk`/`Low52Wk`, `IVolatility`, `SymbolRoot`,
  `DeliveryMonth`/`DeliveryYear`, `CommodityNumber`, `LeftSide`/`RightSide`,
  `SetPlotType`, `TradeVolume`, `Q_UpVolume`/`Q_DownVolume`, `UnionSess*`.
- **`SetExitOnClose` is backtest-only** in MC (works on the report, may be rejected live).
  Use a `Time >= FlatTime` exit with `next bar at market` a few bars before session end.
- **`this bar on close`** — same trap, see above. Default to `next bar`.
- **`for` loop takes no parentheses:** `for i = 1 to N begin … end;` — `for (i = 1 to N)`
  fails with "numerical variable expected". `repeat … until` needs `end;` before `until`
  if you used `begin`. Prefer no loops at all in a signal.
- **`once begin … end;`** compiles in MC (forum-confirmed, undocumented in the wiki). Avoid
  it; write `if CurrentBar = 1 then` — and note `CurrentBar` counts from MaxBarsBack+1.
- **Series functions need a stable call site.** `Average`, `XAverage`, `RSI`, `Highest` keep
  bar history; calling them inside a conditional branch breaks the series. Assign to a
  variable unconditionally at the top, then branch on the variable.
- **`Plot` is numeric-only in MC** (string plots are TS-only). Signals should not plot.
- **Order names must be unique per statement**; an exit `from entry ("X")` with no matching
  entry name never closes the position.
- **Reverse, not flatten:** a `Buy` while short results in *long N*, not flat; a
  `SellShort` while long → short. Blave `0.0` = flat, so emit an explicit exit for flat.
- **Multiple orders on one bar** execute by MC's priority table (exits/reversals before
  same-direction entries) — with stops and signals on the same bar, results differ from
  Blave's single decision per bar.
- **`Time` is bar-close time; Blave indexes are bar-open UTC.** A 13:25–13:30 TXF bar is
  Blave `05:25Z`, MC `Time = 1330`. Convert every session boundary before typing it.
- **`XAverage` seeding and `StdDev` (population vs pandas sample)** — first-bars and level
  differences are expected; do not "fix" them by hand-rolling the math.
- **Reserved words as names.** `Contracts`, `Shares`, `Total`, `Entry`, `Short`, `Cover`,
  `All`, `Range`, `Text`, `Data`, `Point`, `Day`, `Month` are keywords — an Input named
  `Contracts` fails to compile. Use `Qty`, `Len`, `FastLen`.
- **`{ }` comments do not nest** and end at the first `}`. Never put a brace inside a block
  comment (set notation like `{+1, 0, -1}` in the header kills the compile). Use `( )`.
- `=>` appears in the MC wiki operator table; write `>=` — every MC example does.
- **Comparison of floats with `=`** is exact; use `<=`/`>=` bands.
- **Don't emit `[IntrabarOrderGeneration = true]`** — with IOG on, `next bar` means *next
  tick*, and every `next bar at market` fires per tick while its condition holds.

## Set in MC UI, not in code

Say these in the delivery text; do not try to express them in the script.

- **Resolution / symbol / session** — the chart the signal is applied to. State Blave's
  `SYMBOL` + `INTERVAL` (1h → 60 min, 1d → Daily); the user picks the matching chart.
- **Maximum Bars Back** — Format Signals › Properties. Must be ≥ the longest lookback
  (Blave `WARMUP`); orders start after it. Give the number.
- **Commission / Slippage / Initial Capital** — Strategy Properties › Properties. No
  code keyword sets them (`Commission`/`Slippage` only *read* them).
- **Trade size** — Strategy Properties › Trade size, unless the script says `N contracts`
  (templates expose `Qty` as an Input; code overrides the UI).
- **Pyramiding / position limits** — Strategy Properties; keep off unless the strategy
  intends same-direction adds.
- **Intra-bar order generation** — Format Signal › Calculations. Leave OFF.
- **Backtest engine detail** — Bar Magnifier / tick precision for stop and limit fills is a
  chart-data setting; Blave has no intrabar fills at all (see below).

## Data / cost differences — what the honesty clause rests on

- **Data:** Blave `fetch_kline` = Binance USDT-M perps; TXF/TW data = Blave's own
  minute/daily datasets (UTC, bar-open labels). MC charts come from the broker feed (Taipei
  time, bar-close labels, its own session template and roll handling). Bars, gaps, and
  night-session coverage differ.
- **Fills:** Blave fills every signal at next-bar open (or this-bar close) at the bar's
  recorded price, no intrabar path; stops in Blave are decided at bar close. MC fills
  `stop`/`limit` orders intra-bar using the bar's High/Low (or Bar Magnifier) and evaluates
  `SetStop*` intra-bar, possibly on the entry bar.
- **Costs:** Blave charges `|Δweight| × FEE` per side, a proportional rate that already
  bundles commission + slippage. MC applies Strategy Properties commission (per trade or per
  contract, currency) plus slippage (ticks / currency) — not a percentage. Pick MC settings
  that approximate `FEE`, but the equity curves will still diverge.
- **Sizing:** Blave positions are fractions of equity (compounding, vol-scaled where used);
  MC trades a fixed contract count unless the user configures cash-per-trade. Returns,
  drawdown, and Sharpe are therefore not comparable.
- **Warm-up:** Blave trims `WARMUP` bars; MC's MaxBarsBack is user-set — a mismatch shifts
  the first trade.
- **Rolls:** Blave's futures data is a back-adjusted continuous series with settlement
  exits; the MC chart's continuous contract and roll rule are whatever the broker feed does.

## Cannot export — say so, offer two paths

No marker; explain which parts *would* translate and offer (a) drop the unsupported part
and export the rest, or (b) keep running it on Blave. Triggers:

- Any Blave-only data: alpha/holder concentration, whale hunter, taker intensity,
  liquidation, funding, open interest, screener, institutional / margin / broker flows,
  dividend points, economic calendar, anything from `fetch_*` other than plain OHLCV.
- Cross-market or multi-symbol logic (Type C, pair/basis signals, TXF-vs-spot basis).
- External APIs, web fetches, files, notifications inside the signal logic.
- Position sizing that is the strategy (vol targeting, Kelly, fraction-of-equity ladders):
  export with fixed size only if the user accepts that the sizing is dropped.
- Blave settlement masks (`txf_settlement_mask`, `settlement_signals_from_db`): a
  single-symbol MC chart has no roll calendar. Export the signal without it and state that
  the user must handle roll in MC (continuous contract settings / manual flat), or keep it
  on Blave.

A strategy that mixes plain OHLCV indicators with one unsupported input is the common case:
show the OHLCV part exported, mark the missing input in the header comment, and let the
user choose.

## Sources (verified 2026-08-25)

MC wiki (`multicharts.com/trading-software/index.php?title=…`): Buy, Sell, SellShort,
SetStopLoss, SetStopLoss_pt, SetProfitTarget, SetDollarTrailing, SetPercentTrailing,
SetStopContract, SetExitOnClose, MarketPosition, EntryPrice, BarsSinceEntry,
CurrentContracts, CurrentBar, BarStatus, Time, Inputs, Variables, Switch, Cross, Print,
IntraBarOrderGeneration, MaxBarsBack, How_Scripts_Work, Language_Elements,
Strategy_Properties, Setting_dynamic_order_name, Category:PowerLanguageKeywords and its
sub-categories. MC forum t=3187 (unsupported reserved words), t=10047 (`once`), t=47206
(`for`/`repeat` syntax). Function signatures: TradeStation *EasyLanguage Functions and
Reserved Words Reference* (MC ships the same function library — `AverageFC`, `Highest`,
`Lowest`, `Average` seen in MC's own examples). codereindeer.com MultiCharts tutorials CH02,
CH03, CH06. Items marked `UNVERIFIED` in templates could not be confirmed against MC docs.
