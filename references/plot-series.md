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
```

Spec forms:

| Spec | Meaning |
|---|---|
| `"col"` | column of the backtest df, sub-pane |
| `pd.Series` | any series (reindexed to the df), sub-pane |
| `("col"` or `pd.Series, {"overlay": True})` | drawn over the price chart |
| `("col"` or `pd.Series, {"pane": "<group>"})` | sub-pane shared with all series of the same group id |

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

## Complete example

`examples/tw2317_broker_zscore/strategy.py` — a contrarian strategy entering when the
broker-flow z-score drops below `ENTRY_Z` and exiting above `EXIT_Z`:

```python
ENTRY_Z = -0.77
EXIT_Z  =  0.29

PLOT_SERIES = {"Broker Flow Z-Score": "zscore"}   # the column _add_indicators adds

def _add_indicators(df, ...):
    ...
    df['zscore'] = (raw - roll_mean) / roll_std.replace(0.0, float('nan'))
    return df
```

After `run()` completes, `stats.json` contains:

```json
"panes": [
  {"name": "Broker Flow Z-Score", "overlay": false,
   "points": [[1651017600, -1.23], [1651104000, -0.98], ...]}
]
```

`points` timestamps are epoch seconds on the same basis as `trades[].ts`, so the
workspace aligns the indicator, the candles, and the trade markers on one time axis.
