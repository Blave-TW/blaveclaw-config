// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : N-bar high/low breakout (Donchian), long/short, flip on the opposite break
// Blave    : Type A with df['High'].rolling(N).max().shift(1) / df['Low'].rolling(N).min().shift(1)
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.
// Signals read the COMPLETED bar ([1]): XQ's daily backtest runs intrabar, so current-bar values fire a day early.

input: Lookback(20);
input: Lots(1);

var: brkUp(false), brkDn(false);

// --- indicators ---
// Highest/Lowest INCLUDE the bar they are computed on. The signal bar is the completed bar [1],
// so its prior channel is Value1[2] / Value2[2]: the N bars ending just before the signal bar.
Value1 = Highest(High, Lookback);   // channel top   (incl. current bar)
Value2 = Lowest(Low, Lookback);     // channel bottom(incl. current bar)

// --- signal ---
brkUp = Close[1] > Value1[2];       // completed close above the N-bar high before it
brkDn = Close[1] < Value2[2];       // completed close below the N-bar low before it

// --- orders ---   (first instruction wins: flips/exits before fresh entries)
if Position > 0 and Filled = Position and brkDn then
    SetPosition(-1 * Lots, MARKET, label:="flip to short on low break");

if Position < 0 and Filled = Position and brkUp then
    SetPosition(Lots, MARKET, label:="flip to long on high break");

if Position = 0 and Filled = 0 and brkUp then
    SetPosition(Lots, MARKET, label:="long breakout");

if Position = 0 and Filled = 0 and brkDn then
    SetPosition(-1 * Lots, MARKET, label:="short breakdown");
