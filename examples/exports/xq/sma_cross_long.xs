// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : SMA crossover, long-only (golden cross -> long, death cross -> flat)
// Blave    : examples/tsmc_ma/ , examples/btc_sma_cross/ (Type A)
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.
// Timeframe / 還原 / 逐筆洗價 / 交易成本 are XQ strategy settings, not code.
// Assumes: run on bar close (逐筆洗價 off), 1 position unit = 1 張 (stock) or 1 口 (futures).

input: FastLen(5);        // Blave SMA_FAST
input: SlowLen(60);       // Blave SMA_SLOW
input: Lots(1);           // position size in 張 / 口

var: fastMA(0), slowMA(0);
var: longEntry(false), longExit(false);

// optional guard: only run on the frequency the Blave backtest used
// if BarFreq <> "D" and BarFreq <> "AD" then return;

// --- indicators ---   (Blave _add_indicators)
fastMA = Average(Close, FastLen);
slowMA = Average(Close, SlowLen);

// --- signal ---       (Blave compute_signals)
// UNVERIFIED: `cross over/under` on declared vars - xshelp shows it only on Value1 / function calls.
//             If XQ rejects it, use CrossOver(Average(Close, FastLen), Average(Close, SlowLen)) inline.
longEntry = fastMA cross over  slowMA;   // (f > s) & (f.shift(1) <= s.shift(1))
longExit  = fastMA cross under slowMA;   // (f < s) & (f.shift(1) >= s.shift(1))

// --- orders ---
// XS executes only the FIRST trading instruction per pass: exits come before entries.
if Position > 0 and Filled > 0 and longExit then
    SetPosition(0, MARKET, label:="SMA death cross exit");

if Position = 0 and Filled = 0 and longEntry then
    SetPosition(Lots, MARKET, label:="SMA golden cross entry");
