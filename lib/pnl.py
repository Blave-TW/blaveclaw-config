import glob, json, math, os
import pandas as pd


def daily_returns_typeA(pf_ret):
    """Resample Type A per-bar returns to daily. Returns (dates_list, returns_list)."""
    daily = pf_ret.resample('1D').apply(lambda x: (1 + x).prod() - 1).fillna(0)
    dates = [d.strftime('%Y-%m-%d') for d in daily.index]
    rets  = [0.0 if math.isnan(v) else round(float(v), 6) for v in daily.values]
    return dates, rets


def daily_returns_typeC(pf_series):
    """Extract daily returns from Type C portfolio series. Returns (dates_list, returns_list)."""
    daily = pf_series.fillna(0)
    dates = [d.strftime('%Y-%m-%d') for d in daily.index]
    rets  = [0.0 if math.isnan(v) else round(float(v), 6) for v in daily.values]
    return dates, rets


def update_stats_live(strategy_name, state, config):
    """Update order_params in stats.json after a live/paper signal."""
    path = f'strategies/{strategy_name}/stats.json'
    if not os.path.exists(path):
        return
    with open(path) as f:
        data = json.load(f)
    data['order_params'] = {
        'symbol':           state.get('symbol', config.get('SYMBOL')),
        'exchange':         state.get('exchange', config.get('EXCHANGE')),
        'interval':         config.get('INTERVAL', '1h'),
        'current_position': float(state.get('position', 0.0)),
    }
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def load_all_stats():
    """Load all strategy stats.json files that contain daily_returns.
    Returns {strategy_name: dict}.
    """
    result = {}
    for path in glob.glob('strategies/*/stats.json'):
        name = os.path.basename(os.path.dirname(path))
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get('daily_returns'):
                result[name] = data
        except Exception:
            pass
    return result
