"""
Utilities for 2D parameter scanning and plateau detection.

Usage:
    from lib.param_scan import find_plateau, plot_heatmap, percentile_thresholds
"""

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


def scan_grid(df, compute_signals_fn, row_vals, col_vals,
              row_param='entry_th', col_param='exit_th',
              fee=0.0005, valid_fn=None, warmup=0, **_):
    """
    Run a 2D parameter scan and return a Sharpe grid.

    Parameters
    ----------
    df                : DataFrame with OHLCV + any pre-computed auxiliary data
                        (e.g. realized_vol). compute_signals_fn is called on the
                        FULL df so rolling windows are accurate; PnL is computed
                        on df.iloc[warmup:] to skip the warm-up period.
    compute_signals_fn: strategy's compute_signals(df, **kwargs).
                        Must accept row_param and col_param as keyword arguments.
                        May return a plain pd.Series or a tuple (signal, settle):
                        - plain Series → next-bar open execution
                        - tuple        → settle (bool Series) is used as exec_shifted
                          (True on bar t means execute at open of bar t+1)
    row_vals          : iterable of row parameter values
    col_vals          : iterable of col parameter values
    row_param         : kwarg name for row values (default 'entry_th')
    col_param         : kwarg name for col values (default 'exit_th')
    fee               : per-trade fee rate (default 0.0005)
    valid_fn          : optional (row_val, col_val) → bool; skips invalid combos
                        defaults to row_val > col_val (entry > exit)
    warmup            : int, number of leading bars to skip from PnL computation
                        (set to max rolling window; default 0)

    Returns
    -------
    grid : 2D np.ndarray of Sharpe ratios (NaN for skipped/invalid combos)
    """
    import warnings
    warnings.filterwarnings('ignore', category=FutureWarning)
    from lib.analysis import precise_pnl, compute_stats

    if valid_fn is None:
        valid_fn = lambda r, c: r > c

    df_scan = df.iloc[warmup:] if warmup else df
    close_v = df_scan['Close'].values
    open_v  = df_scan['Open'].values
    n       = len(df_scan)

    row_vals = list(row_vals)
    col_vals = list(col_vals)
    grid     = np.full((len(row_vals), len(col_vals)), np.nan)

    for i, rv in enumerate(row_vals):
        for j, cv in enumerate(col_vals):
            if not valid_fn(rv, cv):
                continue
            result = compute_signals_fn(df, **{row_param: rv, col_param: cv})
            if isinstance(result, tuple):
                sig    = result[0].iloc[warmup:] if warmup else result[0]
                settle = result[1].iloc[warmup:] if warmup else result[1]
                exec_shifted     = np.zeros(n, dtype=bool)
                exec_shifted[1:] = settle.values.astype(bool)[:-1]
            else:
                sig          = result.iloc[warmup:] if warmup else result
                exec_shifted = np.zeros(n, dtype=bool)

            pos    = sig.ffill().fillna(0).values
            w_curr = np.empty(n); w_curr[0] = 0.0; w_curr[1:] = pos[:-1]
            w_prev = np.zeros(n)
            if n >= 2: w_prev[2:] = pos[:-2]

            pf_ret, *_ = precise_pnl(close_v, open_v, w_curr, w_prev, exec_shifted, fee)
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
    best_idx  : (row, col) tuple of best plateau cell
    nbr_mean  : 2D array of neighbourhood-average Sharpe
    best_row  : row_vals[best_idx[0]] if row_vals provided, else None
    best_col  : col_vals[best_idx[1]] if col_vals provided, else None
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

    best_idx = np.unravel_index(np.nanargmax(nbr_mean), nbr_mean.shape)
    best_row = row_vals[best_idx[0]] if row_vals is not None else None
    best_col = col_vals[best_idx[1]] if col_vals is not None else None
    return best_idx, nbr_mean, best_row, best_col


def plot_heatmap(
    grid,
    row_vals,
    col_vals,
    best_idx,
    row_label="ENTRY_TH",
    col_label="EXIT_TH",
    title="Sharpe Heatmap",
    output_path="/tmp/heatmap.png",
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
    output_path: save path

    Returns
    -------
    output_path
    """
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
    return output_path
