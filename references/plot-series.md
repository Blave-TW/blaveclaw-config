# PLOT_SERIES — Indicator Overlay on the Workspace Trade Chart

Type A strategies can declare indicator series to be drawn on the web workspace's
entry/exit chart. Declare `PLOT_SERIES` in the config section of `strategy.py`;
`lib/runner.py` writes the series into `stats.json` alongside the trade log, and the
workspace renders them under (or over) the price chart with the trade markers.

This is display-only: it never affects signals, PnL, or live execution.

## Declaration

`PLOT_SERIES` is a dict of `display name → spec`:

```python
# Sub-pane (default) — an oscillator drawn in its own panel below the price chart.
# The string names a column of the DataFrame fetch_data returns (added by _add_indicators):
PLOT_SERIES = {"Broker Flow Z-Score": "zscore"}

# Overlay — drawn on the price chart itself (moving averages, bands):
PLOT_SERIES = {
    "SMA 20": ("SMA_F", {"overlay": True}),
    "SMA 50": ("SMA_S", {"overlay": True}),
}

# Pane grouping — related series share one sub-pane (compared on the same axis):
PLOT_SERIES = {
    "MACD":   ("MACD",     {"pane": "macd"}),
    "Signal": ("MACD_SIG", {"pane": "macd"}),
}

# Threshold levels — fixed horizontal dashed lines in the series' pane (TradingView hline):
PLOT_SERIES = {"Taker Intensity": ("TI", {"levels": {"Entry": ENTRY_TI, "Exit": EXIT_TI}})}
```

Spec forms:

| Spec | Meaning |
|---|---|
| `"col"` | column of the backtest df, sub-pane |
| `pd.Series` | any series (reindexed to the df), sub-pane |
| `("col"` or `pd.Series, {"overlay": True})` | drawn over the price chart |
| `("col"` or `pd.Series, {"pane": "<group>"})` | sub-pane shared with all series of the same group id |
| `("col"` or `pd.Series, {"levels": {...}` or `[...]})` | fixed horizontal threshold lines in that series' pane |

Options combine freely in one dict, e.g. `{"pane": "macd", "levels": [0]}`.

The display name may be in the user's language (it renders in the web UI, not
matplotlib). Referencing a df column (the usual case) means the series is exactly what
`compute_signals` saw — no recomputation, no drift.

## Overlay vs sub-pane — how to choose

- `"overlay": True` only for series **in price units** sharing the price axis: moving
  averages, Bollinger bands, VWAP, entry/exit reference levels.
- Everything else (z-scores, RSI/KD, ratios, flow counts — anything not in price
  units) stays the default sub-pane. Overlaying a z-score on price flattens one of
  the two into a straight line.

## Pane grouping — how to choose

- Give the same `"pane"` group id to series that are **meant to be read against each
  other on one axis**: MACD + its signal line, %K + %D, an indicator + its threshold
  band. They render stacked in a single sub-pane.
- Unrelated series (different units or scales) keep separate panes — omit `"pane"`
  and each gets its own panel, the default. Forcing, say, RSI and raw volume into one
  group squashes one of them, same failure mode as overlaying a z-score on price.
- `"pane"` is ignored for `"overlay": True` series (they already share the price
  chart). Group ids are internal (≤ 32 chars, any string) and never rendered.

## Threshold levels — how to choose

> Note: `label` is accepted and stored but **not rendered** on the web chart for now — the text would not follow the UI language. Declare levels as a plain list of values unless you need the label for your own bookkeeping.

`"levels"` draws fixed horizontal dashed lines in the series' pane so the user can
see *where* the rule fires, not just the indicator wiggling. Two forms:

```python
# label → value (label renders next to the line; ≤ 16 chars, user's language is fine)
PLOT_SERIES = {"Taker Intensity": ("TI", {"levels": {"進場": 1.2, "出場": -0.5}})}

# plain values, no labels
PLOT_SERIES = {"RSI": ("RSI", {"levels": [30, 70]})}
```

Pass the **same constants `compute_signals` compares against** (`ENTRY_Z`, `EXIT_Z`,
…) — never retype the number, or the chart drifts from the rule.

Use it for:
- entry / exit thresholds the indicator is compared against (`z < ENTRY_Z`)
- a filter line (`ratio > 1.0`, `spread > 0`)
- the indicator's centre line (RSI 50, z-score 0, MACD 0)
- a fixed price level on an `"overlay": True` series (a breakout level, a cap) — same
  `levels` option, in price units

Do NOT use it for:
- a **dynamic** threshold (rolling quantile, ATR band, a moving average of the
  indicator) — that is a line that moves, so declare it as another series, grouped
  into the same `"pane"`
- a **disabled** threshold: if a parameter is a sentinel (`EXIT_Z = -1e9` meaning
  "never"), leave it out of `levels` yourself — the runner draws whatever finite value
  you give it and does not guess that an off-scale number means "off"
- a pure trend series with no threshold (a moving-average overlay, a cumulative flow
  line) — nothing to mark

Limits: ≤ **4** levels per series (first 4 kept), value must be finite
(nan/inf/non-numeric entries are skipped, the rest still render), label ≤ **16**
chars (truncated). A series with no valid level simply has no `levels` field.

## When to declare (and when not to)

Declaring is **mandatory** whenever a computed or external indicator drives the
entries/exits (anything from `_add_indicators`, a `rolling`/`ewm` window, an alpha or
twstock feed) — without it the workspace has no indicator pane. Only a pure price rule
(e.g. Close breaks a fixed level) may omit it. `lib/quality_check.py` flags a missing
declaration as WARNING and the backtest runner prints the same hint.

Declare only the 1–2 series that **explain the entry/exit decisions** — the indicator
the thresholds are applied to is almost always the right choice. Do NOT dump every
intermediate column of the df: the chart is for the user to see *why* the strategy
traded, and three unrelated lines bury that answer. Raw inputs the signal is derived
from (raw volume, unsmoothed flow) usually add noise, not explanation.

## Limits (enforced by the runner AND the server)

- Max **4** series (first 4 declared kept), name ≤ **64** chars
- Max **20,000** points per series (tail kept, same as the trade log)
- Non-finite values (nan/inf — e.g. the z-score's warm-up head) are skipped per point;
  the frontend shows a gap there, which is correct
- Max **4** `levels` per series, label ≤ **16** chars, value finite

## Complete example

`examples/tw2317_broker_zscore/strategy.py` — a contrarian strategy entering when the
broker-flow z-score drops below `ENTRY_Z` and exiting above `EXIT_Z` (the shipped
example declares the plain `"zscore"` form; shown here with its thresholds added):

```python
ENTRY_Z = -0.77
EXIT_Z  =  0.29

# the column _add_indicators adds, with the two thresholds drawn in the same pane
PLOT_SERIES = {"Broker Flow Z-Score": ("zscore", {"levels": {"Entry": ENTRY_Z, "Exit": EXIT_Z}})}

def _add_indicators(df, ...):
    ...
    df['zscore'] = (raw - roll_mean) / roll_std.replace(0.0, float('nan'))
    return df
```

After `run()` completes, `stats.json` contains:

```json
"panes": [
  {"name": "Broker Flow Z-Score", "overlay": false,
   "points": [[1651017600, -1.23], [1651104000, -0.98], ...],
   "levels": [{"value": -0.77, "label": "Entry"}, {"value": 0.29, "label": "Exit"}]}
]
```

`levels` is absent when the series declares none.

`points` timestamps are epoch seconds on the same basis as `trades[].ts`, so the
workspace aligns the indicator, the candles, and the trade markers on one time axis.
