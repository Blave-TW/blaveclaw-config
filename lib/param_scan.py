"""
Utilities for 2D parameter scanning and plateau detection.

Usage:
    from lib.param_scan import percentile_thresholds, scan_grid, find_plateau, write_scan, plot_heatmap

Canonical scan flow: scan_grid → find_plateau → write_scan → plot_heatmap
"""

import json
import os
import time

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.patches import Rectangle


def percentile_thresholds(series, n_parts=9):
    """
    Split an indicator's distribution into n_parts equal percentile bands.
    Returns (entry_vals, exit_vals) — upper and lower halves of the percentile grid.

    Typical use: indicator threshold scan where entry > exit (dead zone strategy).

    Example (n_parts=9):
        percentiles at 10%, 20%, ..., 90% → 9 threshold candidates
        entry_vals = upper half (p50–p90)  → candidates for ENTRY_TH
        exit_vals  = lower half (p10–p50)  → candidates for EXIT_TH

    Parameters
    ----------
    series   : pd.Series — indicator values (NaNs ignored)
    n_parts  : int — number of equal bands (default 9)

    Returns
    -------
    entry_vals : np.ndarray — upper percentile thresholds
    exit_vals  : np.ndarray — lower percentile thresholds
    """
    s    = series.dropna()
    lo   = np.percentile(s, 5)
    hi   = np.percentile(s, 95)
    vals = np.round(np.linspace(lo, hi, n_parts), 3)
    mid  = len(vals) // 2
    entry_vals = vals[mid:]
    exit_vals  = vals[:mid + 1]

    p = np.percentile(s, [0, 25, 50, 75, 100])
    print(f"指標分佈 (n={len(s):,}): "
          f"min={p[0]:.3f}  p25={p[1]:.3f}  median={p[2]:.3f}  "
          f"p75={p[3]:.3f}  max={p[4]:.3f}")
    print(f"ENTRY_TH 候選 ({len(entry_vals)}): {list(entry_vals)}")
    print(f"EXIT_TH  候選 ({len(exit_vals)}):  {list(exit_vals)}")
    return entry_vals, exit_vals


def scan_grid(data, compute_signals_fn, row_vals, col_vals,
              row_param='entry_th', col_param='exit_th',
              fee=0.0005, valid_fn=None, warmup=0, **_):
    """
    Run a 2D parameter scan and return a Sharpe grid.
    Supports both Type A (single-symbol) and Type C (portfolio) strategies —
    auto-detected from compute_signals_fn's return type.

    Parameters
    ----------
    data              : Type A → DataFrame (OHLCV); Type C → tuple from fetch_data().
                        compute_signals_fn is called on the FULL data so rolling
                        windows are accurate; warmup trims the PnL only.
    compute_signals_fn: compute_signals(data, **kwargs) — must accept row_param and
                        col_param as keyword arguments.
                        Type A: returns pd.Series or (pd.Series, exec_at_close)
                        Type C: returns (weights_mat np.ndarray, price_df[, exec_at_close])
    row_vals          : iterable of row parameter values
    col_vals          : iterable of col parameter values
    row_param         : kwarg name for row values (default 'entry_th')
    col_param         : kwarg name for col values (default 'exit_th')
    fee               : per-trade fee rate (default 0.0005)
    valid_fn          : (row_val, col_val) → bool; skips invalid combos.
                        Type A threshold default: row > col (entry > exit).
                        Type C default: all combos valid (lambda r, c: True).
                        Pass explicitly to override.
    warmup            : leading bars to skip from PnL (= longest rolling window)

    Returns
    -------
    grid : 2D np.ndarray of Sharpe ratios (NaN for skipped/invalid combos and for
           combos that never traded — a do-nothing cell's Sharpe 0.0 would otherwise
           beat every losing cell and win the plateau in a bear period)
    """
    import warnings
    warnings.filterwarnings('ignore', category=FutureWarning)
    import pandas as pd
    from lib.analysis import precise_pnl, compute_stats

    row_vals = list(row_vals)
    col_vals = list(col_vals)
    grid     = np.full((len(row_vals), len(col_vals)), np.nan)

    for i, rv in enumerate(row_vals):
        for j, cv in enumerate(col_vals):
            result = compute_signals_fn(data, **{row_param: rv, col_param: cv})

            # ── Type C: (weights_mat, price_df[, exec_at_close]) ──────────────
            if isinstance(result, tuple) and isinstance(result[0], np.ndarray):
                if valid_fn is not None and not valid_fn(rv, cv):
                    continue
                weights_orig, price_df, *_opt = result
                w_orig = weights_orig[warmup:]
                pf     = price_df.iloc[warmup:]
                cl     = pf['close'].values
                op     = pf['open'].values
                n, k   = w_orig.shape
                w_curr = np.vstack([np.zeros((1, k)), w_orig[:-1]])
                w_prev = np.vstack([np.zeros((2, k)), w_orig[:-2]])
                exec_s = np.zeros(n, dtype=bool)
                if _opt:  # honour exec_at_close, same shift as lib/runner.py
                    ea = np.asarray(_opt[0], dtype=bool)[warmup:]
                    exec_s[1:] = ea[:-1]
                pf_ret, _, delta_w, _ = precise_pnl(cl, op, w_curr, w_prev, exec_s, fee)
                if not np.count_nonzero(np.nan_to_num(delta_w)):
                    continue  # 0 trades → leave NaN; Sharpe 0.0 would beat losing cells
                sharpe, *_ = compute_stats(pf_ret, pf['close'].index)

            # ── Type A: pd.Series or (pd.Series, exec_at_close) ──────────────
            else:
                _valid_fn = valid_fn if valid_fn is not None else (lambda r, c: r > c)
                if not _valid_fn(rv, cv):
                    continue
                if isinstance(result, tuple):
                    sig, settle = result[0], result[1]
                else:
                    sig, settle = result, None
                df_scan = data.iloc[warmup:] if warmup else data
                cl      = df_scan['Close'].values
                op      = df_scan['Open'].values
                n       = len(df_scan)
                sig_s   = sig.iloc[warmup:] if warmup else sig
                pos     = sig_s.ffill().fillna(0).values
                w_curr  = np.empty(n); w_curr[0] = 0.0; w_curr[1:] = pos[:-1]
                w_prev  = np.zeros(n)
                if n >= 2: w_prev[2:] = pos[:-2]
                if settle is not None:
                    settle_s = settle.iloc[warmup:] if warmup else settle
                    exec_s   = np.zeros(n, dtype=bool)
                    exec_s[1:] = settle_s.values.astype(bool)[:-1]
                else:
                    exec_s = np.zeros(n, dtype=bool)
                pf_ret, _, delta_w, _ = precise_pnl(cl, op, w_curr, w_prev, exec_s, fee)
                if not np.count_nonzero(np.nan_to_num(delta_w)):
                    continue  # 0 trades → leave NaN; Sharpe 0.0 would beat losing cells
                sharpe, *_ = compute_stats(pf_ret, df_scan.index)

            if np.isfinite(sharpe):
                grid[i, j] = sharpe

    return grid


def find_plateau(grid, row_vals=None, col_vals=None, window=1):
    """
    Find the most robust cell in a 2D Sharpe grid by neighbourhood-average.

    Parameters
    ----------
    grid      : 2D np.ndarray of Sharpe ratios (NaN for invalid combos)
    row_vals  : list of row parameter values (optional, returned for convenience)
    col_vals  : list of col parameter values (optional, returned for convenience)
    window    : neighbourhood radius (default 1 = 3×3 neighbourhood)

    Returns
    -------
    best_idx   : (row, col) tuple of best plateau cell
    nbr_mean   : 2D array of neighbourhood-average Sharpe  ← array, NOT a scalar
    best_row   : row_vals[best_idx[0]] if row_vals provided, else None
    best_col   : col_vals[best_idx[1]] if col_vals provided, else None
    best_sharpe: float — neighbourhood-average Sharpe at the best cell (use this for printing)

    Typical usage:
        best_idx, _, best_row, best_col, best_sharpe = find_plateau(grid, ROW_VALS, COL_VALS)
        print(f"Best: row={best_row}  col={best_col}  plateau_sharpe={best_sharpe:.3f}")
    """
    rows, cols = grid.shape
    nbr_mean   = np.full((rows, cols), np.nan)

    for i in range(rows):
        for j in range(cols):
            if np.isnan(grid[i, j]):
                continue
            nb = [
                grid[i + di, j + dj]
                for di in range(-window, window + 1)
                for dj in range(-window, window + 1)
                if 0 <= i + di < rows and 0 <= j + dj < cols
                and not np.isnan(grid[i + di, j + dj])
            ]
            if nb:
                nbr_mean[i, j] = np.mean(nb)

    if np.all(np.isnan(nbr_mean)):
        raise ValueError(
            "find_plateau: every grid cell is NaN — no parameter combo produced a single "
            "trade (or all were invalid). Widen the scan ranges toward the indicator's "
            "actual value range instead of picking from this grid."
        )
    best_idx    = np.unravel_index(np.nanargmax(nbr_mean), nbr_mean.shape)
    best_row    = row_vals[best_idx[0]] if row_vals is not None else None
    best_col    = col_vals[best_idx[1]] if col_vals is not None else None
    best_sharpe = float(nbr_mean[best_idx])
    return best_idx, nbr_mean, best_row, best_col, best_sharpe


def _grid_json(a):
    """2D array → nested lists, NaN/inf → None (JSON null), rounded to 4 dp."""
    return [[None if not np.isfinite(v) else round(float(v), 4) for v in row]
            for row in np.asarray(a, dtype=float)]


def _locate(val, vals):
    """Index of the grid value closest to val, or None if even that one is not
    (tightly) equal — so a fine step on large values can never match several cells."""
    arr = np.asarray(vals, dtype=float)
    k   = int(np.argmin(np.abs(arr - float(val))))
    return k if np.isclose(arr[k], float(val), rtol=1e-9, atol=1e-12) else None


def _finite_numbers(x, what):
    """Reject NaN/inf/bool/non-numeric axis or current values — a bool would serialise
    as true/false and a NaN would 400 the api upload, both leave the web tab blank."""
    items = list(np.ravel(np.asarray(x, dtype=object)))  # object: keep a stray bool as bool
    if any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, float, np.integer, np.floating))
           for v in items) or not np.isfinite(np.asarray(items, dtype=float)).all():
        raise ValueError(f"write_scan: {what} must be finite numbers, got {items}")


# api-side cap on each scan.json axis (api/openclaw/agent_strategies.py ingest) — a longer
# axis is refused there and the web 穩健參數 tab stays blank, so refuse it here first.
SCAN_MAX_AXIS = 40


def write_scan(grid, row_vals, col_vals, nbr_mean, best_idx, output_dir,
               row_param, col_param, fee, start, end, current=None, window=1):
    """
    Write the scan result to `strategies/<name>/scan.json` — the web workspace's
    穩健參數 (robust parameters) tab reads this file. **Call it after every
    scan_grid → find_plateau**, before plot_heatmap; a scan without scan.json is
    invisible to the web user.

    Parameters
    ----------
    grid       : 2D Sharpe grid from scan_grid (NaN = invalid / never traded)
    row_vals   : row parameter values (y-axis), same order as grid rows — max 40
    col_vals   : col parameter values (x-axis), same order as grid cols — max 40
                 (api cap; a bigger grid raises here instead of vanishing on the web)
    nbr_mean   : 2D neighbourhood-average grid returned by find_plateau
    best_idx   : (i, j) plateau cell returned by find_plateau
    output_dir : REQUIRED — 'strategies/{strategy_name}' (scan.json lands inside it)
    row_param  : the strategy CONSTANT name swept along rows, e.g. 'ENTRY_TH'
                 (the web builds "把 ENTRY_TH 改成 …" prompts from it — use the
                 module constant name, not the compute_signals kwarg)
    col_param  : the strategy constant name swept along cols, e.g. 'EXIT_TH'
    fee        : per-trade fee rate used for the scan (s.FEE)
    start, end : scan data range as 'YYYY-MM-DD' strings (end = df.index[-1] date;
                 pass None if unknown)
    current    : (row_val, col_val) — the constants the strategy file holds RIGHT
                 NOW, so the web can mark "you are here". Located on the grid by
                 np.isclose; off-grid values keep their vals with i/j = null.
                 None → written as null.
    window     : neighbourhood radius passed to find_plateau (default 1)

    Returns
    -------
    path of the written scan.json

    File shape (shared contract with api/web — do NOT rename keys):
      row_param, col_param, row_vals, col_vals,
      grid[rows][cols]      Sharpe, NaN → null
      nbr_mean[rows][cols]  neighbourhood mean, NaN → null
      peak     {i, j, sharpe}  global max of grid (the fragile optimum)
      plateau  {i, j, sharpe}  find_plateau best cell; sharpe = neighbourhood mean
      current  {i, j, vals} | null
      fee, start, end, window, generated_at (epoch seconds)
    """
    # inf is as unusable as NaN for the web (and for nanargmax) — treat it as a hole.
    grid     = np.where(np.isfinite(grid), np.asarray(grid, dtype=float), np.nan)
    nbr_mean = np.where(np.isfinite(nbr_mean), np.asarray(nbr_mean, dtype=float), np.nan)
    _finite_numbers(row_vals, 'row_vals')
    _finite_numbers(col_vals, 'col_vals')
    if current is not None:
        _finite_numbers(current, 'current')
    # round(6): keep the axis labels the web shows free of float noise (0.30000000000000004)
    row_list = [round(v, 6) if isinstance(v, float) else v for v in np.asarray(row_vals).tolist()]
    col_list = [round(v, 6) if isinstance(v, float) else v for v in np.asarray(col_vals).tolist()]
    if len(row_list) > SCAN_MAX_AXIS or len(col_list) > SCAN_MAX_AXIS:
        raise ValueError(
            f"write_scan: grid {len(row_list)}×{len(col_list)} exceeds the api limit of "
            f"{SCAN_MAX_AXIS}×{SCAN_MAX_AXIS} — the web would show nothing. Use a coarser "
            "step or a narrower range and rescan."
        )
    if grid.shape != (len(row_list), len(col_list)) or nbr_mean.shape != grid.shape:
        raise ValueError(
            f"write_scan: grid {grid.shape} / nbr_mean {nbr_mean.shape} must be "
            f"({len(row_list)}, {len(col_list)}) = (len(row_vals), len(col_vals))"
        )
    # Contract: peak/plateau are always complete objects with finite sharpe. An all-NaN
    # grid (no combo ever traded) therefore writes NOTHING — find_plateau already raised
    # upstream; never write a scan.json with null peak/plateau.
    if np.all(np.isnan(grid)):
        raise ValueError("write_scan: every grid cell is NaN — nothing to write")

    pi, pj = np.unravel_index(np.nanargmax(grid), grid.shape)
    bi, bj = int(best_idx[0]), int(best_idx[1])
    if not (0 <= bi < grid.shape[0] and 0 <= bj < grid.shape[1]) or not np.isfinite(nbr_mean[bi, bj]):
        raise ValueError(f"write_scan: best_idx {best_idx} is off-grid or has no neighbourhood mean")
    doc = {
        'row_param': row_param, 'col_param': col_param,
        'row_vals': row_list, 'col_vals': col_list,
        'grid':     _grid_json(grid),
        'nbr_mean': _grid_json(nbr_mean),
        'peak':     {'i': int(pi), 'j': int(pj), 'sharpe': round(float(grid[pi, pj]), 4)},
        'plateau':  {'i': bi, 'j': bj, 'sharpe': round(float(nbr_mean[bi, bj]), 4)},
        'current':  None,
        'fee': float(fee), 'start': start, 'end': end, 'window': int(window),
        'generated_at': int(time.time()),
    }
    if current is not None:
        rv, cv = current  # exactly two values — (row_val, col_val); vals is always length 2
        i, j = _locate(rv, row_list), _locate(cv, col_list)
        doc['current'] = {'i': i if (i is not None and j is not None) else None,
                          'j': j if (i is not None and j is not None) else None,
                          'vals': [np.asarray(rv).item(), np.asarray(cv).item()]}

    os.makedirs(str(output_dir), exist_ok=True)
    path = os.path.join(str(output_dir), 'scan.json')
    # Atomic (same pattern as lib/report.py): the uploader may read mid-write, and a
    # half-written scan.json would break the web tab until the next scan. allow_nan=False
    # so a NaN that slipped past _grid_json fails here with a stack trace, not in the api.
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
    print(f"Scan written: {path}  (peak {row_param}={row_list[pi]}, {col_param}={col_list[pj]} "
          f"Sharpe={grid[pi, pj]:.2f}; plateau {row_param}={row_list[bi]}, {col_param}={col_list[bj]} "
          f"nbr Sharpe={nbr_mean[bi, bj]:.2f})")
    return path


def plot_heatmap(
    grid,
    row_vals,
    col_vals,
    best_idx,
    row_label="ENTRY_TH",
    col_label="EXIT_TH",
    title="Sharpe Heatmap",
    output_path=None,
    send_telegram=True,
):
    """
    Plot a Sharpe heatmap with the plateau cell highlighted.

    Parameters
    ----------
    grid       : 2D np.ndarray of Sharpe ratios
    row_vals   : list of row parameter values (y-axis labels)
    col_vals   : list of col parameter values (x-axis labels)
    best_idx   : (row, col) tuple from find_plateau
    row_label  : y-axis label (default 'ENTRY_TH')
    col_label  : x-axis label (default 'EXIT_TH')
    title      : chart title
    output_path: REQUIRED — use 'strategies/{strategy_name}/heatmap.png'
    send_telegram: if True (default), broadcast the saved heatmap to Telegram
                 via lib.notify.send_photo — mirrors run()'s auto-send of
                 pnl.png so scan charts reach the user without manual sending

    Returns
    -------
    output_path
    """
    if output_path is None:
        raise ValueError(
            "plot_heatmap() requires output_path — "
            "use output_path='strategies/{strategy_name}/heatmap.png'"
        )
    n_rows, n_cols = len(row_vals), len(col_vals)
    bi, bj         = best_idx

    fig, ax = plt.subplots(figsize=(max(8, n_cols * 1.2), max(6, n_rows * 1.0)))

    from matplotlib.colors import TwoSlopeNorm
    masked = np.ma.masked_invalid(grid)
    valid  = grid[~np.isnan(grid)]
    vmin   = min(np.nanmin(valid), -0.01) if len(valid) else -1
    vmax   = max(np.nanmax(valid),  0.01) if len(valid) else  1
    norm   = TwoSlopeNorm(vmin=vmin, vcenter=0, vmax=vmax)
    im     = ax.imshow(masked, aspect="auto", cmap="RdYlGn",
                       origin="upper", norm=norm)
    plt.colorbar(im, ax=ax, label="Sharpe")

    ax.set_xticks(range(n_cols)); ax.set_xticklabels([str(v) for v in col_vals], fontsize=8)
    ax.set_yticks(range(n_rows)); ax.set_yticklabels([str(v) for v in row_vals], fontsize=8)
    ax.set_xlabel(col_label, fontsize=9)
    ax.set_ylabel(row_label, fontsize=9)

    for i in range(n_rows):
        for j in range(n_cols):
            v = grid[i, j]
            if not np.isnan(v):
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=7)

    ax.add_patch(Rectangle((bj - 0.5, bi - 0.5), 1, 1,
                            linewidth=2.5, edgecolor="white", facecolor="none"))
    ax.set_title(
        f"{title} — plateau: {row_label}={row_vals[bi]}, {col_label}={col_vals[bj]} "
        f"(Sharpe={grid[bi, bj]:.2f})",
        fontsize=10,
    )

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Heatmap saved: {output_path}")

    # Mirror the heatmap into the web workspace chat (no-op off web), regardless of
    # the Telegram gate below — this is the chart the user asked to see.
    from lib.notify import report_photo_web
    report_photo_web(output_path)

    if send_telegram:
        try:
            from lib.notify import send_photo
            send_photo(output_path)
        except Exception as e:
            print(f"Error: heatmap saved but Telegram send failed: {e}")

    return output_path
