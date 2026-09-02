"""
Statistical validation utilities for strategy backtesting.

Usage:
    from lib.validation import mcpt, plot_mcpt, write_mcpt_to_stats

Canonical MCPT flow (validate.py): mcpt → write_mcpt_to_stats → plot_mcpt
"""

import json
import os
import tempfile
import numpy as np
import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def mcpt(
    close,
    position,
    n=2000,
    fee=0.0005,
    target_vol=0.30,
    max_lev=2.0,
    vol_window=720,
    periods_per_year=8760,
):
    """
    Monte Carlo Permutation Test (MCPT) for strategy edge.

    Permutes the forward return series (not positions) n times, keeping
    position fixed so fee drag and vol-scaling are identical across all
    permutations.

    Null hypothesis: the return periods selected by this strategy are no
    better than random.

    p-value: fraction of permuted Sharpes >= actual Sharpe.
    p < 0.05 → statistically significant edge at 95% confidence.

    Why permute returns, not positions?
    Shuffling a binary position array creates ~N×p×(1-p) transitions vs
    the strategy's far smaller count. With fee=0.0005 that produces 30-40×
    more fee drag on every permutation, forcing all permuted Sharpes deeply
    negative and biasing the p-value regardless of real edge.

    Parameters
    ----------
    close            : 1D array of close prices
    position         : 1D array of position fractions (0.0=flat, 1.0=full long)
    n                : number of permutations (default 2000)
    fee              : one-way fee rate (default 0.0005)
    target_vol       : vol-targeting annualized target vol (default 0.30)
    max_lev          : vol-targeting cap (default 2.0)
    vol_window       : rolling window for realized vol (default 720 bars)
    periods_per_year : bars per year for annualization (default 8760 for 1h)

    Returns
    -------
    actual  : float  — actual OOS Sharpe
    p_value : float  — fraction of permuted Sharpes >= actual
    dist    : array  — full permuted Sharpe distribution (length n)
    """
    close    = np.asarray(close, dtype=float)
    position = np.asarray(position, dtype=float)

    log_ret = np.concatenate([[0.0], np.log(close[1:] / close[:-1])])
    fwd_ret = np.concatenate([np.diff(close) / close[:-1], [0.0]])

    realized_vol = pd.Series(log_ret).rolling(vol_window).std().values * np.sqrt(periods_per_year)
    vol_scalar   = np.where(
        (realized_vol > 0) & ~np.isnan(realized_vol),
        np.clip(target_vol / realized_vol, 0, max_lev),
        1.0,
    )
    sized    = position * vol_scalar
    fee_cost = np.abs(np.diff(sized, prepend=0)) * fee

    def _sharpe(ret):
        sr = sized * ret - fee_cost
        r  = sr[~np.isnan(sr)]
        return (r.mean() / r.std()) * np.sqrt(periods_per_year) if r.std() > 0 else np.nan

    actual  = _sharpe(fwd_ret)
    dist    = np.array([_sharpe(np.random.permutation(fwd_ret)) for _ in range(n)])
    p_value = float((dist >= actual).mean())
    return actual, p_value, dist


def plot_mcpt(actual, dist, label="Strategy", output_path=os.path.join(tempfile.gettempdir(), "mcpt.png")):
    """
    Plot MCPT Sharpe distribution with actual Sharpe marked.

    Parameters
    ----------
    actual      : float — actual OOS Sharpe (from mcpt())
    dist        : array — permuted Sharpe distribution (from mcpt())
    label       : strategy name for title
    output_path : save path

    Returns
    -------
    output_path
    """
    import matplotlib.pyplot as plt

    p_value = float((dist >= actual).mean())
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(dist, bins=50, color="#95a5a6", alpha=0.7, label="Permuted Sharpes")
    ax.axvline(actual, color="#e74c3c", lw=2,
               label=f"Actual OOS Sharpe = {actual:.2f}")
    ax.set_xlabel("Sharpe Ratio")
    ax.set_ylabel("Count")
    sig = "p < 0.05: significant edge" if p_value < 0.05 else "p >= 0.05: no significant edge"
    ax.set_title(f"MCPT — {label}  (p-value = {p_value:.3f}  n={len(dist):,})  [{sig}]")
    ax.legend()
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    print(f"MCPT chart saved: {output_path}")
    return output_path


def write_mcpt_to_stats(strategy_name, p_value, n):
    """
    Merge the MCPT result into `strategies/<strategy_name>/stats.json` so the web
    workspace can show it next to the backtest stats. **Call it right after mcpt()**
    in validate.py; without it the p-value only exists in your chat reply.

    Adds / overwrites exactly two flat keys (same style as 'Sharpe Ratio'):
        "MCPT p-value"      : float 0–1
        "MCPT Permutations" : int  (= len(dist) = n)
    Every other field is left untouched. Read → update → atomic write (tmp + os.replace,
    like lib/report.py), so a reader never sees a half-written file.

    Key lifetime across backtest / live rewrites: see lib/runner.py `_carry_over`.

    Parameters
    ----------
    strategy_name : STRATEGY_NAME — the folder under strategies/
    p_value       : from mcpt()
    n             : number of permutations, len(dist) from mcpt()

    Returns
    -------
    path of the updated stats.json

    Raises FileNotFoundError if stats.json is missing — run the backtest first; never
    fabricate a stats.json to carry an MCPT result. Raises ValueError on a NaN/inf or
    out-of-range p_value — the key is then simply absent, which is the correct state.
    """
    path = os.path.join(_REPO_ROOT, 'strategies', strategy_name, 'stats.json')
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} not found — run the backtest first, then MCPT; "
            "write_mcpt_to_stats only merges into an existing stats.json"
        )
    # The api ingests stats.json with allow_nan=False — ONE NaN/inf here 400s the whole
    # machine's strategy upload. Never write a non-finite p-value; no result → no key.
    p_value = float(p_value)
    if not np.isfinite(p_value) or not (0.0 <= p_value <= 1.0):
        raise ValueError(f"write_mcpt_to_stats: p_value must be finite in [0, 1], got {p_value}")
    n = int(n)
    if n <= 0:
        raise ValueError(f"write_mcpt_to_stats: n must be a positive permutation count, got {n}")
    with open(path, encoding='utf-8') as f:
        stats = json.load(f)
    stats['MCPT p-value']      = round(p_value, 4)
    stats['MCPT Permutations'] = int(n)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    print(f"MCPT written to {path}: p-value={p_value:.4f}  permutations={int(n)}")
    return path
