// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : N-bar high/low breakout (Donchian), long/short, flip on the opposite break
// Blave    : Type A with df['High'].rolling(N).max().shift(1) / df['Low'].rolling(N).min().shift(1)
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.

input: Lookback(20);
input: Lots(1);

var: brkUp(false), brkDn(false);

// --- indicators ---
// Highest/Lowest INCLUDE the current bar, so shift by one bar to get the prior channel.
// Value1/Value2 are documented built-in series, hence Value1[1] is the previous bar's value.
Value1 = Highest(High, Lookback);   // channel top   (incl. current bar)
Value2 = Lowest(Low, Lookback);     // channel bottom(incl. current bar)

// --- signal ---
brkUp = Close > Value1[1];          // close above the prior N-bar high
brkDn = Close < Value2[1];          // close below the prior N-bar low

// --- orders ---   (first instruction wins: flips/exits before fresh entries)
if Position > 0 and Filled = Position and brkDn then
    SetPosition(-1 * Lots, MARKET, label:="flip to short on low break");

if Position < 0 and Filled = Position and brkUp then
    SetPosition(Lots, MARKET, label:="flip to long on high break");

if Position = 0 and Filled = 0 and brkUp then
    SetPosition(Lots, MARKET, label:="long breakout");

if Position = 0 and Filled = 0 and brkDn then
    SetPosition(-1 * Lots, MARKET, label:="short breakdown");
