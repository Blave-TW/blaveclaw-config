# XQ (嘉實 XQ 全球贏家) — Exporting a Strategy as an XS Trading Script

Use this file when the user asks to export / convert / 匯出 / 轉成 a Blave strategy for
XQ, XS, 嘉實, or 全球贏家. It covers Type A strategies only. Type C (portfolio) and
Type B never export — see *Cannot export* below.

This machine has NO XQ installed. Nothing here can be compiled or backtested. Every
export is a template adaptation plus a static lint; the user compiles it in XQ.

## Export flow (all five steps, in order)

1. **Confirm the Python version works.** `strategies/<name>/stats.json` must exist and
   match the current `strategy.py`; if not, run the backtest first (normal rules apply).
   Never translate logic that has not been backtested on Blave.
2. **Translate by adapting a template** from `examples/exports/xq/` (see its README for
   the list). Pick the closest skeleton, then change indicators / thresholds / inputs.
   NEVER write XS from scratch, NEVER write EasyLanguage-style `Buy`/`Sell` statements.
3. **Lint:** `python lib/lint_export.py --target xq strategies/<name>/exports/xq.xs`.
   Fix and re-run until it passes. Lint output is for you — NEVER paste lint errors into
   the reply.
4. **Save** to `strategies/<name>/exports/xq.xs` (create `exports/`).
5. **End the reply with the delivery marker on its own line, nothing after it:**

   `<export target="xq" path="strategies/<name>/exports/xq.xs" />`

   **After the marker, call no tool** — the marker must sit in your final message, not in
   a paragraph followed by `ls`/`cat` verification (the runtime only reads the last
   segment; verify first, then reply). Write the attributes in this order: `target`, then
   `path`. **On Telegram there is no file delivery:** do not emit the marker; state the
   saved path and tell the user the web workspace's 轉出 button delivers a downloadable copy.

   The runtime strips the marker and pushes the file to the web chat. No marker = no
   file delivered. Emit it only when step 3 passed and the whole strategy is expressed.

**Honesty clause — say this at delivery, every time:** the script was generated from a
template and could not be compiled here; compile it in the XS editor and backtest it in
XQ's 自動交易中心 before trading; XQ's backtest numbers WILL differ from Blave's (data
source, dividend adjustment, fill and cost assumptions differ — see *Data and cost
differences*). Also tell the user which XQ settings the script assumes (frequency,
還原 or not, 逐筆洗價).

## XS essentials

Verified against https://xshelp.xq.com.tw/XSHelp/ (2026-08-25) unless marked UNVERIFIED.

- **Script kinds** (chosen when creating a script in the XS editor): 指標 (indicator),
  選股 (screener), 警示 (alert), 交易 (trading), 函數 (function). Exports are always
  **交易 scripts** (自動交易腳本) — the only kind where `SetPosition` / `Position` /
  `Filled` are meaningful. Not an indicator; no `Plot`.
- **Case-insensitive.** `SetPosition` = `setposition`. Statement terminator is `;`.
- **Comments:** `// line` and `{ block }`. Nothing else.
- **Strings:** double quotes only, no escapes: `"MA cross"`.
- **Declarations** (top of file, one per statement, semicolon-terminated):
  - `input: Name(default);` — one input per `input:` statement. Optional display name:
    `input: Len(10, "天期");`. Numeric, string, or true/false, inferred from the default.
  - `var: a(0), b(false), s("");` — `var` / `vars` / `variable` / `variables` are aliases.
    Initial value sets the type. Declared vars carry their value forward bar to bar.
  - `variable: intrabarpersist x(0);` — keeps its value across tick re-executions of the
    same bar (needed for running maxima, counters). Plain vars are reset to the previous
    bar's final value each time the bar is re-executed under 逐筆洗價.
  - Built-in series scratch vars: `Value1..Value999` (numeric), `Condition1..` (boolean).
- **Operators:** `=` is BOTH assignment and equality; `<>` is not-equal; `and or not`;
  `+ - * /`. No `==`, no `!=`, no `&&`. `:=` exists ONLY in `label:="..."`.
- **Bar indexing:** `Close[1]` = previous bar, `Close[0]` = this bar. `Position[1]`,
  `Filled[1]`, `Value1[1]` are documented series. Fields: `Open High Low Close Volume
  Date Time` (also `O H L C V`). `Date` = yyyymmdd integer; `Time` = hhmmss integer,
  **0 on daily and slower bars**. Whether intraday `Time` is bar start or bar end is
  UNVERIFIED — keep cut-off times conservative.
- **Cross:** `a cross over b` / `cross above` (a ≥ b now and a < b on the previous bar);
  `a cross under b` / `cross below`. Function forms `CrossOver(a,b)` / `CrossUnder(a,b)`.
  Note the `≥` on the current bar: Blave's `(f > s) & (f.shift(1) <= s.shift(1))` differs
  on exact ties only.
- **Control flow:** `if c then stmt;` / `if c then begin ... end;` /
  `if c then begin ... end else begin ... end;` — NO semicolon before `else`, one after the
  final `end`. `else if` chains are allowed. `once(c) begin ... end;` runs one time only.
  `return;` leaves the script for this bar. `for i = 1 to n begin ... end;`, `while`.
- **Indicator functions** (all take a series first, length second, unless noted):
  `Average(Close, n)` SMA · `XAverage(Close, n)` EMA · `RSI(Close, n)` · `ATR(n)` ·
  `Highest(High, n)` / `Lowest(Low, n)` (window INCLUDES the current bar) ·
  `BarsLast(cond)` bars since cond was last true (0 = this bar) · `TrueAll(cond, n)` ·
  `StandardDev`, `BollingerBand`, `MACD`, `Stochastic`, `SAR`, `Bias` exist — argument
  order UNVERIFIED, check xshelp before using.
- **Frequency / period live in XQ's UI, not in code.** The strategy's 執行頻率 (K棒週期),
  還原 vs raw price series, 逐筆洗價, initial position, account, and 交易成本 are all set
  in 自動交易中心 when the user attaches the script. `BarFreq` returns `"Min"`, `"D"`,
  `"AD"` (還原日線), `"W"`, ... — use it only as an optional guard
  (`if BarFreq <> "D" and BarFreq <> "AD" then return;`).
- **Execution model:** the script runs once per bar close, or on every price tick when
  逐筆洗價 is on (same bar re-executed; `Position`/`Filled` refreshed before each run).
  Nothing in the file chooses this — tell the user which mode the logic assumes
  (Blave Type A = bar close).

## Order API — Python signal → XS

`Position` = the strategy's target position (an integer; + long, − short, 0 flat).
`Filled` = what has actually filled. Unit = 1 張 for a stock account, 1 口 for a futures
account. `SetPosition(target)` moves the target; the system works out the order.

| Blave / Python | XS (use this) | EasyLanguage (NEVER — does not exist or means something else in XS) |
|---|---|---|
| signal → 1 (enter long) | `SetPosition(Lots, MARKET);` | `Buy next bar at market;` |
| signal → 0 from long (exit) | `SetPosition(0, MARKET);` | `Sell next bar at market;` |
| signal → −1 (enter short) | `SetPosition(-1 * Lots, MARKET);` | `SellShort` / `Sell short next bar` |
| signal → 0 from short (cover) | `SetPosition(0, MARKET);` | `BuyToCover` |
| flip long → short in one pass | `SetPosition(-1 * Lots, MARKET);` | — |
| current position sign | `Position` (target) / `Filled` (actual) | `MarketPosition` |
| position size | `Position` / `Filled` (signed integer) | `CurrentContracts` |
| entry price | `FilledAvgPrice` (FIFO cost, unsigned, 0 when flat) | `EntryPrice` / `AvgEntryPrice` |
| bars since entry | `BarsLast(Position[1] = 0 and Position <> 0)` — UNVERIFIED expression, or track a counter with `intrabarpersist` | `BarsSinceEntry` |
| fixed % stop-loss (long) | `if Filled > 0 and Close <= FilledAvgPrice * (1 - StopPct/100) then SetPosition(0, MARKET);` | `SetStopLoss` / `SetPercentTrailing` |
| take-profit (long) | `if Filled > 0 and Close >= FilledAvgPrice * (1 + TpPct/100) then SetPosition(0, MARKET);` | `SetProfitTarget` |
| limit price | `SetPosition(1, Close)`, `SetPosition(1, AddSpread(Close, 2))` (+2 ticks) | `... limit` |
| market order | `MARKET` as the price argument (`SetPosition(1, MARKET)`) | `at market` |
| order tag | `SetPosition(1, MARKET, label:="MA cross");` — labels must be unique in the file | `Buy("name")` |
| add to position | `SetPosition(Position + 1)` or `Buy(1)` (Buy/Sell/Short/Cover are position-DELTA functions taking a quantity) | `Buy` as a statement |

Rules that shape every script:

- **Only the FIRST trading instruction per pass executes; later ones are ignored.** Order
  the blocks: risk exits (stop / take-profit / time-out) → signal exits → entries.
- **`Position` and `Filled` do not change inside a pass.** Calling `SetPosition(1)` does not
  make `Position` 1 on the next line; it updates after the pass. So a flat→long→exit
  sequence cannot happen in one bar, and a long→short flip needs one direct
  `SetPosition(-Lots)`, not `SetPosition(0)` followed by `SetPosition(-Lots)`.
- **Guard entries with both:** `Position = 0 and Filled = 0`. Guard exits with
  `Position > 0 and Filled > 0` (or `Filled = Position`). Without the `Filled` guard the
  script re-sends / re-prices while an order is still working.
- `SetPosition` truncates non-integers (1.5 → 1). Never pass a fraction.
- Repeated `SetPosition(1, price)` with a different price cancels and re-prices the
  working order — this is the documented chase idiom, not a bug.

## Blave → XS concept mapping

| Blave (`strategy.py`) | XS |
|---|---|
| `SYMBOL`, `INTERVAL`, `START`, `FEE` | not in code — chosen in 自動交易中心 (商品, 執行頻率, 回測區間, 單邊交易成本) |
| `SMA_FAST = 5` module constants | `input: FastLen(5);` |
| `WARMUP` | nothing — XQ reads 資料讀取筆數 from settings; tell the user to set it ≥ the longest lookback |
| `_add_indicators`: `df['Close'].rolling(n).mean()` | `Average(Close, n)` |
| `df['Close'].ewm(span=n).mean()` | `XAverage(Close, n)` |
| `RSI` column (Wilder) | `RSI(Close, n)` — smoothing method UNVERIFIED; expect small differences |
| `df['High'].rolling(n).max().shift(1)` | `Value1 = Highest(High, n); ... Value1[1]` |
| `x.shift(1)` | `x[1]` |
| `compute_signals`: golden cross mask | `fastMA cross over slowMA` |
| `signal = 1.0 / 0.0 / -1.0` then ffill | `SetPosition(Lots / 0 / -Lots)` — `Position` IS the ffilled state |
| stateful four-threshold loop (`pos` variable) | `Position` replaces `pos`; each `if pos == … and …` branch becomes one `if Position … then SetPosition(…)` block, exits first |
| `apply_vol_scaling` / fractional sizing | not expressible — drop it (fixed `Lots`) and say so |
| `txf_settlement_mask` (flat on settlement day) | UNVERIFIED: XQ has `q_ExpiredDate` / `DaysToExpiration` fields; do not attempt unless the user insists, and mark the line UNVERIFIED |
| bar-close execution (Type A) | XQ default = run on bar close; 逐筆洗價 unchecked. Say so. |
| Blave `FEE` | XQ 單邊交易成本(%) — the user enters it; suggest the Blave value |

## Known traps

1. **`Buy` / `Sell` / `Short` / `Cover` are functions with a quantity, not EL order
   statements.** `Buy next bar at market;` will not compile; `Buy(1);` compiles but is a
   position DELTA (`Sell(1)` reduces a long, never opens a short). Templates use
   `SetPosition` only.
2. **First instruction wins** (see above). Putting entries before stops silently disables
   the stops on any bar where both fire.
3. **Full-width punctuation** (`，` `；` `（）` `：`) anywhere outside a string or comment is a
   compile error. Chinese is fine inside `"..."` and comments.
4. **Semicolons:** every statement and every `end` needs one, except no `;` between `end`
   and `else`. `input:`/`var:` lines need one too.
5. `=` compares AND assigns; `==` / `!=` / `&&` / `||` do not exist.
6. `Time` is 0 on daily bars — a time filter on a daily script silently never matches.
7. `FilledAvgPrice` is unsigned and 0 when flat; multiply by the direction yourself and
   always guard with `Filled > 0` / `Filled < 0`.
8. Plain `var` values are rolled back on every tick re-execution under 逐筆洗價; running
   maxima / counters need `intrabarpersist`.
9. `Highest`/`Lowest` include the current bar; a breakout on `Close > Highest(High, n)`
   never fires. Use the `[1]`-shifted value.
10. Reserved words include harmless-looking English: `A An At Based By Does From Is Of On
    Place Than The Was` are skip-words the parser drops; `Value`, `Condition`, `Label`,
    `Ret`, `Over`, `Under`, `Above`, `Below` are keywords. Never name a var `is`, `on`,
    `value`, `label`, `over`.
11. No `Plot` in a trading script; no `Alert`.
12. Stock orders are limited to 499 張 per order — a `SetPosition` jump larger than that
    raises a runtime error and halts the symbol.
13. `SetPosition(target)` with no price uses the strategy's default buy/sell price from the
    UI. Always pass `MARKET` or an explicit price so behaviour does not depend on a hidden
    setting.

## Data and cost differences vs Blave (for the honesty clause)

- **Taiwan stocks — adjustment.** Blave `fetch_twstock_price_adj` is dividend/rights
  adjusted. XQ uses raw prices on 日線 and adjusted prices on 還原日線 (`BarFreq = "AD"`);
  its 自動交易 backtest reinvests dividends for stocks and removes roll gaps for futures.
  Tell the user to pick 還原 series to get closest to Blave; results still differ.
- **Costs.** Blave `FEE` is a per-trade rate applied on every position change (e.g. 0.003
  for TWSE). XQ takes 單邊交易成本(%) from the strategy settings (commonly 0.2 % for stocks,
  commission + tax combined); day-trade tax reductions are not applied in backtest.
- **Fills.** Blave fills at the bar close of the signal bar. XQ's backtest fills market
  orders at the next simulated tick unless 觸發即判斷成交 is checked; no capital check.
- **Data.** Different vendors, different session handling (night session for futures),
  different history depth. Crypto and US data: XQ coverage depends on the user's modules —
  do not assume a crypto symbol exists in XQ.
- Therefore: Blave stats are the design evidence; XQ's backtest is the acceptance test.
  Never quote Blave's Sharpe / return as what XQ will show.

## Cannot export — no marker, explain instead

Do not emit the marker when the strategy needs any of:

- Blave-only data: liquidation, holder concentration, whale, taker intensity, funding rate,
  alpha/screener scores, 籌碼 z-scores from `lib/data.py`, economic calendar.
- Anything fetched from an external API or the web at run time.
- Cross-symbol / cross-market logic (Type C portfolios, pairs, TXF vs spot basis).
- Fractional or volatility-scaled sizing that the user refuses to replace with fixed lots.
- Type B strategies (no backtest to verify against).

Say plainly which parts would translate and which cannot, then offer exactly two paths:
(a) export a reduced version without the unsupported part (state what changes), or
(b) keep running it on Blave. Do not invent an XS field that "probably" carries the data.
