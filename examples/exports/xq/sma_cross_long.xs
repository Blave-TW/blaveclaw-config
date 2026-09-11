// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : SMA crossover, long-only (golden cross -> long, death cross -> flat)
// Blave    : examples/tsmc_ma/ , examples/btc_sma_cross/ (Type A)
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.
// Timeframe / 還原 / 逐筆洗價 / 交易成本 are XQ strategy settings, not code.
// Signals read the COMPLETED bar ([1]): XQ's daily backtest runs intrabar (模擬逐筆洗價 forced on),
// so the order goes out on the next bar's first tick = Blave's decide-at-close, fill-next-open.
// Daily 執行頻率 needs 自動洗價 checked to save. 1 position unit = 1 張 (stock) or 1 口 (futures).

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
longEntry = fastMA[1] cross over  slowMA[1];   // (f > s) & (f.shift(1) <= s.shift(1)) on the completed bar
longExit  = fastMA[1] cross under slowMA[1];   // (f < s) & (f.shift(1) >= s.shift(1)) on the completed bar

// --- orders ---
// XS executes only the FIRST trading instruction per pass: exits come before entries.
if Position > 0 and Filled > 0 and longExit then
    SetPosition(0, MARKET, label:="SMA death cross exit");

if Position = 0 and Filled = 0 and longEntry then
    SetPosition(Lots, MARKET, label:="SMA golden cross entry");
