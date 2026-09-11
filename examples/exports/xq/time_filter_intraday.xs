// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : intraday SMA cross, trades only inside a time-of-day window, flat before the close
// Blave    : examples/txf_ma_1m/ (Type A, 1m). Its txf_settlement_mask and vol scaling are
//            NOT reproduced here - tell the user both were dropped.
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.
// Time is a hhmmss integer on intraday bars and 0 on daily bars (this script needs an intraday 頻率).
// Whether Time marks the bar's start or end is not documented: keep FlatTime a few bars early.
// TXF day session: 08:45-13:45. Adjust the window for the user's instrument.
// UNVERIFIED: Min-frequency backtest behaviour is untested in XQ. If XQ also forces 模擬逐筆洗價 on
//             minute bars, shift the crossover to fastMA[1] / slowMA[1] as the daily templates do.

input: FastLen(60);
input: SlowLen(300);
input: StartTime(90000);   // hhmmss, first bar allowed to enter
input: EndTime(130000);    // hhmmss, last bar allowed to enter
input: FlatTime(133000);   // hhmmss, force flat at/after this time
input: Lots(1);

var: fastMA(0), slowMA(0);
var: inWindow(false), mustFlat(false);
var: longEntry(false), longExit(false);

// guard: intraday bars only
if BarFreq <> "Min" then return;

// --- indicators ---
fastMA = Average(Close, FastLen);
slowMA = Average(Close, SlowLen);

// --- signal ---
inWindow  = Time >= StartTime and Time <= EndTime;
mustFlat  = Time >= FlatTime or IsSessionLastBar;
longEntry = inWindow and fastMA cross over slowMA;
longExit  = fastMA cross under slowMA;

// --- orders ---   (time-out exit first, then signal exit, then entry)
if Position <> 0 and Filled = Position and mustFlat then
    SetPosition(0, MARKET, label:="flat before close");

if Position > 0 and Filled > 0 and longExit then
    SetPosition(0, MARKET, label:="SMA exit");

if Position = 0 and Filled = 0 and longEntry then
    SetPosition(Lots, MARKET, label:="SMA entry in window");
