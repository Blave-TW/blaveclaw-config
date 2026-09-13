"""
Walk-forward out-of-sample validation: re-pick the parameters in every training
window, apply them to the test window that follows, stitch the test windows into
one out-of-sample series.

Usage:
    from lib.walk_forward import run_walk_forward, default_windows

    lookback_days, step_days = default_windows(total_days)   # 1095/30, 365/30 or 4:1 by length
    run_walk_forward(df, s.compute_signals, ENTRY_VALS, EXIT_VALS,
                     output_dir='strategies/<name>', row_param='ENTRY_TH',
                     col_param='EXIT_TH', fee=s.FEE, warmup=s.WARMUP,
                     current=(s.ENTRY_TH, s.EXIT_TH))

Writes `strategies/<name>/wf.json` — the web workspace's 樣本外驗證 tab reads it;
a run that skips it is invisible to a web user. Rolling windows only (the training
window slides forward by one step each run); anchored windows are not offered.

Two passes, so the whole thing costs about one parameter scan:

  pass 1  every combo's signals are computed ONCE on the full history, then each
          run's training slice is priced from that one signal pass → a
          runs × rows × cols Sharpe grid. No position matrix is kept: one combo's
          arrays live at a time (a 400-combo × full-history float64 matrix would
          not fit a 2C/4GB Starter).
  pass 2  only the run winners (≤ n_runs combos, usually fewer) are recomputed;
          each run's test slice is cut from its own winner's position series and
          the slices are concatenated into ONE series, priced by a single
          precise_pnl over the whole out-of-sample stretch.

Three semantics this module fixes (they are not in the design spec):

1. **Picking uses the neighbourhood mean, reporting uses the cell itself.**
   Each run picks its parameters with `find_plateau` (3×3 neighbourhood average,
   same rule as the parameter scan), but the reported `train_sharpe` — and hence
   `is_stats['Sharpe Ratio']`, the denominator of the walk-forward efficiency — is
   `grid[i, j]` of the picked cell, NOT its neighbourhood mean. The plateau is a
   selection criterion; the cell's own Sharpe is the performance. A neighbourhood
   mean is structurally lower than the cell it is centred on, so using it as the
   denominator would inflate WFE and make the whole tab read optimistic.

2. **Seam positions come from the new run's own signal series.** When run k picks
   different parameters from run k−1, the first bar of run k's test window carries
   whatever run k's parameters say to hold there — the position series is cut, not
   reset. That is what actually happens when you switch parameters: the strategy's
   own logic decides what it holds now. Forcing every seam flat would invent n−1
   exits nobody traded. The switching cost is charged automatically: the stitched
   series changes weight at the seam and `precise_pnl` bills the fee for it.
   Per-run test numbers are sliced out of that one stitched series too, never
   re-priced per run, so they add up to the stitched totals.

3. **Warmup is applied to every training slice, not just the first.** Signals are
   computed on the full history (rolling windows stay accurate) but each training
   window's PnL skips the first `warmup` bars of that window, exactly as
   `scan_grid` skips the head of the data. Without it the first run trains on bars
   where the indicator has not warmed up and picks noise; applying it to every run
   keeps the runs the same effective length and therefore comparable. Test windows
   are NOT trimmed — they are already warm and must stay contiguous to stitch.
   This is also why no embargo gap is needed: the test window sits immediately
   after the training window it was picked from, and nothing after it is used.

The scan axes are fixed for the whole validation — no per-run `on_edge` /
`extend_axis`. Every run must land on the same lattice or the drift grid has no
axis to plot against.
"""

import json
import os
import time

import numpy as np

from lib.param_scan import SCAN_MAX_AXIS, find_plateau, _finite_numbers

# Rolling window defaults (see default_windows), three tiers re-optimised monthly:
# 1095/30 when the history cuts WF_MIN_RUNS runs out of it — a three-year training
# window is closer to how users pick one fixed parameter set from the whole backtest;
# 365/30 below that, because going straight to the ratio formula would hand 455–1184-day
# histories a training window SHORTER than a year; the 4:1 ratio formula (step =
# total/12 → 8 runs) only when even 365/30 cannot cut WF_MIN_RUNS runs. The step stays
# 30 on every tier: the run count only sets how often the pick is redone — the
# out-of-sample span is total days minus the training window either way.
# The web's wfPreset (agent/workspace.html) is meant to prefill the same numbers —
# change both together.
WF_LONG_LOOKBACK_DAYS = 1095
WF_DEFAULT_LOOKBACK_DAYS = 365
WF_DEFAULT_STEP_DAYS = 30
WF_TRAIN_MULT = 4
WF_STEP_DIVISOR = 12
WF_STEP_MIN_DAYS = 30
# Fewer runs than this and there is nothing to read: the run table and the drift
# grid show one or two picks, and the stitched out-of-sample series is one or two
# test windows long.
WF_MIN_RUNS = 3
# Sanity bound on runs[], mirrored by the api (which refuses a longer list — the tab
# would then stay blank). Not a workload limit: a run costs ~225 bytes in wf.json and
# pass 2 is cheap; 1095/30 on daily TW stock history from 1994 is ~360 runs as of 2026.
WF_MAX_RUNS = 1000
# In-sample Sharpe below this → no WFE at all (`wfe: null`). Dividing by a tiny or
# negative denominator inverts the diagnosis: a strategy that was never strong
# in-sample would score a high "efficiency" for giving nothing back.
WF_MIN_DENOM = 0.25
# Points in the out-of-sample curve after the daily downsample below. 4000 is over a
# decade of daily bars — the api caps the same array at the same number, so the lib
# must never hand it more (an over-long array would drop the whole wf block there and
# leave the tab blank).
WF_MAX_POINTS = 4000


def default_windows(total_days):
    """(lookback_days, step_days) for `total_days` of history — the default when the
    caller passes no window.

    1095 / 30 whenever the history can cut at least WF_MIN_RUNS runs out of it
    (≥ 1185 days), else 365 / 30 when that can (≥ 455 days). Shorter histories fall
    back to the ratio formula: step = max(30, floor(total/12)), lookback = 4 × step
    (≈ 8 runs). floor, never round: at 730 days round(60.83) = 61 yields 7 runs,
    floor yields the intended 8.
    """
    total_days = int(total_days)
    for lookback in (WF_LONG_LOOKBACK_DAYS, WF_DEFAULT_LOOKBACK_DAYS):
        if total_days >= lookback + WF_MIN_RUNS * WF_DEFAULT_STEP_DAYS:
            return lookback, WF_DEFAULT_STEP_DAYS
    step = max(WF_STEP_MIN_DAYS, total_days // WF_STEP_DIVISOR)
    return WF_TRAIN_MULT * step, step


def _v(x, digits=4):
    """JSON-safe number: None for anything not finite (allow_nan=False is on)."""
    if x is None:
        return None
    x = float(x)
    return None if not np.isfinite(x) else round(x, digits)


def _run_windows(index, lookback_days, step_days, warmup):
    """Bar-index windows for each run: (t0, t1, t2) with training = [t0+warmup, t1)
    and test = [t1, t2). Test windows are contiguous (t1 of run k+1 == t2 of run k),
    which is what lets them be stitched into one series."""
    import pandas as pd

    total_days = int((index[-1] - index[0]).days)
    if lookback_days <= 0 or step_days <= 0:
        raise ValueError(f"walk_forward: lookback_days / step_days must be positive, "
                         f"got {lookback_days} / {step_days}")
    n_runs = int((total_days - lookback_days) // step_days) if total_days > lookback_days else 0
    if n_runs < WF_MIN_RUNS:
        raise ValueError(
            f"walk_forward: 資料只夠 {n_runs} 輪,至少要 {WF_MIN_RUNS} 輪才看得出樣本外表現 "
            f"({total_days} days of data, lookback {lookback_days} + {n_runs}×{step_days}). "
            "Shorten the training window / step, or fetch more history."
        )
    if n_runs > WF_MAX_RUNS:
        raise ValueError(
            f"walk_forward: {n_runs} runs exceeds the {WF_MAX_RUNS}-run cap (the api refuses "
            "a longer runs[]) — use a longer step."
        )
    t_zero = index[0]
    windows = []
    for k in range(n_runs):
        train_from = t_zero + pd.Timedelta(days=k * step_days)
        train_to = train_from + pd.Timedelta(days=lookback_days)
        test_to = train_to + pd.Timedelta(days=step_days)
        t0 = int(index.searchsorted(train_from, 'left'))
        t1 = int(index.searchsorted(train_to, 'left'))
        t2 = int(index.searchsorted(test_to, 'left'))
        if t1 - (t0 + warmup) < 2 or t2 - t1 < 2:
            raise ValueError(
                f"walk_forward: run {k + 1} has too few bars (train "
                f"{t1 - t0 - warmup}, test {t2 - t1}) — the windows are shorter than the "
                f"warmup ({warmup}) or the data has a gap there. Use a longer training window."
            )
        windows.append((t0, t1, t2))
    tail_days = total_days - lookback_days - n_runs * step_days
    return windows, max(0, tail_days), total_days


def _unpack(result, data):
    """One combo's signals → (pos, exec_raw, close_v, open_v, index) in FULL-history
    space, for Type A (pd.Series) and Type C (weights matrix) alike. Mirrors
    scan_grid's two branches; `exec_raw` is the unshifted exec_at_close mask."""
    if isinstance(result, tuple) and isinstance(result[0], np.ndarray):
        weights, price_df, *opt = result
        pos = np.asarray(weights, dtype=float)
        exec_raw = (np.asarray(opt[0], dtype=bool) if opt
                    else np.zeros(len(price_df), dtype=bool))
        return pos, exec_raw, price_df['close'].values, price_df['open'].values, price_df.index
    sig, settle = (result[0], result[1]) if isinstance(result, tuple) else (result, None)
    pos = sig.ffill().fillna(0).values.astype(float)
    exec_raw = (np.asarray(settle.values, dtype=bool) if settle is not None
                else np.zeros(len(data), dtype=bool))
    return pos, exec_raw, data['Close'].values, data['Open'].values, data.index


def _price_pnl(pos, exec_raw, close_v, open_v, fee):
    """precise_pnl over one already-cut stretch of bars, flat at its first bar —
    the same 2-lag convention scan_grid and lib/runner.py use. Returns
    (pf_ret, delta_w)."""
    from lib.analysis import precise_pnl

    n = len(pos)
    if pos.ndim > 1:
        k = pos.shape[1]
        w_curr = np.vstack([np.zeros((1, k)), pos[:-1]])
        w_prev = np.vstack([np.zeros((2, k)), pos[:-2]])
    else:
        w_curr = np.empty(n)
        w_curr[0] = 0.0
        w_curr[1:] = pos[:-1]
        w_prev = np.zeros(n)
        if n >= 2:
            w_prev[2:] = pos[:-2]
    exec_s = np.zeros(n, dtype=bool)
    exec_s[1:] = np.asarray(exec_raw, dtype=bool)[:-1]
    pf_ret, _, delta_w, _ = precise_pnl(close_v, open_v, w_curr, w_prev, exec_s, fee)
    return pf_ret, delta_w


def _window_stats(pos, exec_raw, close_v, open_v, index, a, b, fee):
    """(sharpe, ann_ret, mdd, trades) for bars [a, b), or None when the combo never
    traded in that window — a do-nothing window's Sharpe 0.0 would otherwise beat
    every losing combo and win the plateau in a bear stretch (same rule as
    scan_grid)."""
    from lib.analysis import compute_stats

    pf_ret, delta_w = _price_pnl(pos[a:b], exec_raw[a:b], close_v[a:b], open_v[a:b], fee)
    trades = int(np.count_nonzero(np.nan_to_num(delta_w)))
    if not trades:
        return None
    sharpe, _, _, mdd, ann_ret = compute_stats(pf_ret, index[a:b])
    return sharpe, ann_ret, mdd, trades


def _oos_curve(index, pf_ret):
    """(dates, cum) for the out-of-sample chart: cumulative return in PERCENT, one
    point per calendar day (the last bar of each day). The tab draws a 220px-high
    line — intraday bars are invisible there and would put 200k points in the
    report. Thinned further if a decade-plus of daily points still overflows."""
    cum = (np.cumprod(1.0 + np.nan_to_num(pf_ret)) - 1.0) * 100.0
    day = np.asarray(index.normalize().values, dtype='datetime64[D]')
    keep = np.flatnonzero(np.append(day[1:] != day[:-1], True))
    if len(keep) > WF_MAX_POINTS:
        pick = np.unique(np.round(np.linspace(0, len(keep) - 1, WF_MAX_POINTS)).astype(int))
        keep = keep[pick]
    return [str(day[i]) for i in keep], [_v(cum[i]) for i in keep]


def _date(index, i):
    return index[i].strftime('%Y-%m-%d')


def _split_runs(train_sharpes):
    """(valid run indices, excluded run numbers). A run whose training window Sharpe
    is ≤ 0 failed: it still produced a test window — so it stays in the run table, in
    the drift grid and in the profitable-window ratio — but it must not join the
    in-sample average. Averaging in a zero or negative denominator inverts the whole
    diagnosis (a strategy that was never strong in-sample would score a high WFE for
    giving nothing back)."""
    valid = [k for k, s in enumerate(train_sharpes)
             if s is not None and np.isfinite(s) and s > 0]
    seen = set(valid)
    return valid, [k + 1 for k in range(len(train_sharpes)) if k not in seen]


def _wfe(oos_sharpe, is_sharpe):
    """Walk-forward efficiency = out-of-sample Sharpe ÷ in-sample Sharpe, or None when
    the denominator is too weak to divide by (WF_MIN_DENOM). The second of the two
    denominator guards; _split_runs is the first."""
    if is_sharpe is None or not np.isfinite(is_sharpe) or is_sharpe < WF_MIN_DENOM:
        return None
    return _v(oos_sharpe / is_sharpe)


def _stitch(picks, windows, load_pos):
    """Concatenate each run's test window, taken from that run's OWN winner, into one
    out-of-sample series — see the module docstring, point 2. `load_pos(i, j)` returns
    one combo's `(pos, exec_raw, close_v, open_v)` in full-history space; it is called
    once per DISTINCT winner (8 runs usually pick 5–6 pairs), not once per run.

    Returns (pos_oos, exec_oos, close_oos, open_oos, oos_a, oos_b). The test windows
    are contiguous by construction, so the result covers bars [oos_a, oos_b) with no
    holes and the seam bar simply carries the incoming run's position.
    """
    oos_a, oos_b = windows[0][1], windows[-1][2]
    groups = {}
    for k, ij in enumerate(picks):
        groups.setdefault(ij, []).append(k)
    pos_oos = exec_oos = close_oos = open_oos = None
    for (i, j), ks in groups.items():
        pos, exec_raw, close_v, open_v = load_pos(i, j)
        if pos_oos is None:
            pos_oos = np.zeros_like(pos[oos_a:oos_b])
            exec_oos = np.zeros(oos_b - oos_a, dtype=bool)
            close_oos, open_oos = close_v[oos_a:oos_b], open_v[oos_a:oos_b]
        for k in ks:
            _t0, t1, t2 = windows[k]
            pos_oos[t1 - oos_a:t2 - oos_a] = pos[t1:t2]
            exec_oos[t1 - oos_a:t2 - oos_a] = exec_raw[t1:t2]
    return pos_oos, exec_oos, close_oos, open_oos, oos_a, oos_b


def run_walk_forward(data, compute_signals_fn, row_vals, col_vals, output_dir,
                     row_param, col_param, fee=0.0005,
                     row_kw='entry_th', col_kw='exit_th',
                     lookback_days=None, step_days=None,
                     valid_fn=None, warmup=0, window=1, current=None):
    """
    Run the walk-forward validation and write `<output_dir>/wf.json`.

    Parameters
    ----------
    data              : Type A → DataFrame (OHLCV); Type C → the tuple fetch_data()
                        returns. Passed to compute_signals_fn untouched, exactly as
                        scan_grid does.
    compute_signals_fn: compute_signals(data, **kwargs) — must accept row_kw and
                        col_kw as keyword arguments. Type A returns pd.Series or
                        (pd.Series, exec_at_close); Type C returns
                        (weights_mat, price_df[, exec_at_close]).
    row_vals, col_vals: the scan axes, same lattice as the parameter scan (build them
                        with nice_grid / percentile_thresholds anchored on the
                        strategy's current constants). Max 40 each — api cap.
    output_dir        : REQUIRED — 'strategies/{strategy_name}' (wf.json lands inside).
    row_param,
    col_param         : the strategy CONSTANT names ('ENTRY_TH', 'EXIT_TH') — the web
                        compares them against the code to tell a stale result.
    row_kw, col_kw    : the compute_signals kwarg names (default 'entry_th'/'exit_th').
    lookback_days,
    step_days         : training window / re-optimisation step in days. None → the
                        default_windows() tiers (1095 / 30, 365 / 30, 4:1 ratio by length).
    valid_fn          : (row_val, col_val) → bool, as in scan_grid (Type A default:
                        row > col).
    warmup            : leading bars every training slice skips — see the module
                        docstring, point 3. Pass the strategy's WARMUP.
    window            : find_plateau neighbourhood radius (default 1 = 3×3).
    current           : (row_val, col_val) the strategy file holds right now.

    Returns
    -------
    path of the written wf.json
    """
    import warnings
    warnings.filterwarnings('ignore', category=FutureWarning)
    from lib.analysis import compute_stats
    try:
        from lib.progress import Progress
    except ImportError:  # half-updated workspace — fail open, no progress lines
        class Progress:
            def __init__(self, *a, **k): pass
            def tick(self, n=1): pass

    # .tolist() (not list()): a numpy axis would keep np.int64 / np.float64 elements,
    # which json.dump refuses — ten minutes into the run, with nothing written. Same
    # reason write_scan does it. round(10) matches nice_grid / write_scan so the axis
    # labels survive the round trip free of float noise (0.30000000000000004).
    row_vals = [round(v, 10) if isinstance(v, float) else v
                for v in np.asarray(row_vals).tolist()]
    col_vals = [round(v, 10) if isinstance(v, float) else v
                for v in np.asarray(col_vals).tolist()]
    _finite_numbers(row_vals, 'row_vals')
    _finite_numbers(col_vals, 'col_vals')
    if current is not None:
        _finite_numbers(current, 'current')
    if len(row_vals) > SCAN_MAX_AXIS or len(col_vals) > SCAN_MAX_AXIS:
        raise ValueError(
            f"run_walk_forward: grid {len(row_vals)}×{len(col_vals)} exceeds the api limit "
            f"of {SCAN_MAX_AXIS}×{SCAN_MAX_AXIS} — the web would show nothing."
        )
    cells = [(i, rv, j, cv) for i, rv in enumerate(row_vals) for j, cv in enumerate(col_vals)
             if valid_fn is None or valid_fn(rv, cv)]
    if not cells:
        raise ValueError("run_walk_forward: valid_fn rejected every combo")

    # One probe pass buys the bar index before the long loop: Type C only learns its
    # index from price_df, and a "資料只夠 2 輪" must not arrive ten minutes into the
    # scan. The result is reused, so the probe costs nothing.
    probe_key = (cells[0][0], cells[0][2])
    probe = compute_signals_fn(data, **{row_kw: cells[0][1], col_kw: cells[0][3]})
    index = _unpack(probe, data)[4]
    if lookback_days is None or step_days is None:
        lookback_days, step_days = default_windows(int((index[-1] - index[0]).days))
    lookback_days, step_days = int(lookback_days), int(step_days)
    windows, tail_days, total_days = _run_windows(index, lookback_days, step_days, warmup)
    n_runs = len(windows)

    # runs × rows × cols, one plane per statistic — a few hundred KB at the 40×40 cap,
    # which is what lets pass 2 report the winners' training numbers without a rerun.
    shape = (n_runs, len(row_vals), len(col_vals))
    g_sharpe, g_ann, g_mdd = (np.full(shape, np.nan) for _ in range(3))
    g_trades = np.zeros(shape, dtype=int)

    progress = Progress('walk-forward', len(cells), 'cells')
    for i, rv, j, cv in cells:
        result = probe if (i, j) == probe_key else compute_signals_fn(
            data, **{row_kw: rv, col_kw: cv})
        progress.tick()
        pos, exec_raw, close_v, open_v, _ = _unpack(result, data)
        # scan_grid's default when no valid_fn was given and the strategy is Type A
        if valid_fn is None and pos.ndim == 1 and not rv > cv:
            continue
        for k, (t0, t1, _t2) in enumerate(windows):
            st = _window_stats(pos, exec_raw, close_v, open_v, index, t0 + warmup, t1, fee)
            if st is None:
                continue
            g_sharpe[k, i, j], g_ann[k, i, j], g_mdd[k, i, j], g_trades[k, i, j] = st
    probe = None  # release the probe combo's arrays before pass 2

    picks = []
    for k in range(n_runs):
        try:
            best_idx, _nbr, _br, _bc, _nbr_sharpe = find_plateau(
                g_sharpe[k], row_vals, col_vals, window)
        except ValueError as e:
            # The usual cause is a training window too short to trade in, not a
            # broken grid: lookback minus warmup leaves a handful of bars.
            raise ValueError(f"walk_forward: run {k + 1} has no usable training window — "
                             f"training window {lookback_days}d minus warmup {warmup} bars may be "
                             f"too short to produce a trade; lengthen lookback_days ({e})") from e
        picks.append((int(best_idx[0]), int(best_idx[1])))

    # ── pass 2: recompute the winners only, stitch their test windows ────────────
    def load_pos(i, j):
        return _unpack(compute_signals_fn(
            data, **{row_kw: row_vals[i], col_kw: col_vals[j]}), data)[:4]

    pos_oos, exec_oos, close_oos, open_oos, oos_a, oos_b = _stitch(picks, windows, load_pos)

    oos_index = index[oos_a:oos_b]
    pf_ret, delta_w = _price_pnl(pos_oos, exec_oos, close_oos, open_oos, fee)
    oos_sharpe, _, _, oos_mdd, oos_ann = compute_stats(pf_ret, oos_index)
    oos_trades = int(np.count_nonzero(np.nan_to_num(delta_w)))
    dates, cum = _oos_curve(oos_index, pf_ret)

    # Every per-run test number is cut out of the ONE stitched series priced above —
    # never re-priced per run — so the run table's returns compound to the stitched
    # total and its trade counts sum to it exactly.
    runs, train_sharpes = [], []
    for k, ((t0, t1, t2), (i, j)) in enumerate(zip(windows, picks)):
        train_sharpes.append(float(g_sharpe[k, i, j]))
        a, b = t1 - oos_a, t2 - oos_a
        seg = pf_ret[a:b]
        runs.append({
            'k': k + 1,
            'train_start': _date(index, t0 + warmup), 'train_end': _date(index, t1 - 1),
            'test_start': _date(index, t1), 'test_end': _date(index, t2 - 1),
            'params': [row_vals[i], col_vals[j]],
            'train_sharpe': _v(train_sharpes[-1]),
            'test_sharpe': _v(compute_stats(seg, oos_index[a:b])[0]),
            'test_return': _v((float(np.prod(1.0 + np.nan_to_num(seg))) - 1.0) * 100),
            'trades': int(np.count_nonzero(np.nan_to_num(delta_w[a:b]))),
        })

    valid, excluded = _split_runs(train_sharpes)

    def _mean(plane):
        xs = [plane[k, picks[k][0], picks[k][1]] for k in valid]
        return float(np.mean(xs)) if xs else None

    is_mean_sharpe = _mean(g_sharpe)
    is_mean_mdd = _mean(g_mdd)
    wfe = _wfe(oos_sharpe, is_mean_sharpe)

    doc = {
        'row_param': row_param, 'col_param': col_param,
        'row_vals': row_vals, 'col_vals': col_vals,
        'current': ([np.asarray(current[0]).item(), np.asarray(current[1]).item()]
                    if current is not None else None),
        'lookback_days': lookback_days, 'step_days': step_days,
        'n_runs': n_runs, 'tail_days': int(tail_days),
        'fee': float(fee),
        'start': _date(index, 0), 'end': _date(index, len(index) - 1),
        'oos': {'dates': dates, 'cum': cum},
        'oos_stats': {
            'Sharpe Ratio': _v(oos_sharpe),
            'Ann. Return [%]': _v(oos_ann * 100),
            'Max Drawdown [%]': _v(-abs(oos_mdd) * 100),
            'Trades': oos_trades,
        },
        # The average across the runs that were NOT excluded. 'Sharpe Ratio' here is
        # the mean of the picked CELLS' own Sharpe (never the neighbourhood mean) and
        # is the denominator of `wfe` — see the module docstring, point 1.
        'is_stats': {
            'Sharpe Ratio': _v(is_mean_sharpe),
            'Ann. Return [%]': _v(_mean(g_ann) * 100 if valid else None),
            'Max Drawdown [%]': _v(-abs(is_mean_mdd) * 100 if is_mean_mdd is not None else None),
            'Trades': _v(_mean(g_trades)),
        },
        'wfe': wfe,
        'excluded_runs': excluded,
        'runs': runs,
        'generated_at': int(time.time()),
    }

    os.makedirs(str(output_dir), exist_ok=True)
    path = os.path.join(str(output_dir), 'wf.json')
    # Atomic, same as write_scan: the uploader may read mid-write and a half-written
    # wf.json would break the tab until the next validation. allow_nan=False so a NaN
    # that slipped past _v fails here with a stack trace instead of 400ing the api.
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    print(f"Walk-forward written: {path}  ({n_runs} runs, train {lookback_days}d / step "
          f"{step_days}d, OOS Sharpe {_v(oos_sharpe)} vs IS {_v(is_mean_sharpe)}, "
          f"WFE {wfe if wfe is not None else '—'}"
          f"{', excluded runs ' + ','.join(map(str, excluded)) if excluded else ''})")
    return path
