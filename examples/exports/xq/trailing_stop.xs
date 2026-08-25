// Blave Agent export template - XQ XS automated-trading script (交易腳本)
// Skeleton : % trailing stop from the best price since entry, on top of an SMA-cross long entry
// Blave    : Type A with a running-max trailing exit in compute_signals
// Generated from a template. NOT compiled here - compile and backtest in XQ before use.
// The running high must survive tick re-execution of the same bar -> intrabarpersist.
// With 逐筆洗價 off it updates once per bar close, matching Blave's bar-close logic.

input: FastLen(10);
input: SlowLen(30);
input: TrailPct(5.0);     // exit when Close falls this % below the peak since entry
input: Lots(1);

var: fastMA(0), slowMA(0);
var: longEntry(false), trailHit(false);
variable: intrabarpersist peakPrice(0);

// --- indicators ---
fastMA = Average(Close, FastLen);
slowMA = Average(Close, SlowLen);

// --- signal ---
// UNVERIFIED: `cross over/under` on declared vars - xshelp shows it only on Value1 / function calls.
//             If XQ rejects it, use CrossOver(Average(Close, FastLen), Average(Close, SlowLen)) inline.
longEntry = fastMA cross over slowMA;

// --- trailing stop state ---
if Filled > 0 then begin
    if peakPrice = 0 or Close > peakPrice then peakPrice = Close;
    trailHit = Close <= peakPrice * (1 - TrailPct / 100);
end else begin
    peakPrice = 0;
    trailHit  = false;
end;

// --- orders ---   (trailing exit first)
if Position > 0 and Filled = Position and trailHit then
    SetPosition(0, MARKET, label:="trailing stop");

if Position = 0 and Filled = 0 and longEntry then
    SetPosition(Lots, MARKET, label:="SMA entry");
