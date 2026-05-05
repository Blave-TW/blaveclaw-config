import gzip, json, requests
import numpy as np


def upload_report(df, strategy_name, symbol, interval, mode, env, fee=0.0005,
                  stats=None, state=None, strategy_file=None, indicators=None):
    """Upload backtest or live report to Blave website."""
    ts_arr = (df.index.astype(np.int64) // 10**9).tolist()
    klines = [[int(ts), float(o), float(h), float(l), float(c)]
               for ts, o, h, l, c in zip(ts_arr, df['Open'], df['High'], df['Low'], df['Close'])]

    if stats is not None:
        trades = []
        for _, row in stats['_trades'].iterrows():
            trades.append({'time': int(row['EntryTime'].timestamp()), 'action': 'BUY',  'price': float(row['EntryPrice'])})
            trades.append({'time': int(row['ExitTime'].timestamp()),  'action': 'SELL', 'price': float(row['ExitPrice'])})
        trades.sort(key=lambda t: t['time'])
        equity  = stats['_equity_curve']['Equity'].reindex(df.index, method='ffill').values
        log_ret = np.diff(np.log(np.where(equity > 0, equity, 1)))
        returns = [0.0] + [0.0 if r != r else float(r) for r in log_ret]
    else:
        trades    = state.get('trades_log', [])
        closes    = df['Close'].values
        side      = None
        returns   = []
        trade_map = {t['time']: t['action'] for t in trades}
        for i, (ts, _) in enumerate(zip(ts_arr, closes)):
            bar_ret = (closes[i] - closes[i-1]) / closes[i-1] if i > 0 else 0.0
            action  = trade_map.get(ts)
            if action == 'BUY':                      side = 'long'
            elif action == 'SHORT':                  side = 'short'
            elif action in ('SELL', 'COVER'):        side = None
            fee_cost = fee if action else 0.0
            if side == 'long':    returns.append(float(bar_ret) - fee_cost)
            elif side == 'short': returns.append(float(-bar_ret) - fee_cost)
            else:                 returns.append(-fee_cost)

    code = open(strategy_file).read() if strategy_file else ''
    if indicators is None:
        indicators = state.get('indicators', []) if state else []
    body = json.dumps({
        'strategy_name': strategy_name, 'symbol': symbol, 'interval': interval,
        'mode':          mode,
        'code':          code,
        'trades':        trades,
        'klines':        klines,
        'indicators':    indicators,
        'returns':       returns,
    }).encode()
    requests.post('https://api.blave.org/openclaw/strategy/report', headers={
        'api-key':          env.get('blave_api_key', ''),
        'secret-key':       env.get('blave_secret_key', ''),
        'Content-Type':     'application/json',
        'Content-Encoding': 'gzip',
    }, data=gzip.compress(body), timeout=60).raise_for_status()
