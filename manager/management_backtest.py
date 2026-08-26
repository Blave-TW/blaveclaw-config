"""
Blave Agent Management Backtest

Walk-forward simulation of the manager's dynamic portfolio allocation.
Each day, optimizes weights using the past `lookback` days (strictly OOS),
then applies those weights to the next day's strategy returns.

Compares against N random static Dirichlet-weighted portfolios.

Usage:
    python3 manager/management_backtest.py [--lookback 365] [--random-n 1000] [--output manager]
    # outputs: manager/pnl.png + manager/stats.json

    python3 manager/management_backtest.py --allocator my_method
    # Weighting method: a built-in (equal, the default, or slope) or
    # allocators/my_method/allocator.py. A user allocator's outputs default to
    # allocators/my_method/ so each method keeps its own stats.json + pnl.png
    # the way each strategy does; both built-ins share manager/.

    python3 manager/management_backtest.py --members a,b --progress manager/mgmt_progress.json
    # subset of strategies + a progress file the workspace page polls (what its
    # 跑回測 runs; --params-json '{"k": v}' overrides an allocator's PARAMS)

Exit codes: 2 = bad input (unknown strategy / PARAMS key / JSON), 3 = not
enough history for the lookback. The reason is the last stderr line.

This is the gate for using ANY weighting method live: it answers "does this
method beat randomly-drawn weights out of sample", which a single in-sample
weight calculation cannot.
"""
# ── Config ────────────────────────────────────────────────────────────────────
LOOKBACK  = 365   # days of history used to optimize weights each step
RANDOM_N  = 1000  # number of random portfolios for benchmark comparison
OUTPUT    = 'manager'  # output folder (saves pnl.png + report.json inside)
# ─────────────────────────────────────────────────────────────────────────────

import argparse, json, math, os, sys, time
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.pnl import load_all_stats
from lib import allocator as allocator_lib

sys.path.insert(0, str(Path(__file__).parent))
from manager import (BUILTIN_METHODS, DEFAULT_METHOD, _BUILTIN_FNS, default_allocator,
                     optimize_weights, _fail, _atomic_json)


def rolling_managed_returns(ret_df: pd.DataFrame, lookback: int, allocate_fn=None,
                            progress_cb=None):
    """
    Walk-forward: for each day i >= lookback, optimize weights on
    ret_df.iloc[i-lookback:i], then record ret_df.iloc[i] @ weights.

    allocate_fn(window, lookback) -> {strategy: weight}; main() always supplies
    one (built-in or user allocator) and the slope/std fallback here is for
    direct callers. This is the whole plug point — everything else (the OOS
    loop, the random benchmark, the stats) is method-agnostic.

    progress_cb(day, total) is called after every OOS day (see --progress).

    Returns:
        managed_ret:     pd.Series of OOS daily returns
        weights_history: pd.DataFrame of shape (OOS days, strategies)
    """
    allocate_fn = allocate_fn or optimize_weights
    values     = ret_df.values
    dates      = ret_df.index
    names      = list(ret_df.columns)
    n_total    = len(ret_df)
    oos_len    = n_total - lookback

    managed_vals  = np.zeros(oos_len)
    weights_array = np.zeros((oos_len, len(names)))

    for k in range(oos_len):
        i     = lookback + k
        window = ret_df.iloc[i - lookback: i]
        w_dict = allocate_fn(window, lookback)
        w_arr  = np.array([w_dict[n] for n in names])
        managed_vals[k]    = float(values[i] @ w_arr)
        weights_array[k]   = w_arr
        if progress_cb:
            progress_cb(k + 1, oos_len)

    oos_index       = dates[lookback:]
    managed_ret     = pd.Series(managed_vals, index=oos_index)
    weights_history = pd.DataFrame(weights_array, index=oos_index, columns=names)
    return managed_ret, weights_history


def random_benchmark(ret_df: pd.DataFrame, oos_index: pd.DatetimeIndex,
                     n: int = 500, seed: int = 42) -> np.ndarray:
    """
    Draw n random Dirichlet weight vectors, apply each to the OOS period.
    Returns ndarray of shape (n, len(oos_index)) — daily returns per portfolio.
    """
    np.random.seed(seed)
    s       = len(ret_df.columns)
    oos_ret = ret_df.loc[oos_index].values          # shape (T_oos, S)
    weights = np.random.dirichlet(np.ones(s), size=n)  # shape (n, S)
    return weights @ oos_ret.T                          # shape (n, T_oos)


def _sharpe(ret_arr: np.ndarray) -> float:
    std = ret_arr.std()
    return float(ret_arr.mean() / std * math.sqrt(365)) if std > 0 else 0.0


def _max_drawdown(ret_arr: np.ndarray) -> float:
    cum  = np.cumprod(1 + ret_arr)
    peak = np.maximum.accumulate(cum)
    dd   = (cum - peak) / peak
    return float(dd.min() * 100)


def _total_return(ret_arr: np.ndarray) -> float:
    return float((np.prod(1 + ret_arr) - 1) * 100)


def _cum_band(managed_ret: pd.Series, bench_rets: np.ndarray):
    """Cumulative-return curves as fractions: managed (T,), and the random
    portfolios' per-day p5 / p50 / p95 (each (T,)). Shared by the png and the
    stats.json `band` the workspace page draws."""
    managed_cum = np.cumprod(1 + managed_ret.values) - 1
    bench_cum = np.cumprod(1 + bench_rets, axis=1) - 1  # (n, T)
    p5  = np.percentile(bench_cum, 5,  axis=0)
    p50 = np.percentile(bench_cum, 50, axis=0)
    p95 = np.percentile(bench_cum, 95, axis=0)
    return managed_cum, p5, p50, p95


def _plot(managed_ret: pd.Series, band,
          weights_history: pd.DataFrame, output_path: str):
    dates = managed_ret.index

    managed_cum, p5, p50, p95 = band  # from _cum_band, computed once in main
    peak        = np.maximum.accumulate(managed_cum + 1)
    dd          = (managed_cum + 1 - peak) / peak

    fig, axes = plt.subplots(3, 1, figsize=(14, 10),
                             gridspec_kw={'height_ratios': [3, 1, 2]}, sharex=True)
    fig.suptitle('Management Walk-Forward Backtest', fontsize=12)

    # ── Panel 1: cumulative return ──
    ax1 = axes[0]
    ax1.fill_between(dates, p5 * 100, p95 * 100,
                     alpha=0.2, color='#888888', label='Random p5–p95')
    ax1.plot(dates, p50 * 100, color='#888888', lw=1, linestyle='--', label='Random median')
    ax1.plot(dates, managed_cum * 100, color='#2ecc71', lw=1.5, label='Managed')
    ax1.axhline(0, color='#888', lw=0.5, ls='--')
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda y, _: f'{y:.0f}%'))
    ax1.set_ylabel('Cumulative Return', fontsize=10)
    ax1.legend(fontsize=9, loc='upper left')
    ax1.grid(True, alpha=0.3)

    # ── Panel 2: drawdown ──
    ax2 = axes[1]
    ax2.fill_between(dates, dd * 100, 0, color='#e74c3c', alpha=0.6)
    ax2.axhline(0, color='#888', lw=0.5)
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda y, _: f'{y:.0f}%'))
    ax2.set_ylabel('Drawdown (%)', fontsize=10)
    ax2.grid(True, alpha=0.3)

    # ── Panel 3: weight history (stacked area) ──
    ax3   = axes[2]
    cols  = weights_history.columns.tolist()
    w_vals = weights_history.values.T
    colors = plt.cm.tab10(np.linspace(0, 1, len(cols)))
    ax3.stackplot(dates, w_vals, labels=cols, colors=colors, alpha=0.8)
    ax3.set_ylim(0, 1)
    ax3.yaxis.set_major_formatter(mticker.FuncFormatter(lambda y, _: f'{y:.0%}'))
    ax3.set_ylabel('Portfolio Weight', fontsize=10)
    ax3.legend(fontsize=7, loc='upper left', ncol=2)
    ax3.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()


def main():
    parser = argparse.ArgumentParser(description='Blave Agent Management Backtest')
    parser.add_argument('--lookback', type=int,   default=LOOKBACK,
                        help=f'Optimization window in days (default {LOOKBACK})')
    parser.add_argument('--random-n', type=int,   default=RANDOM_N,
                        help=f'Number of random benchmark portfolios (default {RANDOM_N})')
    parser.add_argument('--output',   type=str,   default=None,
                        help=f'Output folder (default: {OUTPUT}, or allocators/<name> for a user allocator)')
    parser.add_argument('--allocator', type=str,  default=None,
                        help=f"Weighting method: a built-in ({'/'.join(BUILTIN_METHODS)}) or "
                             f"allocators/<name>/allocator.py (default: the live "
                             f"config's method, else {DEFAULT_METHOD})")
    parser.add_argument('--members',  type=str,   default=None,
                        help='Comma-separated strategy names to include (default: every strategy with a backtest)')
    parser.add_argument('--params-json', type=str, default=None,
                        help="JSON object overriding the allocator's PARAMS (declared keys only; built-ins take none)")
    parser.add_argument('--progress', type=str,   default=None,
                        help='Write {"day", "total", "ts"} here during the walk-forward (at most every 2s)')
    args = parser.parse_args()

    np.random.seed(42)

    # Bad input exits non-zero with the reason as the LAST stderr line — the
    # workspace page's command listener surfaces exactly that line.
    members = [m.strip() for m in args.members.split(',') if m.strip()] if args.members else None
    if args.members is not None and not members:
        _fail('no members given')
    # Omitting the flag means the default method, not "no method" — stats.json
    # names it explicitly so a result is never read as "whatever the default
    # was the day it ran".
    # Same default as manager.py — the two buttons on the page must evaluate
    # and propose the SAME method, so a live portfolio's method wins here too.
    allocator = args.allocator or default_allocator()
    builtin = BUILTIN_METHODS.get(allocator)
    if builtin and os.path.isdir(os.path.join(allocator_lib.ALLOCATORS_DIR, allocator)):
        _fail(f"'{allocator}' is a built-in method, so allocators/{allocator}/ can "
              f"never run — rename that directory to something else")
    try:
        param_overrides = json.loads(args.params_json) if args.params_json else {}
    except ValueError as e:
        _fail(f'--params-json is not valid JSON: {e}')
    if param_overrides and builtin:
        _fail(f'built-in {allocator} takes no PARAMS (got {sorted(param_overrides)}); use --lookback')

    try:
        valid = load_all_stats(only=members)
    except ValueError as e:
        _fail(str(e))
    if not valid:
        _fail('No strategies with daily_returns found. Run backtests first.')

    series_map = {}
    for name, data in valid.items():
        dates = pd.to_datetime(data['daily_dates'])
        rets  = pd.Series(data['daily_returns'], index=dates, dtype=float)
        series_map[name] = rets

    ret_df = pd.DataFrame(series_map).sort_index().fillna(0)

    if len(ret_df) <= args.lookback:
        # Message shape is parsed by the workspace page (it offers 「改跑 N 天」
        # from the number) — keep it stable.
        print(f'insufficient history: {len(ret_df)} days <= lookback {args.lookback} '
              f'(need at least {args.lookback + 1})', file=sys.stderr)
        sys.exit(3)

    # A user allocator keeps its outputs next to the file, so several methods
    # can be compared without overwriting each other. Both built-ins share
    # manager/ — last run wins, and stats.json's `allocator` says which it was.
    mod = None
    names = list(ret_df.columns)
    if builtin:
        fn          = _BUILTIN_FNS[allocator]
        method      = f"built-in {builtin['display_name']}"
        allocate_fn = lambda window, lb: allocator_lib.clean(fn(window, lb), names)
        output      = args.output or OUTPUT
    else:
        try:
            mod = allocator_lib.load(allocator)
        except (OSError, ValueError, TypeError, AttributeError, SyntaxError) as e:
            _fail(str(e))
        try:
            allocator_lib.apply_params(mod, param_overrides)
        except (ValueError, TypeError) as e:
            _fail(str(e))
        method      = getattr(mod, 'DISPLAY_NAME', allocator)
        allocate_fn = lambda window, lb: allocator_lib.clean(mod.allocate(window, lb), names)
        output      = args.output or os.path.join(allocator_lib.ALLOCATORS_DIR, allocator)

    strategies = list(ret_df.columns)
    print(f'\nManagement Walk-Forward Backtest  (lookback={args.lookback}d, method={method})')
    print(f'Strategies : {", ".join(strategies)}')
    print(f'Total data : {len(ret_df)} days  '
          f'({ret_df.index[0].date()} → {ret_df.index[-1].date()})')
    print(f'OOS period : {len(ret_df) - args.lookback} days  '
          f'({ret_df.index[args.lookback].date()} → {ret_df.index[-1].date()})')
    print(f'Running walk-forward optimization... (this may take a moment)')

    progress_cb = None
    if args.progress:
        last = [0.0]

        def progress_cb(day, total):
            now = time.time()
            if day == total or now - last[0] >= 2:
                last[0] = now
                try:
                    _atomic_json(args.progress, {'day': day, 'total': total, 'ts': int(now)})
                except OSError:
                    pass  # best-effort: on Windows os.replace fails if the
                          # reporter has the file open — never abort the run over it

    managed_ret, weights_history = rolling_managed_returns(
        ret_df, args.lookback, allocate_fn, progress_cb=progress_cb)
    oos_index = managed_ret.index

    print(f'Running random benchmark (n={args.random_n})...')
    bench_rets = random_benchmark(ret_df, oos_index, n=args.random_n)

    m_ret   = managed_ret.values
    m_total = _total_return(m_ret)
    m_sharpe = _sharpe(m_ret)
    m_mdd   = _max_drawdown(m_ret)

    bench_totals  = np.array([_total_return(bench_rets[i]) for i in range(args.random_n)])
    bench_sharpes = np.array([_sharpe(bench_rets[i])       for i in range(args.random_n)])
    managed_beats_pct = float((bench_sharpes < m_sharpe).mean() * 100)

    print(f'\n{"":─<60}')
    print(f'  Managed   Return: {m_total:+.1f}%  Sharpe: {m_sharpe:.2f}  MDD: {m_mdd:.1f}%')
    print(f'  Random    Sharpe  median={np.median(bench_sharpes):.2f}  '
          f'p5={np.percentile(bench_sharpes, 5):.2f}  '
          f'p95={np.percentile(bench_sharpes, 95):.2f}')
    print(f'  Managed beats {managed_beats_pct:.1f}% of {args.random_n} random portfolios on Sharpe')
    print(f'{"":─<60}')

    band = _cum_band(managed_ret, bench_rets)
    managed_cum, p5, p50, p95 = band
    # The params the page shows for this method: what it declares, with the
    # caller's actual lookback substituted where the method declares one. A
    # method that declares no knobs records none — the job the page sent and
    # the result it reads back then compare equal.
    params = dict(builtin['params']) if builtin else {}
    if mod is not None and isinstance(getattr(mod, 'PARAMS', None), dict):
        params.update(mod.PARAMS)

    result = {
        'lookback':  args.lookback,
        'allocator': allocator,   # always explicit: a built-in name or a file's
        'members':   strategies,
        'params':    params,
        'computed_at': int(time.time()),
        'start':    oos_index[0].strftime('%Y-%m-%d'),
        'end':      oos_index[-1].strftime('%Y-%m-%d'),
        'managed': {
            'total_return_%': round(m_total, 4),
            'sharpe':         round(m_sharpe, 4),
            'max_drawdown_%': round(m_mdd, 4),
        },
        'random_benchmark': {
            'n':                          args.random_n,
            'median_sharpe':              round(float(np.median(bench_sharpes)),           4),
            'p5_sharpe':                  round(float(np.percentile(bench_sharpes, 5)),    4),
            'p95_sharpe':                 round(float(np.percentile(bench_sharpes, 95)),   4),
            'managed_beats_pct_sharpe':   round(managed_beats_pct,                         2),
            'median_total_return_%':      round(float(np.median(bench_totals)),             4),
            'p5_total_return_%':          round(float(np.percentile(bench_totals, 5)),      4),
            'p95_total_return_%':         round(float(np.percentile(bench_totals, 95)),     4),
            # per-day cumulative return % of the random portfolios — the band
            # behind the managed curve (same numbers as pnl.png panel 1)
            'band': {
                'p5':  [round(float(v) * 100, 4) for v in p5],
                'p50': [round(float(v) * 100, 4) for v in p50],
                'p95': [round(float(v) * 100, 4) for v in p95],
            },
        },
        'daily_dates':     [d.strftime('%Y-%m-%d') for d in oos_index],
        'managed_cum':     [round(float(v) * 100, 4) for v in managed_cum],  # cumulative %
        'managed_returns': [round(float(v), 6) for v in m_ret],
        'weights_history': {
            col: [round(float(v), 4) for v in weights_history[col].values]
            for col in weights_history.columns
        },
    }

    os.makedirs(output, exist_ok=True)
    json_path = os.path.join(output, 'stats.json')
    png_path  = os.path.join(output, 'pnl.png')

    # Atomic: a cancelled / killed run must not leave a half-written stats.json
    # that the reporter would read as a finished backtest.
    _atomic_json(json_path, result)

    _plot(managed_ret, band, weights_history, png_path)

    print(f'\n  Output: {output}/')
    print(f'          ├── stats.json')
    print(f'          └── pnl.png\n')


if __name__ == '__main__':
    main()
