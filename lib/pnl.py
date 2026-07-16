import glob, json, math, os
import pandas as pd


def daily_returns_typeA(pf_ret):
    """Resample Type A per-bar returns to daily. Returns (dates_list, returns_list)."""
    daily = pf_ret.resample('1D').apply(lambda x: (1 + x).prod() - 1).fillna(0)
    dates = [d.strftime('%Y-%m-%d') for d in daily.index]
    rets  = [0.0 if math.isnan(v) else round(float(v), 6) for v in daily.values]
    return dates, rets


def daily_returns_typeC(pf_series):
    """Resample Type C per-bar returns to daily. Returns (dates_list, returns_list).

    Must resample: Type C strategies can run on intraday bars (e.g. 60m), and
    emitting one entry per bar produces duplicate date strings that crash
    management_backtest's DataFrame merge (pandas duplicate-axis)."""
    return daily_returns_typeA(pf_series)


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
