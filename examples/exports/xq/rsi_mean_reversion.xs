// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : RSI mean reversion, long-only (buy when RSI turns up from oversold, exit at mid level)
// Blave    : Type A with an RSI column in _add_indicators
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.
// XS RSI is consistent with Wilder smoothing (7-trade check on 2330 daily vs a Wilder replica).
// Signals read the COMPLETED bar ([1]): XQ's daily backtest runs intrabar, so current-bar values fire a day early.

input: RsiLen(14);
input: OverSold(30);
input: ExitLevel(55);
input: Lots(1);

var: rsiVal(0);
var: longEntry(false), longExit(false);

// --- indicators ---
rsiVal = RSI(Close, RsiLen);

// --- signal ---
longEntry = rsiVal[1] cross over OverSold;   // completed bar: was below, now at/above the oversold line
longExit  = rsiVal[1] >= ExitLevel;

// --- orders ---   (exit before entry)
if Position > 0 and Filled > 0 and longExit then
    SetPosition(0, MARKET, label:="RSI exit");

if Position = 0 and Filled = 0 and longEntry then
    SetPosition(Lots, MARKET, label:="RSI oversold entry");
