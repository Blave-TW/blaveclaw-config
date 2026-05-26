"""
BlaveClaw Strategy Manager

Finds portfolio weights w that maximize slope/std of the combined
portfolio returns (R · w), where R is the strategy returns matrix.
Updates manager/portfolio_config.json.

Usage:
    python3 manager/manager.py [--lookback 365] [--account 10000] [--notify]
"""
import argparse, json, sys
import numpy as np
import pandas as pd
from pathlib import Path
from scipy.optimize import minimize

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.pnl import load_all_stats

# ── Config ────────────────────────────────────────────────────────────────────
LOOKBACK   = 365   # days of history used for weight optimization
TARGET_VOL = 0.30  # target annual volatility (used to compute leverage)
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


def main():
    parser = argparse.ArgumentParser(description='BlaveClaw Strategy Manager')
    parser.add_argument('--lookback',   type=int,   default=LOOKBACK,   help=f'Lookback window in days (default {LOOKBACK})')
    parser.add_argument('--account',    type=float, default=None,       help='Override account_value in portfolio_config.json')
    parser.add_argument('--target-vol', type=float, default=TARGET_VOL, help=f'Target annual volatility for leverage (default {TARGET_VOL})')
    parser.add_argument('--notify',     action='store_true',             default=NOTIFY, help='Send Telegram notification after update')
    args = parser.parse_args()

    np.random.seed(42)

    valid = load_all_stats()
    if not valid:
        print('No strategies with daily_returns found. Run backtests first.')
        return

    # Build aligned daily returns matrix
    series_map = {}
    for name, data in valid.items():
        dates = pd.to_datetime(data['daily_dates'])
        rets  = pd.Series(data['daily_returns'], index=dates, dtype=float)
        series_map[name] = rets

    ret_df = pd.DataFrame(series_map).sort_index().fillna(0)

    # Portfolio optimization: maximize slope/std of R·w
    weights = optimize_weights(ret_df, args.lookback)

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

    # Preserve deployment fields that manager must never overwrite
    cfg.setdefault('exchanges', {})
    cfg.setdefault('asset_specs', {})

    if args.account is not None:
        cfg['account_value'] = args.account
    cfg['weights'] = weights

    # Portfolio volatility and Sharpe
    active_names = [k for k, v in weights.items() if v > 0]
    if len(active_names) >= 2:
        w_active = np.array([weights[k] for k in active_names])
        cov      = ret_df[active_names].cov() * 365
        port_vol = float(np.sqrt(w_active @ cov.values @ w_active)) * 100
    elif len(active_names) == 1:
        port_vol = float(ret_df[active_names[0]].std() * np.sqrt(365)) * 100
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

    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)

    # Print summary
    print(f'\nBlaveClaw Strategy Manager  (lookback={args.lookback}d)')
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
    print(f'manager/portfolio_config.json updated\n')

    if args.notify:
        try:
            from lib.notify import send_text
            lines = [f'[BlaveClaw Manager] Portfolio updated (lookback={args.lookback}d)']
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
