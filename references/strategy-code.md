# Strategy Code Structure (Type A & C)

NOTE: This guide covers **Type A (Signal Strategy)** and **Type C (Portfolio Strategy)**. For Type B (everything else), write from scratch — no template, no backtest.

CRITICAL: Every Type A strategy MUST be based on `strategies/TEMPLATE_A.py`. Copy the template and fill in the marked sections. Do NOT write a standalone backtest script from scratch.

## Steps

1. Copy `strategies/TEMPLATE_A.py` to `strategies/[strategy_name]/strategy.py`
2. Set config at the top: `STRATEGY_NAME`, `DISPLAY_NAME`, `DESCRIPTION`, `SYMBOL`, `EXCHANGE`, `INTERVAL`, `START` (see *Naming & description* below)
3. Fill in the three marked sections:
   - `_add_indicators(df, p1, p2)` — add indicator columns, parameterized for scan
   - `fetch_data(hdrs)` — fetch kline, call `_add_indicators` with module params
   - `compute_signals(df)` — vectorized signal logic, returns pd.Series
4. Run: `python3 strategies/[strategy_name]/strategy.py`
   - **Long runs:** a large universe (Type C with 100+ symbols, cold cache) can take 10+ minutes. Run it in the foreground with an explicit long `timeout` on the Bash tool (up to 30 min) and report the stats when it finishes. Before re-running an existing strategy, delete its stale `stats.json` first (`rm -f strategies/<name>/stats.json`) — the runner overwrites it only at the end, so an old file would otherwise be mistaken for the new result. That is the ONLY moment deleting `stats.json` is allowed: immediately before a re-run you then execute in the same turn. `stats.json` is the web workspace's report — the 回測數據 and 進出場紀錄 tabs and the 下單設定 › 選擇策略 picker all read it — so never end your turn with a strategy missing its `stats.json`, and never delete one as "cleanup": if you delete it (or decide the last run's result shouldn't stand), you must run a backtest that regenerates a `stats.json` matching the current `strategy.py` before ending the turn. (Removing an entire strategy directory at the user's request is a different operation, unaffected by this rule.) If that regenerating run fails or the user interrupts, do not fabricate a `stats.json` and do not iterate past the brakes to force one — stop and tell the user the strategy is currently left without `stats.json` (its workspace tabs will be empty until the next successful run); honest failure reporting wins over this invariant. If a run does get moved to the background anyway, wait in the foreground with `until [ -f strategies/<name>/stats.json ]; do sleep 10; done` (same long timeout); never a background loop you then stop. Never end the turn while it is running: the turn's exit kills the process and the user paid for nothing. Warn the user up front when a run will take minutes.

## Naming & description

Every strategy (Type A, B, and C) sets three name/label fields at the top of the file:

- `STRATEGY_NAME` — the **technical id**: lowercase, snake_case, used for the directory, files, and reporting key. Keep it stable; nothing user-facing depends on it. (e.g. `doge_holder_conc`)
- `DISPLAY_NAME` — the **human-facing name** shown in the workspace strategy list. Write it in plain language: *what it trades + what it does*, NOT the engineer id or a raw indicator name. Use the language the user is conversing in (Chinese users → Chinese). Examples: `doge_holder_conc` → `"DOGE 大戶持倉集中度"`, `rsi_bb_reversal` → `"RSI＋布林通道反轉"`, `supertrend_sol` → `"SOL SuperTrend 順勢"`.
- `DESCRIPTION` — **one plain sentence** the user could read months later and remember what this strategy does, e.g. `"追蹤 DOGE 大戶持倉集中度,集中度升高時進場做多"`. One line, no jargon dump.

```python
STRATEGY_NAME = "doge_holder_conc"
DISPLAY_NAME  = "DOGE 大戶持倉集中度"
DESCRIPTION   = "追蹤 DOGE 大戶持倉集中度,集中度升高時進場做多"
```

Always set `DISPLAY_NAME` and `DESCRIPTION` when creating a strategy — the workspace shows the technical id only as a fallback when they are missing.

## Editing a live strategy (fork, never in place)

A strategy that is in the trading portfolio with an amount > 0 is LIVE: its
scheduled run re-imports `strategy.py` every bar, so a saved edit takes effect
on the next bar and can flip a real position with no warning. Never edit any
file of a live strategy in place — not even a "small" parameter tweak (a
threshold change is exactly what flips a position). No platform lets edits act
on live positions (Pionex/QuantConnect hard-stop first; 3Commas scopes edits
to new deals; TradingView alerts snapshot the script). Check
`portfolio_config.json` `amounts` before touching any deployed strategy's files.

The flow is FORK → EDIT → DEPLOY → SWITCH:

1. Tell the user the strategy is live and that you will build the change as a
   new strategy while the original keeps trading untouched.
2. Fork it to a new strategy under its own `STRATEGY_NAME` (same conventions
   as `references/marketplace.md` › *Forking a strategy*: own
   `DISPLAY_NAME`/`DESCRIPTION`, `MODE = "backtest"`). Edit the fork.
3. Backtest the fork; show the result next to the original's current stats.
   Not satisfied → iterate (Iteration Brakes apply as usual) or discard the
   fork; the live strategy was never touched.
4. **Deploy the fork's signal schedule BEFORE switching** and confirm one run
   has written `strategies/<fork>/state.json`. A funded strategy with no
   `state.json` contributes NOTHING to the reconciler's target — switch
   without this and the original's position closes while the fork's never
   opens, silently (no healthcheck fires for a never-scheduled strategy). On
   the Blave Agent runtime the platform schedules a funded strategy on its
   own within ~a minute of the 下單設定 save; on crontab machines YOU must
   schedule it per `references/deployment.md` first. Either way, verify
   `state.json` exists before step 5.
5. Only after the user confirms: switch via 下單設定 in ONE save — set the
   fork's amount, and set the original's amount to 0 while KEEPING it
   selected (a 0 amount stops sizing but preserves its routing and registry,
   which is what makes rollback a one-save operation; unselecting removes
   them). Tell the user BEFORE the save what will happen: with the fork's
   `state.json` in place the reconciler nets old-out/new-in in the same
   round — same symbol, same direction means little or no trading at the
   switch; opposite direction, or a fork whose signal hasn't run yet, means
   the original's position closes within seconds and the fork enters on its
   own signal (a full close-and-reopen round trip of fees/slippage).
6. Keep the defunded original unless the user asks to delete it — it is the
   rollback path (switch back the same way).

Identity changes (NAME / SYMBOL / MARKET) are the same case, not a lighter
one: mutating identity in place makes the old target vanish, so the
reconciler closes the position with no warning (measured 2026-08-05: an
agent renamed a funded strategy to flip MARKET to spot; the futures position
auto-closed, the web's picker showed a ghost entry, and the user read all of
it as breakage). An identity change IS a new strategy — same fork-and-switch
flow, same warning before the save.

## Signal Contract

`compute_signals(df)` receives the DataFrame returned by `fetch_data` and returns either a **pd.Series** or a **(pd.Series, exec_at_close)** tuple:

| Value | Meaning | Execution |
|-------|---------|-----------|
| positive float (`1.0`, `0.6`, …) | long, value = position size fraction | next bar **Open** |
| negative float (`-1.0`, `-0.6`, …) | short, value = position size fraction | next bar **Open** |
| `0.0` | flat — exit | next bar **Open** |
| `nan` | hold — keep current position unchanged | no trade |

**Execution model — two types, must be explicit:**
- **next-bar open** (default): return plain `pd.Series` — signal at close[t] executes at open[t+1]
- **this-bar close**: return `(signals, exec_at_close)` where `exec_at_close` is a bool `pd.Series` (True on bars that execute at close). For futures settlement use `settlement_signals_from_db()` which handles this automatically.

`nan` vs `0.0`: use `nan` to hold the current position (e.g. inside a trend). Use `0.0` to exit at next open.

## Three-Layer Architecture

```
_add_indicators(df, p1, p2)   → df with indicators       (indicator layer, parameterized)
fetch_data(hdrs)               → fetch_kline + _add_indicators(module params)
compute_signals(df)            → pd.Series: 1.0 / 0.0 / nan
```

`lib/runner.py` handles everything else: backtest, chart, stats, notify, live execution.
You write `_add_indicators`, `fetch_data`, and `compute_signals`.

Separating `_add_indicators` allows `scan.py` to fetch data once and sweep parameters cheaply without re-hitting the API.

**CRITICAL — signal logic belongs only in `compute_signals()`:**
- `_add_indicators` computes indicator columns only
- `fetch_data` fetches kline and calls `_add_indicators` with module-level params
- All threshold logic, dead zone logic, signal decisions → `compute_signals`

**Dead zone example (threshold strategy with hold zone):**
```python
ENTRY_TH = 0.5
EXIT_TH  = -0.5

def _add_indicators(df, hdrs, entry_th=ENTRY_TH, exit_th=EXIT_TH):
    from lib.data import fetch_taker_intensity
    df = df.copy()
    ti = fetch_taker_intensity(SYMBOL, INTERVAL, START, END, hdrs)
    df = df.join(ti.rename(columns={"alpha": "TI"}))
    df["TI"] = df["TI"].ffill()
    return df

def fetch_data(hdrs):
    from lib.data import fetch_kline
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    return _add_indicators(df, hdrs)

def compute_signals(df):
    import pandas as pd, numpy as np
    ti     = df["TI"]
    signal = pd.Series(np.nan, index=df.index)
    signal[ti > ENTRY_TH] = 1.0
    signal[ti < EXIT_TH]  = 0.0
    return signal
```

**Crossover example (golden / death cross):**
```python
SMA_FAST = 20
SMA_SLOW = 50

def _add_indicators(df, fast=SMA_FAST, slow=SMA_SLOW):
    df = df.copy()
    df['SMA_F'] = df['Close'].rolling(fast).mean()
    df['SMA_S'] = df['Close'].rolling(slow).mean()
    return df

def fetch_data(hdrs):
    from lib.data import fetch_kline
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    return _add_indicators(df)

def compute_signals(df):
    import pandas as pd, numpy as np
    k, d   = df['SMA_F'], df['SMA_S']
    golden = (k > d) & (k.shift(1) <= d.shift(1))
    death  = (k < d) & (k.shift(1) >= d.shift(1))
    signal = pd.Series(np.nan, index=df.index)
    signal[golden] = 1.0
    signal[death]  = 0.0
    return signal
```

`compute_signals` must be a **pure function** (no API calls, no I/O, no side effects).

## Indicator hygiene — standardise ratios in log space

A **ratio** (put/call ratio, volume ratio, OI ratio — anything `>0` and right-skewed) must be **log-transformed before** computing a rolling z-score or mean. A raw z-score assumes a symmetric distribution; a ratio is multiplicative and bounded below by 0, so a raw z-score compresses the high tail (exactly where the extreme signal usually lives) and exaggerates the low tail. Take `log` first, then standardise:

```python
logp = np.log(df['pcr'])                       # ratio → additive, ~symmetric
z    = (logp - logp.rolling(w).mean()) / logp.rolling(w).std()
```

This makes the z-score symmetric, keeps extreme readings meaningful, and tends to move the scanned optimum away from the grid edge (a raw-ratio scan often pins the best threshold at the boundary because the skew piles up there). Same idea for any strictly-positive, multiplicatively-distributed input.

## Long/Short — use FOUR independent thresholds

CRITICAL: A strategy that trades **both** long and short needs four distinct thresholds, never two. Long-exit and short-entry are **separate decisions** and must not share a threshold.

```python
BUY_TH   =  0.8   # enter long   when indicator rises above this
SELL_TH  =  0.2   # exit long → flat when it falls back below this
COVER_TH = -0.2   # exit short → flat when it rises back above this
SHORT_TH = -0.8   # enter short  when it falls below this
# HARD constraints (per side): BUY_TH > SELL_TH, COVER_TH > SHORT_TH, and BUY_TH > SHORT_TH.
# SELL_TH vs COVER_TH is a DESIGN CHOICE, not a rule:
#   SELL_TH > COVER_TH → clean flat neutral band in the middle (tight exits, mean-reversion style)
#   SELL_TH < COVER_TH → overlapping HOLD zone (cover_th..sell_th): positions ride through the
#                        middle, exiting only on the far side (trend / give-room style)
```

**Why not two?** Collapsing long-exit and short-entry into one `exit_th` (e.g. `signal[x < exit_th] = -1.0`) removes the flat state entirely — the book is *always* long or short and flips directly at one level. That couples two unrelated risk decisions, eliminates any neutral zone, and makes the backtest flip on every minor oscillation around that single level. The four-threshold form gives each side its own entry and its own exit, with a flat hold-band in the middle.

**Two sides on → position is stateful; use `lib.strategy.threshold_position`, never a vectorized assignment.**

The naive `signal[x>buy]=1; signal[x<short]=-1; signal[(x>cover)&(x<sell)]=0` (then ffill) is **buggy**. With one side only, the exit is a half-line (`x < exit_th` → 0) that price can never skip — vectorized is fine. But with two sides the flat exit must be a *bounded band* `(cover_th, sell_th)` (a half-line would overwrite the opposite entry). An exit is a **threshold crossing**, not "landing inside a band": if price gaps over the band in a single bar (e.g. from short territory straight into the long dead zone), `ffill` keeps holding the **stale** position instead of exiting. The dead zones can no longer be told apart from a carried-over position. Correct exit logic requires the current position, i.e. **state**:

```python
def compute_signals(df, buy_th=BUY_TH, sell_th=SELL_TH,
                     cover_th=COVER_TH, short_th=SHORT_TH):
    from lib.strategy import threshold_position
    return threshold_position(df['indicator'], buy_th, sell_th, cover_th, short_th)
```

Dead zones now behave correctly: a long is **held** through `(sell_th, buy_th)` and only exits once `x` actually crosses *below* `sell_th` — no matter how far it gaps; a short is held through `(short_th, cover_th)` and exits only on crossing *above* `cover_th`. A big gap from one side past the flat band straight to the opposite entry flips in a single bar (exit then enter), which the band-landing version silently misses.

`threshold_position` is the per-bar state machine (exit checked before entry, NaN holds). It is a Python loop — ~0.5 s per 390k bars (5-min since 2023, measured on a Lightsail medium) — which is nothing once per backtest. Never hand-write the loop in `strategy.py`; call the lib. For scans see *Scanning four thresholds* below: with one side pushed out of range the lib takes a vectorized path, so a scan cell costs milliseconds, not the loop.

**Scanning any pair — the flow is always `scan_grid → find_plateau → (on_edge → extend_axis → scan_grid → find_plateau, once) → write_scan → plot_heatmap`** (`write_scan` feeds the web 穩健參數 tab; details and the two web prompts in `references/lib.md` › *Parameter scan workflow*).

**Grid size — three principles** (Pardo's plateau-search practice / industry convention): **10–20 values per axis** (the `nice_grid` / `percentile_thresholds` defaults: ≈ 15 → 100–400 combos — scan time is linear in combos × bars × the cost of ONE `compute_signals` call: vectorized signals (`hysteresis`, rolling/where/ffill) run 100–400 cells in ≤ 10 s on any bar count, 40×40 on two years of 5-min bars in ~45 s; a per-bar Python loop inside `compute_signals` costs ~0.5 s per cell on 390k bars (minutes per grid) — vectorize before widening the grid, never the other way round); **the step must have trading meaning** (whole bars for windows, a threshold move a trader would notice — a finer step makes neighbours differ by noise and the plateau's neighbourhood mean degenerate into a single cell); **coarse first, then fine** (zoom a second scan into the plateau's neighbourhood only if it needs resolving). 40 per axis is the hard cap.

**Plateau on a grid border → extend that side once, rescan, then `write_scan`.** A border cell's neighbourhood is truncated and the real optimum may lie outside the range. After `find_plateau`, `on_edge(best_idx, grid.shape)` lists the borders hit (`(axis, side)`); `extend_axis(vals, side)` adds 5 cells beyond that end at the same step (int axes stay int, never past 40; `floor=1` for bar counts). Rerun `scan_grid → find_plateau` on the extended axes and write that grid. **One extension only** — it is the same scan and the same iteration under the Iteration Brakes; a plateau still on the border after it is reported as such.

**Scanning four thresholds — scan each side independently, two heatmaps.**

`scan_grid` / `plot_heatmap` are inherently 2D (two params → one heatmap). Do NOT force symmetry to squeeze four params into one chart — long and short are independent decisions and the market is rarely symmetric (crashes are faster than rallies). Instead scan each side on its own:

1. **Long scan** — sweep `buy_th` × `sell_th` with the short side turned OFF → `heatmap_long.png`, pick the best long pair.
2. **Short scan** — sweep `short_th` × `cover_th` with the long side turned OFF → `heatmap_short.png`, pick the best short pair.
3. Put all four into `strategy.py`, run the full long+short backtest to verify.

With one side off, the position is a one-sided hysteresis (a half-line exit, exactly expressible as `where` + `ffill`). `threshold_position` detects a side whose entry no bar ever reaches and takes that vectorized path itself (`lib.strategy.hysteresis`, ~5 ms per call on 390k bars), so the ±1e9 idiom in the full example below is what makes a per-side scan fast — a hand-written loop in `compute_signals` would pay ~0.5 s per cell instead. Anything `compute_signals` does after the position (vol scaling, filters, `exec_at_close`) is kept, because the scan still goes through `compute_signals`.

**Choosing the scan ranges — do NOT use `percentile_thresholds` for a contrarian/mean-reversion strategy.** That helper splits the distribution into entry=upper-half / exit=lower-half, which hard-codes the assumption "exit on the opposite side of zero from entry" (fine for momentum). A contrarian long often takes profit on a *positive* indicator reading (fear normalising, not flipping to greed), so its best exit lives on the **same side as entry** — a region `percentile_thresholds` never samples. Instead derive the span from the indicator's own distribution and let the exit sweep **both sides**:

```python
from lib.param_scan import nice_grid, nice_step
zs     = df['indicator'].dropna()
LO, HI = np.percentile(zs, [2, 98])                              # data-driven span
step   = nice_step(HI - LO, 15)                                  # one nice step for both axes (~15 cells across the span)
buy_vals  = nice_grid(0.2, HI, current=s.BUY_TH,  step=step)    # entry on the signal side
sell_vals = nice_grid(LO,  HI, current=s.SELL_TH, step=step)    # exit: full range, BOTH sides
```

**Axes are always `nice_grid` axes** (never a raw `linspace` / percentile list): look at the distribution to choose the range, but the cells must be integers or multiples of 1/2/2.5/5 (`step` = span/(n-1) rounded to `{1, 2, 2.5, 5}×10^k`) **and the strategy's current constant must be a cell** — `nice_grid` anchors the lattice on `current`. The web 穩健參數 tab marks "you are here" by locating `current` on the axis; a linspace like `[0.065, 0.543, 1.022, 1.5, 1.979]` never contains the file's `0.5`, so the tab shows 「不在掃描範圍」 and cannot highlight the current cell. Bar-count parameters use `integer=True` (step ≥ 1, int cells). `nice_grid` coarsens an axis that would exceed the 40-cell api cap instead of truncating it.

Because each scan turns the **other side OFF**, the long scan and short scan are fully decoupled — so optimising each in isolation is valid no matter how `SELL_TH` and `COVER_TH` end up ordered relative to each other. Turn a side off by pushing its thresholds out of range (`compute_signals` already takes all four as kwargs):

```python
import numpy as np
from lib.param_scan import scan_grid, find_plateau, write_scan, plot_heatmap
import strategy as s

# long side: short_th/cover_th pushed to -inf so no short ever triggers
long_fn  = lambda data, buy_th, sell_th: s.compute_signals(
    data, buy_th=buy_th, sell_th=sell_th, short_th=-1e9, cover_th=-1e9)
grid_L = scan_grid(df, long_fn, buy_vals, sell_vals,
                   row_param='buy_th', col_param='sell_th',
                   fee=s.FEE, freq='1d', warmup=s.WARMUP,
                   valid_fn=lambda b, sll: b > sll)
best_L, nbr_L, *_ = find_plateau(grid_L, buy_vals, sell_vals)
# scan.json holds ONE grid (the web 穩健參數 tab shows one heatmap): write the side you
# are recommending — here the long side; the short side stays heatmap-only.
write_scan(grid_L, buy_vals, sell_vals, nbr_L, best_L, 'strategies/<name>',
           row_param='BUY_TH', col_param='SELL_TH', fee=s.FEE, start=s.START,
           end=df.index[-1].strftime('%Y-%m-%d'), current=(s.BUY_TH, s.SELL_TH))
plot_heatmap(grid_L, buy_vals, sell_vals, best_L, row_label='BUY_TH', col_label='SELL_TH',
             output_path='strategies/<name>/heatmap_long.png')

# short side: buy_th/sell_th pushed to +inf so no long ever triggers
short_fn = lambda data, short_th, cover_th: s.compute_signals(
    data, buy_th=1e9, sell_th=1e9, short_th=short_th, cover_th=cover_th)
grid_S = scan_grid(df, short_fn, short_vals, cover_vals,
                   row_param='short_th', col_param='cover_th',
                   fee=s.FEE, freq='1d', warmup=s.WARMUP,
                   valid_fn=lambda sh, c: sh < c)
best_S, *_ = find_plateau(grid_S, short_vals, cover_vals)
plot_heatmap(grid_S, short_vals, cover_vals, best_S, row_label='SHORT_TH', col_label='COVER_TH',
             output_path='strategies/<name>/heatmap_short.png')
```

After combining, the only hard checks are the per-side ones — `BUY_TH > SELL_TH`, `COVER_TH > SHORT_TH`, and `BUY_TH > SHORT_TH`. The order of `SELL_TH` vs `COVER_TH` is **not** a correctness check: `SELL_TH > COVER_TH` gives a flat neutral band, `SELL_TH < COVER_TH` gives an overlapping hold zone (positions ride through the middle). Both are valid — just confirm the independently-picked exits put you in the regime you intended.

**Scanning three or more parameters — pairwise coordinate descent, max two rounds, ONE `scan.json`.** Canonical example: `examples/tw2317_broker_zscore/scan.py` (four constants: `ENTRY_Z`, `EXIT_Z`, `WINDOW`, `ZSCORE_WIN`).

`scan_grid` / `plot_heatmap` / `scan.json` are 2D, and the web's 穩健參數 tab draws exactly one grid. Do not try to flatten N parameters into one chart; sweep them a pair at a time with the others pinned:

1. **Round 1 — the two most likely sensitive constants**, everything else pinned at the values `strategy.py` holds now. Thresholds first (entry/exit levels move the Sharpe most), then window lengths, then the rest. Every constant must be a `compute_signals` kwarg so the pinned ones are just passed through. `find_plateau` → pin that pair at its plateau.
2. **Round 2 — the next pair** (or, with three constants, the remaining one paired with the most sensitive one from round 1), thresholds pinned at the round-1 plateau. `find_plateau` → done.
3. **Stop after two rounds.** Each round is one iteration under `AGENTS.md` › *Iteration Brakes*; do not re-scan round 1 with the round-2 windows, do not add a third pair. Report both plateaus and let the user choose. The border extension (`on_edge → extend_axis → rescan`, once) applies to **round 1 only** — it stays inside that iteration and is what `scan.json` gets; round 2 is the last iteration and never extends.

Every round gets its own `plot_heatmap` PNG (`heatmap.png`, `heatmap_round2.png` — they reach the user through the workspace chat / Telegram). **`write_scan` is called once, for the round-1 pair only.** Reason: the web's adopt prompt only changes the two constants in `scan.json`, and round 1 is the only grid computed with every *other* constant at the file's current value — so its `current` mark is honest and 「把 ENTRY_Z 改成 …」 lands on a cell that was actually scanned. Round 2 was computed with the thresholds pinned at the plateau, not the file's values; writing it would mark a current cell whose Sharpe was never measured under the file's constants. The round-2 recommendation goes in the conversation. Long/short threshold pairs that decouple by turning the other side off (previous section) are a separate case — two independent 2D scans, write the side you recommend.

**One real parameter only** (`FEE`, `SYMBOL`, `INTERVAL` do not count): pair it with another constant that actually exists in the strategy. If there truly is only one, scan it 1D — loop `compute_signals` over a `nice_grid` axis, plot a line chart, send the PNG — and **write no `scan.json`**: the web's first version draws 2D grids only, and a padded fake axis would show the user a heatmap of a parameter that does not exist.

## What You Do NOT Need to Write

- Backtest loop — handled by `lib/runner.py`
- `main()` function — handled by `lib/runner.py`
- `place_order()` — handled by `strategies/reconciler/reconciler.py`
- Logging setup — handled by `lib/runner.py`
- Chart / stats / notify — handled by `lib/runner.py`

## Alpha Indicators (Blave alpha signals)

Use `lib/data` fetch functions inside `fetch_data(hdrs)`:

```python
def fetch_data(hdrs):
    from lib.data import fetch_kline, fetch_holder_concentration
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    hc = fetch_holder_concentration(SYMBOL, INTERVAL, START, END, hdrs)
    df = df.join(hc.rename(columns={"alpha": "HC"}))
    df["HC"] = df["HC"].ffill()
    return df
```


## Vol Targeting (optional)

Add `VOL_TARGETING = True` and `TARGET_VOL`, `VOL_LOOKBACK`, `PERIODS_PER_YEAR`, `VOL_CAP` to config. Then use `lib/strategy` helpers:

```python
# fetch_data — add one line after building df:
def fetch_data(hdrs):
    from lib.data import fetch_kline
    from lib.strategy import add_realized_vol
    df = fetch_kline(SYMBOL, INTERVAL, START, END, hdrs)
    if VOL_TARGETING:
        add_realized_vol(df, VOL_LOOKBACK, PERIODS_PER_YEAR)
    return df

# compute_signals — scale signal before returning:
def compute_signals(df):
    import pandas as pd, numpy as np
    from lib.strategy import apply_vol_scaling
    signal = pd.Series(np.nan, index=df.index)
    # ... signal logic ...
    return apply_vol_scaling(signal, df, TARGET_VOL, VOL_CAP) if VOL_TARGETING else signal
```

Default values: `TARGET_VOL=0.10`, `VOL_CAP=2.0`. For 1h bars: `VOL_LOOKBACK=720`, `PERIODS_PER_YEAR=8760`.

## Futures Strategies (CME / NYMEX / ICE)

For futures contracts, use `fetch_db_kline(dataset, symbol, schema, start, end, headers)`.

**CRITICAL — always include settlement exit.** Futures contracts expire monthly. Holding through expiry risks forced settlement at an unfavorable price. Always force-exit on the last trading day using `settlement_signals_from_db()`:

```python
def compute_signals(df):
    import pandas as pd, numpy as np
    signal = pd.Series(np.nan, index=df.index)
    # ... signal logic ...

    # settlement exit: forces signal=0.0 on last bar before each contract rollover,
    # marks those bars exec_at_close=True (executes at this-bar close, not next open)
    from lib.data import settlement_signals_from_db
    return settlement_signals_from_db(df, signal)
```

`settlement_signals_from_db(df, signal)` returns `(signal, exec_at_close)` — return it directly from `compute_signals`. It detects rollover dates automatically from the `instrument_id` column in the df returned by `fetch_db_kline`. See `examples/cl_sma/strategy.py` for a complete working example.

## Key Rules

- `END = None` always fetches to today — `fetch_kline` handles this automatically
- Default is always `MODE = "backtest"` — only switch to `"live"` after user confirms
- NEVER truncate or cap arrays (no `[:N]` slicing)
- Execution timing: signal fires at Close[t] → executes at Open[t+1] by default (next-bar open); use `exec_at_close` for this-bar close execution (futures settlement only)

---

## Blave API Headers

All `lib/data.py` functions accept a `headers` dict. Construct it as:

```python
from dotenv import load_dotenv; load_dotenv()
import os
hdrs = {'api-key': os.environ['blave_api_key'], 'secret-key': os.environ['blave_secret_key']}
```

The runner builds this automatically — only needed when calling lib functions outside of `run()`.

**NEVER use** `X-API-KEY`, `X-SECRET-KEY`, or `Authorization: Bearer ...` — those are wrong formats and will return 403.

---

## Telegram Pairing Check

Run this at session start, before any strategy run or notification:

```python
import json, os, platform
# Same BLAVECLAW_HOME resolution as lib/notify.py — the unset-default is
# runtime-dependent, never a single hardcoded path: old BlaveClaw machines use
# /root/.openclaw, the Blave Agent runtime uses /opt/blave-agent (detected by
# its openclaw.json FILE existing — not just the directory, or a half-provisioned
# machine passes falsely). A wrong home doesn't error here: paired just reads
# False on a machine that IS paired.
def _blaveclaw_home():
    if os.environ.get("BLAVECLAW_HOME"):
        return os.environ["BLAVECLAW_HOME"]
    if platform.system() == "Windows":
        return r"C:\openclaw"
    if os.path.isfile("/opt/blave-agent/openclaw.json"):
        return "/opt/blave-agent"
    return "/root/.openclaw"

allow_path = os.path.join(_blaveclaw_home(), "credentials", "telegram-default-allowFrom.json")
paired = (
    os.path.exists(allow_path)
    and bool(json.load(open(allow_path)).get("allowFrom"))
)
```

If `paired` is False: tell the user "Telegram is not paired yet. Please complete the pairing flow via the bot." Do not proceed until pairing is confirmed.

---

## Type C — Portfolio Strategy

CRITICAL: Every Type C strategy MUST be based on `strategies/TEMPLATE_C.py`. Copy it and fill in the marked sections. Read `examples/tw100_foreign_zscore/strategy.py` as a complete working reference.

### Steps

1. Copy `strategies/TEMPLATE_C.py` to `strategies/[strategy_name]/strategy.py`
2. Set config: `STRATEGY_NAME`, `DISPLAY_NAME`, `DESCRIPTION`, `UNIVERSE`, `SIGNAL_WINDOW`, `WARMUP`, `FEE`, `START` (see *Naming & description* above)
3. Fill in three sections:
   - `fetch_data(hdrs)` — fetches close/open/signal data for all UNIVERSE symbols; returns a **tuple**
   - helpers (e.g. `_compute_weights`, `_rebalance_mask`) — signal → weight conversion
   - `compute_signals(data)` — unpacks tuple, builds weight matrix, returns `(weights.values, price_df)`
4. Run: `python3 strategies/[strategy_name]/strategy.py`

### Interface Contract

```
fetch_data(hdrs)         → tuple of DataFrames   (close_df, open_df, ...)   # open_df always second; aux data (e.g. foreign_df) appended after
compute_signals(data)    → (weights_mat, price_df)
```

| Return element | Type | Shape | Notes |
|---|---|---|---|
| `weights_mat` | `np.ndarray` | `(n_days, n_stocks)` | target weight at each close; rows sum to ≤ 1 |
| `price_df` | `pd.DataFrame` (MultiIndex) | `(n_days, 2×n_stocks)` | `pd.concat({'close': close_df, 'open': open_df}, axis=1)` |
| `exec_at_close` *(optional)* | `np.ndarray[bool]` | `(n_days,)` | bars that execute at this-bar close (rare) |

**DO NOT pre-shift weights.** Runner shifts by 1 bar automatically: `w_curr[t] = weights[t-1]`.

### Weight Matrix Pattern

Use helpers for signal computation, weight building, and rebalance mask — this keeps `compute_signals` clean and lets `scan.py` sweep params without touching globals.

```python
def _compute_signal(close_df, param1=PARAM1):
    """Per-asset signal DataFrame. Examples: pct_change, rolling z-score, MA ratio."""
    # return close_df.pct_change(param1, fill_method=None)
    raise NotImplementedError

def _compute_weights(signal_df, param2, is_rebalance):
    """Signal → weight DataFrame. Two common patterns:
      A) Top-N equal weight:
            rank = signal_df.rank(axis=1, ascending=False, na_option='bottom')
            w = DataFrame(np.where(rank <= param2, 1/param2, 0), ...)
            w[signal_df.isna().all(axis=1)] = 0.0
      B) Proportional (z-score → normalize):
            pos = signal_df.clip(lower=0).fillna(0)
            w = pos.div(pos.sum(axis=1).where(pos.sum(axis=1) > 0), axis=0).fillna(0.0)
    Always finish with rebalance mask + ffill:
    """
    # ... build w ...
    w[~is_rebalance] = float('nan')
    return w.ffill().fillna(0.0)

def _rebalance_mask(idx, freq='W'):
    """Bool numpy array — True on last bar of each period. freq: 'W', 'M', 'D'."""
    import pandas as pd, numpy as np
    if freq == 'D':
        return np.ones(len(idx), dtype=bool)
    s = pd.Series(idx.to_period(freq), index=idx)
    return (s != s.shift(-1)).fillna(True).to_numpy()

def compute_signals(data, param1=PARAM1, param2=PARAM2):
    import pandas as pd
    close_df, open_df = data          # unpack in same order as fetch_data return

    signal       = _compute_signal(close_df, param1)
    is_rebalance = _rebalance_mask(close_df.index, freq='W')
    weights      = _compute_weights(signal, param2, is_rebalance)

    price_df = pd.concat({'close': close_df, 'open': open_df}, axis=1)
    return weights.values, price_df   # numpy array required — runner detects Type C by this
```

See `examples/tw100_foreign_zscore/strategy.py` (z-score proportional) and `examples/twstock_momentum/strategy.py` (top-N equal weight) for complete working implementations.

### Lookahead Bias Warning

`UNIVERSE` must be **fixed at backtest start** — determined only from information available before `START`.

- **WRONG:** `universe = all_stocks.nlargest(100, 'cumulative_net_buy')` — uses full-period data
- **RIGHT:** hardcode the universe from a historical constituent list, or use all IDs that ever appear in the data source

Let `compute_signals` do per-rebalance ranking using only the lookback window available at each date.

### What You Do NOT Need to Write

- Backtest loop — handled by `lib/runner.py`
- Weight shifting — runner shifts by 1 bar
- PnL computation — runner calls `precise_pnl` with the weight matrix
- Chart / stats / notify — runner handles all output

---

## END and WARMUP (Type A and C)

**`END = None`, always** — backtest, weight optimisation, and live all fetch to the
latest data. There is no cache-hit reason to pin a date: the monthly-delta cache (used
by nearly every fetcher — see lib/data.py for the one exact-range exception) keeps
past months cached and only re-fetches the current month. A pinned END is a production
bug — nothing on the live path overrides END, so a deployed strategy freezes its
signals at that date forever. `lib/quality_check.py` flags any non-None END as
CRITICAL and the runner refuses to backtest the file. Never write a dynamic expression
for END either — `None` already means "latest".

**WARMUP** (optional config) — number of bars to trim from the start of the backtest (warm-up period where indicators are not yet stable). Set to the sum of all rolling windows used. Runner automatically trims if present.

**run() call signature (Type A):**

```python
run(locals(), fetch_data, compute_signals, send_telegram_fn=make_sender())
```

Runner handles everything else: backtest, chart, stats, notify, live execution.

## Type C — Additional Rules

**Taiwan stock universe** must be sampled by sector — never take `[:N]` from the raw list (codes are ordered by sector, a head-slice concentrates in cement/food/textile). Use the sector-stratified sampling helper in `references/twstock.md`.
