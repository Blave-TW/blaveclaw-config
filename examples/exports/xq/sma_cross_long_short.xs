// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : SMA crossover, long/short, always in the market (flip on each cross)
// Blave    : examples/btc_sma_cross/ with signal -1 on death cross (Type A)
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.
// Timeframe / 還原 / 逐筆洗價 / 交易成本 are XQ strategy settings, not code.
// Shorting stocks needs a 信用交易 account in XQ; futures short freely.

input: FastLen(20);
input: SlowLen(50);
input: Lots(1);

var: fastMA(0), slowMA(0);
var: goLong(false), goShort(false);

// --- indicators ---
fastMA = Average(Close, FastLen);
slowMA = Average(Close, SlowLen);

// --- signal ---
// UNVERIFIED: `cross over/under` on declared vars - xshelp shows it only on Value1 / function calls.
//             If XQ rejects it, use CrossOver(Average(Close, FastLen), Average(Close, SlowLen)) inline.
goLong  = fastMA cross over  slowMA;
goShort = fastMA cross under slowMA;

// --- orders ---
// Position/Filled do not change inside a pass, and only the first SetPosition runs,
// so a flip must be ONE direct SetPosition to the opposite side (never 0 then -Lots).
// Filled = Position guards against acting while a previous order is still working.
if Position > 0 and Filled = Position and goShort then
    SetPosition(-1 * Lots, MARKET, label:="flip long to short");

if Position < 0 and Filled = Position and goLong then
    SetPosition(Lots, MARKET, label:="flip short to long");

if Position = 0 and Filled = 0 and goLong then
    SetPosition(Lots, MARKET, label:="enter long");

if Position = 0 and Filled = 0 and goShort then
    SetPosition(-1 * Lots, MARKET, label:="enter short");
