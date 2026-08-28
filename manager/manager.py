"""
Blave Agent Strategy Manager

Computes the portfolio weights w for the live reconciler and updates
manager/portfolio_config.json.

Usage:
    python3 manager/manager.py [--lookback 365] [--notify]
    python3 manager/manager.py --apply   # actually write portfolio_config.json
    python3 manager/manager.py --allocator my_method   # use allocators/my_method/
    python3 manager/manager.py --members a,b --json manager/proposal.json
        # subset of strategies + machine-readable proposal (what the workspace
        # page's 跑優化 runs; --params-json '{"k": v}' overrides an allocator's PARAMS)

Default is DRY-RUN: weights are computed and printed but portfolio_config.json
is NOT touched. Pass --apply only after the user has confirmed the new weights.

Two built-in methods: `equal` (every strategy the same share) and `slope` (the
slope/std optimiser below). `--allocator <name>` picks either, or swaps in
allocators/<name>/allocator.py — see references/allocator-code.md.

Omitting `--allocator` does NOT mean `equal`: a portfolio that has already been
applied keeps the method it is live on (a config with a null or absent
`allocator` predates `equal`, so it means `slope`). Only a portfolio that has
never been applied falls to `equal` — which is the default for new portfolios
because a method that re-fits weights every day has to beat leaving them alone,
and the walk-forward gate is where it proves it.

This command ONLY recomputes weights / leverage. It never changes
account_value — that is the live position-sizing base (see lib/portfolio.py:
contribution = account_value * leverage * weight * position). To change capital,
edit portfolio_config.json["account_value"] directly as a separate, explicit step.
"""
import argparse, json, os, sys, time
import numpy as np
import pandas as pd
from pathlib import Path
from scipy.optimize import minimize

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.pnl import load_all_stats
from lib import allocator as allocator_lib

# ── Config ────────────────────────────────────────────────────────────────────
TARGET_VOL = 0.30  # target annual volatility (used to compute leverage)
VOL_WINDOW = 90    # trailing days for realized portfolio vol (leverage denominator)
NOTIFY     = False # send Telegram notification after update
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / 'portfolio_config.json'


def portfolio_slope_std(w, ret_matrix, lookback):
    """Annualized slope of cumsum(R·w) divided by std, over last `lookback` days."""
    port_ret = ret_matrix @ w
    recent   = port_ret[-lookback:]
    if len(recent) < 10:
        return 0.0
    cum = np.cumsum(recent)
    x   = np.arange(len(cum), dtype=float)
    slope, _ = np.polyfit(x, cum, 1)
    ann_slope = slope * 252
    std = recent.std()
    if std == 0:
        return 0.0
    return float(ann_slope / std)


def individual_score(returns_arr, lookback):
    """Annualized slope of cumsum / std for a single strategy (display only)."""
    recent = returns_arr[-lookback:] if len(returns_arr) > lookback else returns_arr
    if len(recent) < 10:
        return 0.0
    cum = np.cumsum(recent)
    x   = np.arange(len(cum), dtype=float)
    slope, _ = np.polyfit(x, cum, 1)
    ann_slope = slope * 252
    std = recent.std()
    return float(ann_slope / std) if std > 0 else 0.0


def equal_weights(ret_df, lookback):
    """Every strategy the same share. The default method.

    `lookback` is unused — that is the point: nothing is fitted, so there is
    nothing to overfit. The walk-forward benchmark draws random static weights,
    and equal weight sits at the centre of that cloud, which is why a
    re-fitting method has to beat it to be worth its turnover.
    """
    # Raw scores; clean() normalises and quantises them for display.
    return {name: 1.0 for name in ret_df.columns}


def optimize_weights(ret_df, lookback):
    """Find w that maximizes slope/std of R·w.

    Constraints: sum(w) = 1, w >= 0.
    Starting point: equal weight. Tries multiple random restarts for robustness.
    """
    names      = list(ret_df.columns)
    n          = len(names)
    ret_matrix = ret_df.values

    def objective(w):
        return -portfolio_slope_std(w, ret_matrix, lookback)

    constraints = [{'type': 'eq', 'fun': lambda w: w.sum() - 1}]
    bounds      = [(0.0, 1.0)] * n

    best_result = None
    best_score  = np.inf

    # Equal-weight start + 10 random restarts
    starts = [np.ones(n) / n] + [
        (r := np.random.dirichlet(np.ones(n))) for _ in range(10)
    ]
    for w0 in starts:
        res = minimize(objective, w0, method='SLSQP',
                       bounds=bounds, constraints=constraints,
                       options={'ftol': 1e-9, 'maxiter': 1000})
        if res.success and res.fun < best_score:
            best_score  = res.fun
            best_result = res

    if best_result is None:
        w_opt = np.ones(n) / n
    else:
        w_opt = best_result.x
        w_opt = np.clip(w_opt, 0, 1)
        w_opt /= w_opt.sum()

    return {names[i]: round(float(w_opt[i]), 4) for i in range(n)}


# The built-in methods, by the name `--allocator` selects them with. Reserved
# in lib/allocator.py so a user file can never shadow one. equal is first
# because it is the default: a fitted method must earn its turnover.
#
# Declared as a plain literal on purpose: the platform's reporter reads this
# with ast.literal_eval to show the methods (and their params) in the workspace
# page, exactly as it reads an allocator file's DISPLAY_NAME / PARAMS. It never
# imports workspace code, so anything it must see has to be a literal.
# Same contract as a user allocator: `params` is what the page offers, and a
# method that needs no knob declares none.
BUILTIN_METHODS = {
    'equal': {
        'display_name': '等權',
        'description': '每檔策略給一樣的配置比例,不看歷史',
        'params': {},
    },
    'slope': {
        'display_name': '斜率/波動',
        'description': '最大化組合權益曲線的斜率除以波動',
        # It fits on this window, so it is this method's own knob. `equal`
        # declares none — it does not look at history at all, and a knob that
        # changes nothing has no business in the page's parameter form.
        'params': {'lookback': 365},
    },
}
DEFAULT_METHOD = 'equal'

_BUILTIN_FNS = {'equal': equal_weights, 'slope': optimize_weights}


def default_allocator():
    """The method to use when `--allocator` is omitted.

    A portfolio that is ALREADY LIVE keeps the method it was applied with.
    `equal` became the default after `slope` had been the only built-in, so
    resolving a bare command to the new default would silently re-weight real
    positions the next time someone re-ran the usual apply. An existing config
    whose `allocator` is null or absent was applied by that older manager, i.e.
    slope. Only a portfolio that has never been applied gets the new default.
    """
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return DEFAULT_METHOD
    if not isinstance(cfg, dict) or not cfg.get('weights'):
        return DEFAULT_METHOD
    return cfg.get('allocator') or 'slope'


def default_target_vol():
    """`--target-vol` when omitted: the account's own setting, not the module
    default. manager.py writes target_vol_pct into portfolio_config on --apply,
    so taking the module default here would overwrite whatever the user set —
    and the leverage in the proposal they act on would be computed at someone
    else's target. The workspace page no longer offers this knob at all."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        pct = float(cfg['target_vol_pct'])
    except (OSError, ValueError, TypeError, KeyError):
        return TARGET_VOL
    # Out of range is not a reason to quietly substitute 30%: --apply writes
    # this number back, so a config reading 0.5 would be re-written as 30 — a
    # 60x change in leverage that nobody asked for and nothing reports.
    if not 0.01 <= pct / 100 <= 5:
        _fail(f"portfolio_config.json target_vol_pct is {pct}, outside 1–500% — "
              f"fix it, or pass --target-vol explicitly")
    return pct / 100


def _sync_lookback(args, declared, method=''):
    """One number across `--lookback`, the method's PARAMS and allocate()'s
    second argument, whichever end the caller came in from.

    A method that declares `lookback` fits on that window and may read it
    either way, so the two must agree with what gets reported: a window passed
    on the command line wins and is written back into PARAMS; with no flag, the
    declared window runs. A method that declares none has no window — 0, which
    a walk-forward reads as "every day is out of sample" — because a method
    that used history without saying so would be fitting on a window nobody
    can see or set. That is the contract: use the window, declare the window.
    """
    if not isinstance(declared, dict) or 'lookback' not in declared:
        if args.lookback is not None:
            # Say so: a flag that vanishes without a word looks like it took.
            print(f'--lookback {args.lookback} ignored: {method or "this method"} '
                  f'declares no window, so it runs with none', file=sys.stderr)
        args.lookback = 0
        return
    value = declared['lookback'] if args.lookback is None else args.lookback
    # Both ends must agree, so both are held to the same rule: a whole number
    # of days, at least one. A declared 180.0 or "200" would otherwise run on
    # 365 while the method reads its own value — the report and the run apart.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f'{method or "the method"} lookback must be a positive integer of days, '
              f'got {value!r} ({"from --lookback" if args.lookback is not None else "declared in PARAMS"})')
    args.lookback = value
    declared['lookback'] = value


def _fail(msg, code=2):
    """Bad input: reason as the LAST stderr line, exit 2 (3 = not enough
    history) — the workspace page's command listener shows exactly that line."""
    print(msg, file=sys.stderr)
    sys.exit(code)


def _atomic_json(path, obj):
    """tmp + os.replace so a reader never sees a half-written file. default=str:
    a user allocator's PARAMS may hold a tuple / numpy scalar — failing here,
    after the whole run, would throw the result away over a display value."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, indent=2, default=str)
    # Windows: os.replace fails while another process (the reporter, every
    # 10s during a run) has the target open — transient, retry briefly.
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.3)


def main():
    parser = argparse.ArgumentParser(description='Blave Agent Strategy Manager')
    parser.add_argument('--lookback',   type=int,   default=None,   help="Window in days the method fits on (default: the method's own; a method that declares none runs with no window)")
    parser.add_argument('--target-vol', type=float, default=None, help=f"Target annual volatility for leverage (default: portfolio_config's target_vol_pct, else {TARGET_VOL})")
    parser.add_argument('--notify',     action='store_true',             default=NOTIFY, help='Send Telegram notification after update')
    parser.add_argument('--apply',      action='store_true', help='Write the new weights to portfolio_config.json (default: dry-run, file untouched)')
    parser.add_argument('--allocator',  type=str, default=None, help=f"Weighting method: a built-in ({'/'.join(BUILTIN_METHODS)}) or allocators/<name>/allocator.py (default: the live config's method, else {DEFAULT_METHOD})")
    parser.add_argument('--members',    type=str, default=None, help='Comma-separated strategy names to include (default: every strategy with a backtest)')
    parser.add_argument('--params-json', type=str, default=None, help="JSON object overriding the allocator's PARAMS (declared keys only; built-ins take none)")
    parser.add_argument('--json',       type=str, default=None, help='Also write the proposal as JSON to this path (written only on success)')
    args = parser.parse_args()

    np.random.seed(42)

    # Bad input exits non-zero with the reason as the LAST stderr line — the
    # workspace page's command listener surfaces exactly that line.
    members = [m.strip() for m in args.members.split(',') if m.strip()] if args.members else None
    if args.members is not None and not members:
        _fail('no members given')
    # Omitting the flag means the default method, not "no method" — every
    # record downstream (proposal, portfolio_config) names it explicitly, so a
    # file never has to be read as "whatever the default was that day".
    allocator = args.allocator or default_allocator()
    builtin = BUILTIN_METHODS.get(allocator)
    if builtin and os.path.isdir(os.path.join(allocator_lib.ALLOCATORS_DIR, allocator)):
        _fail(f"'{allocator}' is a built-in method, so allocators/{allocator}/ can "
              f"never run — rename that directory to something else, then re-apply "
              f"with --allocator=<new name>: portfolio_config.json still points at "
              f"'{allocator}' and a bare --apply would switch you to the built-in")
    if args.target_vol is None:
        args.target_vol = default_target_vol()
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

    # Build aligned daily returns matrix
    series_map = {}
    for name, data in valid.items():
        dates = pd.to_datetime(data['daily_dates'])
        rets  = pd.Series(data['daily_returns'], index=dates, dtype=float)
        series_map[name] = rets

    ret_df = pd.DataFrame(series_map).sort_index().fillna(0)

    # Portfolio weighting: a built-in method, or a user allocator. A named
    # allocator that fails to load raises — never silently fall back to a
    # built-in, or a typo would quietly trade on different weights.
    mod = None
    if builtin:
        method = f"built-in {builtin['display_name']}"
        _sync_lookback(args, builtin['params'], method)
        weights = allocator_lib.clean(
            _BUILTIN_FNS[allocator](ret_df, args.lookback), list(ret_df.columns)
        )
    else:
        try:
            mod = allocator_lib.load(allocator)
        except (OSError, ValueError, TypeError, AttributeError, SyntaxError) as e:
            _fail(str(e))
        try:
            allocator_lib.apply_params(mod, param_overrides)
        except (ValueError, TypeError) as e:
            _fail(str(e))
        method = getattr(mod, 'DISPLAY_NAME', allocator)
        _sync_lookback(args, getattr(mod, 'PARAMS', None), method)
        weights = allocator_lib.clean(
            mod.allocate(ret_df, args.lookback), list(ret_df.columns)
        )

    # Individual scores for display
    scores = {
        name: individual_score(ret_df[name].values, args.lookback)
        for name in ret_df.columns
    }

    # Portfolio slope/std at optimal weights
    w_arr    = np.array([weights[k] for k in ret_df.columns])
    port_score = portfolio_slope_std(w_arr, ret_df.values, args.lookback)

    # Load and update portfolio_config.json
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    else:
        cfg = {'account_value': 10000}

    # Preserve deployment fields that manager must never overwrite.
    # account_value is deliberately NOT settable here — it is the live
    # position-sizing base and must be changed as a separate explicit step,
    # never as a side effect of a weight update.
    cfg.setdefault('account_value', 10000)
    cfg.setdefault('exchanges', {})
    cfg.setdefault('asset_specs', {})

    cfg['weights'] = weights
    # Which method produced these weights. This is NOT decoration: it is the
    # only input to default_allocator(), so a bare `manager.py --apply` re-runs
    # whatever is recorded here. Editing this field changes the method a live
    # portfolio falls back to. The workspace also reads it to pick the method
    # shown when the page opens.
    cfg['allocator'] = allocator

    # Portfolio volatility and Sharpe. Vol uses only the trailing VOL_WINDOW
    # days: it feeds the leverage suggestion (target_vol / port_vol), and
    # volatility clusters — the full sample would let a regime from years ago
    # dilute what leverage is safe today.
    active_names = [k for k, v in weights.items() if v > 0]
    vol_ret_df   = ret_df.tail(VOL_WINDOW)
    vol_days     = int(len(vol_ret_df))
    if len(active_names) >= 2:
        w_active = np.array([weights[k] for k in active_names])
        cov      = vol_ret_df[active_names].cov() * 365
        port_vol = float(np.sqrt(w_active @ cov.values @ w_active)) * 100
    elif len(active_names) == 1:
        port_vol = float(vol_ret_df[active_names[0]].std() * np.sqrt(365)) * 100
    else:
        port_vol = 0.0

    port_ret_series = ret_df.values @ w_arr
    port_std = port_ret_series.std()
    port_sharpe = float(port_ret_series.mean() / port_std * np.sqrt(365)) if port_std > 0 else 0.0

    account_value = cfg['account_value']
    leverage      = round(args.target_vol / (port_vol / 100), 4) if port_vol > 0 else 1.0

    cfg['ann_volatility_pct'] = round(port_vol, 2)
    cfg['target_vol_pct']     = round(args.target_vol * 100, 1)
    cfg['leverage']           = leverage
    cfg['sharpe_ratio']       = round(port_sharpe, 4)
    cfg.pop('daily_vol_usdt', None)

    if args.apply:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(cfg, f, indent=2)

    if args.json:
        # The proposal the workspace page renders. Independent of --apply on
        # purpose: the page only ever asks for a dry-run, applying stays a
        # separate explicit step (agent chat / --apply).
        # The params the page shows for this method: what it declares, with the
        # caller's actual lookback substituted where the method declares one. A
        # method that declares no knobs records none — the job the page sent and
        # the result it reads back then compare equal.
        params = dict(builtin['params']) if builtin else {}
        if mod is not None and isinstance(getattr(mod, 'PARAMS', None), dict):
            params.update(mod.PARAMS)
        # A method that declares `lookback` owns it: record what actually ran,
        # not the declared default.
        if 'lookback' in params:
            params['lookback'] = args.lookback
        proposal = {
            'members':        list(ret_df.columns),
            'allocator':      allocator,
            # Top level, mirroring stats.json: the window that actually ran
            # (0 = the method declares none), written for every method so the
            # page always has one field to compare. `params` carries it too,
            # but only for a method that declares it.
            'lookback':       args.lookback,
            'params':         params,
            'weights':        weights,
            'sharpe':         round(port_sharpe, 4),
            'ann_vol_pct':    round(port_vol, 2),
            # Days the vol above was actually computed over: min(VOL_WINDOW,
            # data length). history_days below stays the full data span.
            'vol_window_days': vol_days,
            'target_vol_pct': round(args.target_vol * 100, 1),
            'leverage':       leverage,
            'slope_std':      round(port_score, 4),
            'history_days':   int(len(ret_df)),
            'start':          ret_df.index[0].strftime('%Y-%m-%d'),
            'end':            ret_df.index[-1].strftime('%Y-%m-%d'),
            'computed_at':    int(time.time()),
        }
        _atomic_json(args.json, proposal)

    # Print summary
    window = f'lookback={args.lookback}d' if args.lookback else 'no window'
    print(f'\nBlave Agent Strategy Manager  ({window}, method={method})')
    print(f'{"Strategy":<28} {"Indiv Score":>12} {"Weight":>8} {"Sharpe":>8} {"MDD%":>8} {"Symbol":<16}')
    print('-' * 86)
    for name in sorted(weights, key=lambda x: weights[x], reverse=True):
        d      = valid[name]
        sym    = d.get('symbol') or d.get('strategy', '?')
        sharpe = d.get('Sharpe Ratio') or 0.0
        mdd    = d.get('Max Drawdown [%]') or 0.0
        print(f'{name:<28} {scores[name]:>12.4f} {weights[name]:>7.1%} '
              f'{sharpe:>8.2f} {mdd:>7.1f}% {sym:<16}')

    print(f'\nPortfolio slope/std : {port_score:.4f}')
    print(f'Portfolio Sharpe    : {port_sharpe:.4f}')
    print(f'Account Value       : ${account_value:,.0f}')
    print(f'Ann. Volatility     : {port_vol:.1f}%  →  target {args.target_vol*100:.0f}%  leverage {leverage:.2f}x')
    if args.apply:
        print(f'manager/portfolio_config.json updated\n')
    else:
        print('DRY RUN — manager/portfolio_config.json NOT modified.')
        print('Live weights are unchanged. Re-run with --apply to write the weights above.\n')

    if args.notify:
        try:
            from lib.notify import send_text
            action = 'Portfolio updated' if args.apply else 'Proposed weights (DRY RUN, config not modified)'
            lines = [f'[Blave Agent Manager] {action} ({window}, method={method})']
            for name, w in sorted(weights.items(), key=lambda x: x[1], reverse=True):
                sharpe = valid[name].get('Sharpe Ratio') or 0.0
                lines.append(f'  {name}: {w:.1%}  Sharpe {sharpe:.2f}')
            lines.append(f'Portfolio slope/std: {port_score:.4f}')
            lines.append(f'Account: ${account_value:,.0f}  Ann.Vol: {port_vol:.1f}%')
            send_text('\n'.join(lines))
        except Exception as e:
            print(f'Telegram notification failed: {e}')


if __name__ == '__main__':
    main()
