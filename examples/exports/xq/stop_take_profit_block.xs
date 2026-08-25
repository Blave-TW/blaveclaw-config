// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : fixed % stop-loss + take-profit exit block, on top of an SMA-cross long/short entry
//            Copy the "--- risk exits ---" block into any other template; keep it FIRST.
// Blave    : Type A with stop_pct / tp_pct constants applied in compute_signals
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.
// FilledAvgPrice = FIFO cost of the open position, unsigned, 0 when flat (guard with Filled).
// For stops to react intrabar the user must enable 逐筆洗價 in XQ; on bar close they act at the close.

input: FastLen(10);
input: SlowLen(30);
input: StopPct(3.0);      // stop-loss distance in %
input: TpPct(6.0);        // take-profit distance in %
input: Lots(1);

var: fastMA(0), slowMA(0);
var: goLong(false), goShort(false);
var: stopHit(false), tpHit(false);

// --- indicators ---
fastMA = Average(Close, FastLen);
slowMA = Average(Close, SlowLen);

// --- signal ---
// UNVERIFIED: `cross over/under` on declared vars - xshelp shows it only on Value1 / function calls.
//             If XQ rejects it, use CrossOver(Average(Close, FastLen), Average(Close, SlowLen)) inline.
goLong  = fastMA cross over  slowMA;
goShort = fastMA cross under slowMA;

// --- risk exits ---   (must be the first trading instructions in the file)
stopHit = false;
tpHit   = false;
if Filled > 0 then begin
    stopHit = Close <= FilledAvgPrice * (1 - StopPct / 100);
    tpHit   = Close >= FilledAvgPrice * (1 + TpPct / 100);
end;
if Filled < 0 then begin
    stopHit = Close >= FilledAvgPrice * (1 + StopPct / 100);
    tpHit   = Close <= FilledAvgPrice * (1 - TpPct / 100);
end;

if Position <> 0 and Filled = Position and stopHit then
    SetPosition(0, MARKET, label:="stop loss");

if Position <> 0 and Filled = Position and tpHit then
    SetPosition(0, MARKET, label:="take profit");

// --- signal exits / entries ---
if Position > 0 and Filled = Position and goShort then
    SetPosition(0, MARKET, label:="exit long on cross");

if Position < 0 and Filled = Position and goLong then
    SetPosition(0, MARKET, label:="exit short on cross");

if Position = 0 and Filled = 0 and goLong then
    SetPosition(Lots, MARKET, label:="enter long");

if Position = 0 and Filled = 0 and goShort then
    SetPosition(-1 * Lots, MARKET, label:="enter short");
