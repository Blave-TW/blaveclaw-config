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
enough history for the method's window. The reason is the last stderr line.

The walk-forward answers "how would this weighting method have traded out of
sample" — something a single in-sample weight calculation cannot. The random
comparison it reports is a REFERENCE figure evaluated only on the overlap
where every member has data, not a pass/fail gate: measured on real data it
moves by tens of points when the period shifts.
"""
# ── Config ────────────────────────────────────────────────────────────────────
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
                     _sync_lookback, optimize_weights, _fail, _atomic_json,
                     build_returns, ABSENT_FILL_ANNUAL)


def rolling_managed_returns(ret_df: pd.DataFrame, lookback: int, allocate_fn=None,
                            progress_cb=None, real_df: pd.DataFrame = None):
    """
    Walk-forward: for each day i >= lookback, optimize weights on
    ret_df.iloc[i-lookback:i], then record real_df.iloc[i] @ weights.

    allocate_fn(window, lookback) -> {strategy: weight}; main() always supplies
    one (built-in or user allocator) and the slope/std fallback here is for
    direct callers. This is the whole plug point — everything else (the OOS
    loop, the random benchmark, the stats) is method-agnostic.

    `ret_df` is what the method FITS on and `real_df` is what the portfolio
    EARNS (build_returns() makes the pair; they differ only on days a strategy
    had no data). Defaulting real_df to ret_df keeps a direct caller that
    passes one frame doing exactly what it did before.

    progress_cb(day, total) is called after every OOS day (see --progress).

    Returns:
        managed_ret:     pd.Series of OOS daily returns
        weights_history: pd.DataFrame of shape (OOS days, strategies)
    """
    allocate_fn = allocate_fn or optimize_weights
    real_df    = ret_df if real_df is None else real_df
    values     = real_df.values
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
    """Cumulative-return curves as fractions: managed (T,) over the FULL OOS,
    and the random portfolios' per-day p5 / p50 / p95 (each (T_eval,)) over the
    bench slice it is given — the caller passes the overlap-period slice, so
    the band compounds from the overlap start, not from days when some member
    did not exist. Shared by the png and the stats.json `band` the workspace
    page draws."""
    managed_cum = np.cumprod(1 + managed_ret.values) - 1
    if bench_rets.shape[1] == 0:
        empty = np.array([])
        return managed_cum, empty, empty, empty
    bench_cum = np.cumprod(1 + bench_rets, axis=1) - 1  # (n, T_eval)
    p5  = np.percentile(bench_cum, 5,  axis=0)
    p50 = np.percentile(bench_cum, 50, axis=0)
    p95 = np.percentile(bench_cum, 95, axis=0)
    return managed_cum, p5, p50, p95


def _plot(managed_ret: pd.Series, band, band_dates: pd.DatetimeIndex,
          weights_history: pd.DataFrame, output_path: str):
    dates = managed_ret.index

    managed_cum, p5, p50, p95 = band  # from _cum_band, computed once in main
    peak        = np.maximum.accumulate(managed_cum + 1)
    dd          = (managed_cum + 1 - peak) / peak

    fig, axes = plt.subplots(3, 1, figsize=(14, 10),
                             gridspec_kw={'height_ratios': [3, 1, 2]}, sharex=True)
    fig.suptitle('Management Walk-Forward Backtest', fontsize=12)

    # ── Panel 1: cumulative return ──
    # The random band only spans band_dates (the days every member has data) —
    # outside it there is no fair comparison, so nothing is drawn there.
    ax1 = axes[0]
    if len(band_dates):
        ax1.fill_between(band_dates, p5 * 100, p95 * 100,
                         alpha=0.2, color='#888888', label='Random p5–p95 (all members live)')
        ax1.plot(band_dates, p50 * 100, color='#888888', lw=1, linestyle='--', label='Random median')
        if band_dates[0] != dates[0]:
            ax1.axvline(band_dates[0], color='#888888', lw=0.8, ls=':')
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
    parser.add_argument('--lookback', type=int,   default=None,
                        help="Walk-forward window in days (default: the method's own; a "
                             "method that declares none runs with no window). A window "
                             "given here is never moved.")
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
        _fail(f'built-in {allocator} takes no PARAMS (got {sorted(param_overrides)})')

    try:
        valid = load_all_stats(only=members)
    except ValueError as e:
        _fail(str(e))
    if not valid:
        _fail('No strategies with daily_returns found. Run backtests first.')

    # fit_df is what the method sees, ret_df is what the money did — they
    # differ only on days a strategy had no data (see build_returns).
    fit_df, ret_df = build_returns(valid)

    # Per-member data span, and the overlap where EVERY member has data. The
    # random comparison below runs only inside the overlap: outside it the
    # benchmark would be forced to hold members that did not exist yet, which
    # biases the verdict in whichever direction the fill happens to point.
    spans = {n: (pd.to_datetime(d['daily_dates'][0]), pd.to_datetime(d['daily_dates'][-1]))
             for n, d in valid.items()}
    overlap_start = max(s[0] for s in spans.values())
    overlap_end   = min(s[1] for s in spans.values())

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

    # The window is the method's, so it is only known once the method is.
    # A method that declares `lookback` fits on it, and a window somebody chose
    # is never moved: the run has to be the one that was asked for, or the
    # number reported is not the number that ran — fail and let them pick (the
    # page turns this message into 「改跑 N 天」). A method that declares none
    # gets 0: it fits nothing, so nothing has to be held out, and every day of
    # history is out of sample. That is also why a young portfolio on the
    # default method can always run the gate.
    declared = builtin['params'] if builtin else (getattr(mod, 'PARAMS', None) or {})
    _sync_lookback(args, declared, method)
    if args.lookback and len(ret_df) <= args.lookback:
        # Message shape is parsed by the workspace page — keep it stable.
        print(f'insufficient history: {len(ret_df)} days <= lookback {args.lookback} '
              f'(need at least {args.lookback + 1})', file=sys.stderr)
        sys.exit(3)

    strategies = list(ret_df.columns)
    window = f'lookback={args.lookback}d' if args.lookback else 'no window'
    print(f'\nManagement Walk-Forward Backtest  ({window}, method={method})')
    print(f'Strategies : {", ".join(strategies)}')
    print(f'Total data : {len(ret_df)} days  '
          f'({ret_df.index[0].date()} → {ret_df.index[-1].date()})')
    print(f'OOS period : {len(ret_df) - args.lookback} days  '
          f'({ret_df.index[args.lookback].date()} → {ret_df.index[-1].date()})')
    # Per strategy, so the count is days (a matrix-wide count would exceed the
    # history whenever two members are short at once) and the reader can see
    # WHICH member is short — the one the method may weight to zero.
    absent_by = {n: int(d) for n, d in (fit_df != ret_df).sum().items() if d}
    absent = int((fit_df != ret_df).any(axis=1).sum())
    if absent_by:
        detail = ', '.join(f'{n} {d}d' for n, d in sorted(absent_by.items()))
        print(f'No-data days: {detail} — charged at {ABSENT_FILL_ANNUAL:+.1%}/yr while '
              f'fitting; the returns below still count them as 0')
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
        fit_df, args.lookback, allocate_fn, progress_cb=progress_cb, real_df=ret_df)
    oos_index = managed_ret.index

    print(f'Running random benchmark (n={args.random_n})...')
    bench_rets = random_benchmark(ret_df, oos_index, n=args.random_n)

    # Managed stats describe the WHOLE walk-forward; the random comparison is
    # evaluated only on the overlap (all members live) — a reference figure,
    # not a pass/fail verdict. It moves a lot with the period you look at.
    m_ret   = managed_ret.values
    m_total = _total_return(m_ret)
    m_sharpe = _sharpe(m_ret)
    m_mdd   = _max_drawdown(m_ret)

    eval_mask  = (oos_index >= overlap_start) & (oos_index <= overlap_end)
    eval_index = oos_index[eval_mask]
    eval_days  = int(eval_mask.sum())
    if eval_days:
        m_eval        = m_ret[eval_mask]
        bench_eval    = bench_rets[:, eval_mask]
        m_eval_sharpe = _sharpe(m_eval)
        bench_totals  = np.array([_total_return(bench_eval[i]) for i in range(args.random_n)])
        bench_sharpes = np.array([_sharpe(bench_eval[i])       for i in range(args.random_n)])
        managed_beats_pct = float((bench_sharpes < m_eval_sharpe).mean() * 100)
    else:
        bench_eval = bench_rets[:, :0]
        m_eval_sharpe = None
        bench_totals = bench_sharpes = np.array([])
        managed_beats_pct = None

    print(f'\n{"":─<60}')
    print(f'  Managed   Return: {m_total:+.1f}%  Sharpe: {m_sharpe:.2f}  MDD: {m_mdd:.1f}%')
    if eval_days:
        print(f'  Random comparison on the {eval_days} days every member has data '
              f'({eval_index[0].date()} → {eval_index[-1].date()}):')
        print(f'    Managed Sharpe {m_eval_sharpe:.2f}  vs random '
              f'median={np.median(bench_sharpes):.2f}  '
              f'p5={np.percentile(bench_sharpes, 5):.2f}  '
              f'p95={np.percentile(bench_sharpes, 95):.2f}')
        print(f'    Beats {managed_beats_pct:.1f}% of {args.random_n} random portfolios on Sharpe '
              f'(reference — this number moves a lot with the period)')
    else:
        print('  Random comparison skipped — no day inside the walk-forward has '
              'data for every member')
    print(f'{"":─<60}')

    band = _cum_band(managed_ret, bench_eval)
    managed_cum, p5, p50, p95 = band
    # The params the page shows for this method: what it declares, with the
    # caller's actual lookback substituted where the method declares one. A
    # method that declares no knobs records none — the job the page sent and
    # the result it reads back then compare equal.
    params = dict(builtin['params']) if builtin else {}
    if mod is not None and isinstance(getattr(mod, 'PARAMS', None), dict):
        params.update(mod.PARAMS)
    # A method that declares `lookback` owns it: record the window that ACTUALLY
    # ran, not the declared default. They differ whenever the caller passed
    # --lookback.
    if 'lookback' in params:
        params['lookback'] = args.lookback

    result = {
        'lookback':  args.lookback,   # 0 = the method declares no window; all OOS
        # What a no-data day cost the method while fitting, so a result is
        # never read as assumption-free. Top level, not in `params`: `params`
        # is the method's own declared knobs — a built-in takes none at all and
        # an allocator rejects any key it did not declare — so a fixed cost
        # that no method chooses does not belong there.
        'absent_fill_annual_pct': round(ABSENT_FILL_ANNUAL * 100, 4),
        # Days on which SOME member had no data, not member-days.
        'absent_days': absent,
        'allocator': allocator,   # always explicit: a built-in name or a file's
        'members':   strategies,
        'params':    params,
        'computed_at': int(time.time()),
        'start':    oos_index[0].strftime('%Y-%m-%d'),
        'end':      oos_index[-1].strftime('%Y-%m-%d'),
        # Each member's own backtest span, and the overlap where every member
        # has data — the ONLY period the random comparison below is run on.
        'member_spans': {n: [s[0].strftime('%Y-%m-%d'), s[1].strftime('%Y-%m-%d')]
                         for n, s in spans.items()},
        'overlap': {
            'start': overlap_start.strftime('%Y-%m-%d') if eval_days else None,
            'end':   overlap_end.strftime('%Y-%m-%d')   if eval_days else None,
            'eval_days': eval_days,
        },
        'managed': {
            'total_return_%': round(m_total, 4),
            'sharpe':         round(m_sharpe, 4),
            'max_drawdown_%': round(m_mdd, 4),
        },
        # Reference comparison, not a verdict — evaluated on the overlap only,
        # and it moves a lot with the period. All None / empty when no OOS day
        # has every member live.
        'random_benchmark': {
            'n':                          args.random_n,
            'sharpe_eval':                None if m_eval_sharpe is None else round(m_eval_sharpe, 4),
            'median_sharpe':              round(float(np.median(bench_sharpes)),           4) if eval_days else None,
            'p5_sharpe':                  round(float(np.percentile(bench_sharpes, 5)),    4) if eval_days else None,
            'p95_sharpe':                 round(float(np.percentile(bench_sharpes, 95)),   4) if eval_days else None,
            'managed_beats_pct_sharpe':   round(managed_beats_pct,                         2) if eval_days else None,
            'median_total_return_%':      round(float(np.median(bench_totals)),             4) if eval_days else None,
            'p5_total_return_%':          round(float(np.percentile(bench_totals, 5)),      4) if eval_days else None,
            'p95_total_return_%':         round(float(np.percentile(bench_totals, 95)),     4) if eval_days else None,
            # per-day cumulative return % of the random portfolios — the band
            # behind the managed curve (same numbers as pnl.png panel 1).
            # Spans ONLY the overlap: align it at `band_start` in daily_dates.
            'band_start': eval_index[0].strftime('%Y-%m-%d') if eval_days else None,
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

    _plot(managed_ret, band, eval_index, weights_history, png_path)

    print(f'\n  Output: {output}/')
    print(f'          ├── stats.json')
    print(f'          └── pnl.png\n')


if __name__ == '__main__':
    main()
