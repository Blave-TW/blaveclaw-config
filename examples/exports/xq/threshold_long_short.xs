// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : indicator vs FOUR thresholds, long/short with a flat band
//            (Blave rule: BUY_TH > SELL_TH, COVER_TH > SHORT_TH; exits checked before entries)
// Blave    : references/strategy-code.md > "Long/Short - use FOUR independent thresholds"
//            The Python stateful loop's `pos` variable is XS's built-in Position.
// Indicator here = % deviation of Close from its SMA; swap in the strategy's own indicator.
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.

input: Len(20);
input: BuyTh(3.0);        // enter long  when dev >  BuyTh
input: SellTh(1.0);       // exit long   when dev <  SellTh
input: CoverTh(-1.0);     // exit short  when dev >  CoverTh
input: ShortTh(-3.0);     // enter short when dev <  ShortTh
input: Lots(1);

var: ma(0), dev(0);

// --- indicators ---
ma  = Average(Close, Len);
dev = 0;
if ma <> 0 then dev = (Close - ma) / ma * 100;

// --- orders ---   (same order as the Python loop: 1) exit first, 2) then entry)
// A same-bar exit-then-enter is impossible in XS (Position is fixed for the pass);
// the entry fires on the next pass instead. Report this difference to the user.
if Position > 0 and Filled > 0 and dev < SellTh then
    SetPosition(0, MARKET, label:="exit long");

if Position < 0 and Filled < 0 and dev > CoverTh then
    SetPosition(0, MARKET, label:="exit short");

if Position = 0 and Filled = 0 and dev > BuyTh then
    SetPosition(Lots, MARKET, label:="enter long");

if Position = 0 and Filled = 0 and dev < ShortTh then
    SetPosition(-1 * Lots, MARKET, label:="enter short");
