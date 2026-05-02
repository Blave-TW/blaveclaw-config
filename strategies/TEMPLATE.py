'''
Strategy: [strategy name]
Symbol: BTCUSDT
Interval: 1h
Logic: [entry/exit rules]
'''

import gzip, json, logging, os, sys, requests
import numpy as np
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from backtesting import Backtest, Strategy
from dotenv import dotenv_values

# --- Config ---
MODE          = "backtest"   # "backtest" | "paper" | "live"
STRATEGY_NAME = "[strategy_name]"
SYMBOL        = "BTCUSDT"
INTERVAL      = "1h"
START         = "2024-01-01"
END           = None         # backtest end; None = today; live always fetches to today
FEE           = 0.0005       # 0.05% per side (taker fee)
BUDGET_USDT   = 1_000        # trading capital — backtest uses this as starting cash so P&L reflects real dollar amounts

_env = dotenv_values()
_HDRS = {'api-key': _env.get('blave_api_key', ''), 'secret-key': _env.get('blave_secret_key', '')}

# --- Logging ---
os.makedirs('/root/.openclaw/workspace/logs', exist_ok=True)
logging.basicConfig(
    filename=f'/root/.openclaw/workspace/logs/{STRATEGY_NAME}.log',
    level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s'
)

# --- State (live/paper only) ---
_STATE_FILE = f'{STRATEGY_NAME}_state.json'

def load_state():
    return json.load(open(_STATE_FILE)) if os.path.exists(_STATE_FILE) else None

def save_state(state):
    json.dump(state, open(_STATE_FILE, 'w'), indent=2)

def send_telegram(msg):
    pass  # wire up if needed

def place_order(side):
    pass  # implement using exchange API — read skills/blave-quant/references/<exchange>-skill.md

# --- Data ---
def fetch_historical(symbol, start, end):
    from datetime import datetime, timedelta
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
    rows, cursor = [], s
    while cursor < e:
        chunk_end = min(cursor + timedelta(days=365), e)
        r = requests.get('https://api.blave.org/kline', headers=_HDRS, params={
            'symbol': symbol, 'period': INTERVAL,
            'start_date': cursor.strftime('%Y-%m-%d'),
            'end_date':   chunk_end.strftime('%Y-%m-%d'),
        }, timeout=60)
        r.raise_for_status()
        rows.extend(r.json())
        cursor = chunk_end
    df = pd.DataFrame(rows)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df = df.set_index('time').sort_index()
    df = df[~df.index.duplicated(keep='first')]
    df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'})
    df['Volume'] = 0
    return df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)


# ─────────────────────────────────────────────────────────────
# FILL IN: Signal logic
# Pure function — no API calls, no I/O. Called identically in
# backtest (via BlaveStrategy.next) and live (via main loop).
# Signature: add whatever indicator values you need as params.
# Returns desired position state (not an event):
#   "LONG"  — hold long
#   "SHORT" — hold short
#   "FLAT"  — no position
# ─────────────────────────────────────────────────────────────
def compute_signal(close, ...) -> str:
    ...


# ─────────────────────────────────────────────────────────────
# FILL IN: Backtest wrapper (backtesting.py Strategy subclass)
# init()  — precompute indicators with self.I() to avoid look-ahead bias
# next()  — call compute_signal() with the current bar's values
# ─────────────────────────────────────────────────────────────
class BlaveStrategy(Strategy):
    # Define optimizable parameters here, e.g.:
    # param1 = 20

    def init(self):
        # self.sma = self.I(lambda x: pd.Series(x).rolling(20).mean().values, self.data.Close)
        pass

    def next(self):
        signal = compute_signal(self.data.Close[-1], ...)
        if signal == 'LONG':
            if self.position.is_short:
                self.position.close()
            if not self.position.is_long:
                self.buy()
        elif signal == 'SHORT':
            if self.position.is_long:
                self.position.close()
            if not self.position.is_short:
                self.sell()
        elif signal == 'FLAT' and self.position:
            self.position.close()


# --- Report ---
def upload_report(df, stats=None, state=None):
    ts_arr = (df.index.astype(np.int64) // 10**9).tolist()
    klines = [[int(ts), float(o), float(h), float(l), float(c)]
               for ts, o, h, l, c in zip(ts_arr, df['Open'], df['High'], df['Low'], df['Close'])]

    if stats is not None:
        # Backtest mode: extract trades and returns from backtesting.py stats
        trades = []
        for _, row in stats['_trades'].iterrows():
            trades.append({'time': int(row['EntryTime'].timestamp()), 'action': 'BUY',  'price': float(row['EntryPrice'])})
            trades.append({'time': int(row['ExitTime'].timestamp()),  'action': 'SELL', 'price': float(row['ExitPrice'])})
        trades.sort(key=lambda t: t['time'])
        equity  = stats['_equity_curve']['Equity'].reindex(df.index, method='ffill').values
        log_ret = np.diff(np.log(np.where(equity > 0, equity, 1)))
        returns = [0.0] + [0.0 if r != r else float(r) for r in log_ret]
    else:
        # Live/paper mode: reconstruct returns from trades_log
        trades    = state.get('trades_log', [])
        closes    = df['Close'].values
        side      = None   # 'long' | 'short' | None
        returns   = []
        trade_map = {t['time']: t['action'] for t in trades}
        for i, (ts, _) in enumerate(zip(ts_arr, closes)):
            bar_ret = (closes[i] - closes[i-1]) / closes[i-1] if i > 0 else 0.0
            action  = trade_map.get(ts)
            if action == 'BUY':    side = 'long'
            elif action == 'SHORT': side = 'short'
            elif action in ('SELL', 'COVER'): side = None
            fee = FEE if action else 0.0
            if side == 'long':    returns.append(float(bar_ret) - fee)
            elif side == 'short': returns.append(float(-bar_ret) - fee)
            else:                 returns.append(-fee)

    body = json.dumps({
        'strategy_name': STRATEGY_NAME, 'symbol': SYMBOL, 'interval': INTERVAL,
        'mode':          MODE,
        'code':          open(__file__).read(),
        'trades':        trades,
        'klines':        klines,
        'indicators':    state.get('indicators', []) if state else [],
        'returns':       returns,
    }).encode()
    requests.post('https://api.blave.org/openclaw/strategy/report', headers={
        'api-key':          _env.get('blave_api_key', ''),
        'secret-key':       _env.get('blave_secret_key', ''),
        'Content-Type':     'application/json',
        'Content-Encoding': 'gzip',
    }, data=gzip.compress(body), timeout=60).raise_for_status()


# --- Live execute ---
def execute(candle, signal, state):
    price = candle['close']

    # --- Close opposite position first ---
    if signal == 'LONG' and state['side'] == 'short':
        pnl = (state['entry'] - price) / state['entry'] * 100
        state.update({'side': None, 'entry': None, 'pnl': state['pnl'] + pnl, 'trades': state['trades'] + 1})
        state['trades_log'].append({'time': candle['time'], 'action': 'COVER', 'price': price})
        if MODE == 'live':            place_order('COVER')
        if MODE in ('live', 'paper'): send_telegram(f"COVER @ {price}  PnL={pnl:+.2f}%")
        logging.info(f"COVER @ {price}  PnL={pnl:+.2f}%")

    elif signal == 'SHORT' and state['side'] == 'long':
        pnl = (price - state['entry']) / state['entry'] * 100
        state.update({'side': None, 'entry': None, 'pnl': state['pnl'] + pnl, 'trades': state['trades'] + 1})
        state['trades_log'].append({'time': candle['time'], 'action': 'SELL', 'price': price})
        if MODE == 'live':            place_order('SELL')
        if MODE in ('live', 'paper'): send_telegram(f"SELL @ {price}  PnL={pnl:+.2f}%")
        logging.info(f"SELL @ {price}  PnL={pnl:+.2f}%")

    elif signal == 'FLAT' and state['side']:
        action = 'SELL' if state['side'] == 'long' else 'COVER'
        pnl = (price - state['entry']) / state['entry'] * 100 * (1 if state['side'] == 'long' else -1)
        state.update({'side': None, 'entry': None, 'pnl': state['pnl'] + pnl, 'trades': state['trades'] + 1})
        state['trades_log'].append({'time': candle['time'], 'action': action, 'price': price})
        if MODE == 'live':            place_order(action)
        if MODE in ('live', 'paper'): send_telegram(f"{action} @ {price}  PnL={pnl:+.2f}%")
        logging.info(f"{action} @ {price}  PnL={pnl:+.2f}%")

    # --- Open new position ---
    if signal == 'LONG' and not state['side']:
        state.update({'side': 'long', 'entry': price})
        state['trades_log'].append({'time': candle['time'], 'action': 'BUY', 'price': price})
        if MODE == 'live':            place_order('BUY')
        if MODE in ('live', 'paper'): send_telegram(f"BUY @ {price}")
        logging.info(f"BUY @ {price}")

    elif signal == 'SHORT' and not state['side']:
        state.update({'side': 'short', 'entry': price})
        state['trades_log'].append({'time': candle['time'], 'action': 'SHORT', 'price': price})
        if MODE == 'live':            place_order('SHORT')
        if MODE in ('live', 'paper'): send_telegram(f"SHORT @ {price}")
        logging.info(f"SHORT @ {price}")


def main():
    from datetime import datetime
    today = datetime.utcnow().strftime('%Y-%m-%d')
    end   = END if MODE == 'backtest' else today
    df    = fetch_historical(SYMBOL, START, end)

    if MODE == 'backtest':
        bt    = Backtest(df, BlaveStrategy, cash=BUDGET_USDT, commission=FEE, trade_on_close=True)
        stats = bt.run()
        print(stats[['Return [%]', 'Sharpe Ratio', 'Max. Drawdown [%]', 'Win Rate [%]', '# Trades']])
        bt.plot(filename=f'{SYMBOL}_{STRATEGY_NAME}_pnl.html', open_browser=False)
        upload_report(df, stats=stats)
        return

    # paper / live
    candles = [{'time': int(t.timestamp()), 'close': float(r['Close']),
                 'open': float(r['Open']), 'high': float(r['High']), 'low': float(r['Low'])}
                for t, r in df.iterrows()]

    state = load_state()
    if state is None:
        # First run — replay history to find correct position (no orders placed)
        state = {'side': None, 'entry': None, 'pnl': 0.0, 'trades': 0,
                 'trades_log': [], 'indicators': []}
        for candle in candles[:-1]:
            sig = compute_signal(candle['close'], ...)  # match compute_signal signature
            p   = candle['close']
            if sig == 'LONG' and state['side'] != 'long':
                if state['side'] == 'short':
                    pnl = (state['entry'] - p) / state['entry'] * 100
                    state.update({'side': None, 'entry': None, 'pnl': state['pnl'] + pnl, 'trades': state['trades'] + 1})
                    state['trades_log'].append({'time': candle['time'], 'action': 'COVER', 'price': p})
                state.update({'side': 'long', 'entry': p})
                state['trades_log'].append({'time': candle['time'], 'action': 'BUY', 'price': p})
            elif sig == 'SHORT' and state['side'] != 'short':
                if state['side'] == 'long':
                    pnl = (p - state['entry']) / state['entry'] * 100
                    state.update({'side': None, 'entry': None, 'pnl': state['pnl'] + pnl, 'trades': state['trades'] + 1})
                    state['trades_log'].append({'time': candle['time'], 'action': 'SELL', 'price': p})
                state.update({'side': 'short', 'entry': p})
                state['trades_log'].append({'time': candle['time'], 'action': 'SHORT', 'price': p})
            elif sig == 'FLAT' and state['side']:
                action = 'SELL' if state['side'] == 'long' else 'COVER'
                pnl = (p - state['entry']) / state['entry'] * 100 * (1 if state['side'] == 'long' else -1)
                state.update({'side': None, 'entry': None, 'pnl': state['pnl'] + pnl, 'trades': state['trades'] + 1})
                state['trades_log'].append({'time': candle['time'], 'action': action, 'price': p})
        save_state(state)
        logging.info(f"Bootstrapped: side={state['side']} entry={state['entry']}")

    candle = candles[-1]
    signal = compute_signal(candle['close'], ...)  # match compute_signal signature
    logging.info(f"signal={signal} close={candle['close']}")
    execute(candle, signal, state)
    upload_report(df, state=state)
    save_state(state)


if __name__ == '__main__':
    main()
