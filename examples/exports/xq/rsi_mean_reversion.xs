// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : RSI mean reversion, long-only (buy when RSI turns up from oversold, exit at mid level)
// Blave    : Type A with an RSI column in _add_indicators
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.
// RSI smoothing in XS (Wilder vs simple) is not documented - expect small differences vs pandas.

input: RsiLen(14);
input: OverSold(30);
input: ExitLevel(55);
input: Lots(1);

var: rsiVal(0);
var: longEntry(false), longExit(false);

// --- indicators ---
rsiVal = RSI(Close, RsiLen);

// --- signal ---
// UNVERIFIED: `cross over/under` on declared vars - xshelp shows it only on Value1 / function calls.
//             If XQ rejects it, use CrossOver(Average(Close, FastLen), Average(Close, SlowLen)) inline.
longEntry = rsiVal cross over OverSold;   // was below, now at/above the oversold line
longExit  = rsiVal >= ExitLevel;

// --- orders ---   (exit before entry)
if Position > 0 and Filled > 0 and longExit then
    SetPosition(0, MARKET, label:="RSI exit");

if Position = 0 and Filled = 0 and longEntry then
    SetPosition(Lots, MARKET, label:="RSI oversold entry");
