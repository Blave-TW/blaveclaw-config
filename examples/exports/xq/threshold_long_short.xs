// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : indicator vs FOUR thresholds, long/short with a flat band
//            (Blave rule: BUY_TH > SELL_TH, COVER_TH > SHORT_TH; exits checked before entries)
// Blave    : references/strategy-code.md > "Long/Short - use FOUR independent thresholds"
//            The Python stateful loop's `pos` variable is XS's built-in Position.
// Indicator here = % deviation of Close from its SMA; swap in the strategy's own indicator.
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.
// Signals read the COMPLETED bar ([1]): XQ's daily backtest runs intrabar, so current-bar values fire a day early.

input: Len(20);
// XQ rejects identifiers that start with Buy/Sell, so Blave's BUY_TH/SELL_TH names cannot be kept.
input: LongEntryTh(3.0);  // Blave BUY_TH:   enter long  when dev >  LongEntryTh
input: LongExitTh(1.0);   // Blave SELL_TH:  exit long   when dev <  LongExitTh
input: ShortExitTh(-1.0); // Blave COVER_TH: exit short  when dev >  ShortExitTh
input: ShortEntryTh(-3.0);// Blave SHORT_TH: enter short when dev <  ShortEntryTh
input: Lots(1);

var: ma(0), dev(0);

// --- indicators ---
ma  = Average(Close, Len);

// --- signal ---
dev = 0;
if ma[1] <> 0 then dev = (Close[1] - ma[1]) / ma[1] * 100;

// --- orders ---   (same order as the Python loop: 1) exit first, 2) then entry)
// A same-bar exit-then-enter is impossible in XS (Position is fixed for the pass);
// the entry fires on the next pass instead. Report this difference to the user.
if Position > 0 and Filled > 0 and dev < LongExitTh then
    SetPosition(0, MARKET, label:="exit long");

if Position < 0 and Filled < 0 and dev > ShortExitTh then
    SetPosition(0, MARKET, label:="exit short");

if Position = 0 and Filled = 0 and dev > LongEntryTh then
    SetPosition(Lots, MARKET, label:="enter long");

if Position = 0 and Filled = 0 and dev < ShortEntryTh then
    SetPosition(-1 * Lots, MARKET, label:="enter short");
