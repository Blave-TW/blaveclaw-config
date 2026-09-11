// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : % trailing stop from the best price since entry, on top of an SMA-cross long entry
// Blave    : Type A with a running-max trailing exit in compute_signals
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.
// Bar-close trailing stop, like Blave's running-max exit: peak and exit read the COMPLETED bar's
// close ([1]); Filled[1] skips the entry bar, whose Close[1] predates the fill. intrabarpersist
// keeps peakPrice across tick re-executions - required if you switch to the live Close.

input: FastLen(10);
input: SlowLen(30);
input: TrailPct(5.0);     // exit when the completed close falls this % below the peak since entry
input: Lots(1);

var: fastMA(0), slowMA(0);
var: longEntry(false), trailHit(false);
variable: intrabarpersist peakPrice(0);

// --- indicators ---
fastMA = Average(Close, FastLen);
slowMA = Average(Close, SlowLen);

// --- signal ---
longEntry = fastMA[1] cross over slowMA[1];

// --- trailing stop state ---
if Filled > 0 and Filled[1] > 0 then begin
    if peakPrice = 0 or Close[1] > peakPrice then peakPrice = Close[1];
    trailHit = Close[1] <= peakPrice * (1 - TrailPct / 100);
end else begin
    peakPrice = 0;
    trailHit  = false;
end;

// --- orders ---   (trailing exit first)
if Position > 0 and Filled = Position and trailHit then
    SetPosition(0, MARKET, label:="trailing stop");

if Position = 0 and Filled = 0 and longEntry then
    SetPosition(Lots, MARKET, label:="SMA entry");
