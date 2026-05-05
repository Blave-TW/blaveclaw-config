import json, logging, os


def bootstrap(df, signal_fn):
    """Replay historical candles to find initial position state (no orders placed).
    df: DataFrame with OHLCV + indicator columns (output of add_indicators)
    signal_fn: compute_signal function, accepts a pd.Series row
    """
    state = {'side': None, 'entry': None, 'pnl': 0.0, 'trades': 0, 'trades_log': [], 'indicators': []}
    for ts, row in df.iloc[:-1].iterrows():
        sig = signal_fn(row)
        p   = float(row['Close'])
        t   = int(ts.timestamp())
        if sig == 'LONG' and state['side'] != 'long':
            if state['side'] == 'short':
                pnl = (state['entry'] - p) / state['entry'] * 100
                state.update({'side': None, 'entry': None, 'pnl': state['pnl'] + pnl, 'trades': state['trades'] + 1})
                state['trades_log'].append({'time': t, 'action': 'COVER', 'price': p})
            state.update({'side': 'long', 'entry': p})
            state['trades_log'].append({'time': t, 'action': 'BUY', 'price': p})
        elif sig == 'SHORT' and state['side'] != 'short':
            if state['side'] == 'long':
                pnl = (p - state['entry']) / state['entry'] * 100
                state.update({'side': None, 'entry': None, 'pnl': state['pnl'] + pnl, 'trades': state['trades'] + 1})
                state['trades_log'].append({'time': t, 'action': 'SELL', 'price': p})
            state.update({'side': 'short', 'entry': p})
            state['trades_log'].append({'time': t, 'action': 'SHORT', 'price': p})
        elif sig == 'FLAT' and state['side']:
            action = 'SELL' if state['side'] == 'long' else 'COVER'
            pnl = (p - state['entry']) / state['entry'] * 100 * (1 if state['side'] == 'long' else -1)
            state.update({'side': None, 'entry': None, 'pnl': state['pnl'] + pnl, 'trades': state['trades'] + 1})
            state['trades_log'].append({'time': t, 'action': action, 'price': p})
    logging.info(f"Bootstrapped: side={state['side']} entry={state['entry']}")
    return state


def load_state(strategy_name):
    path = f'{strategy_name}_state.json'
    return json.load(open(path)) if os.path.exists(path) else None


def save_state(strategy_name, state):
    json.dump(state, open(f'{strategy_name}_state.json', 'w'), indent=2)


def execute(candle, signal, state, mode, place_order_fn=None, send_telegram_fn=None):
    """Process one candle: close opposite position, open new position."""
    price = candle['close']

    if signal == 'LONG' and state['side'] == 'short':
        pnl = (state['entry'] - price) / state['entry'] * 100
        state.update({'side': None, 'entry': None, 'pnl': state['pnl'] + pnl, 'trades': state['trades'] + 1})
        state['trades_log'].append({'time': candle['time'], 'action': 'COVER', 'price': price})
        if mode == 'live' and place_order_fn:            place_order_fn('COVER')
        if mode in ('live', 'paper') and send_telegram_fn: send_telegram_fn(f"COVER @ {price}  PnL={pnl:+.2f}%")
        logging.info(f"COVER @ {price}  PnL={pnl:+.2f}%")

    elif signal == 'SHORT' and state['side'] == 'long':
        pnl = (price - state['entry']) / state['entry'] * 100
        state.update({'side': None, 'entry': None, 'pnl': state['pnl'] + pnl, 'trades': state['trades'] + 1})
        state['trades_log'].append({'time': candle['time'], 'action': 'SELL', 'price': price})
        if mode == 'live' and place_order_fn:            place_order_fn('SELL')
        if mode in ('live', 'paper') and send_telegram_fn: send_telegram_fn(f"SELL @ {price}  PnL={pnl:+.2f}%")
        logging.info(f"SELL @ {price}  PnL={pnl:+.2f}%")

    elif signal == 'FLAT' and state['side']:
        action = 'SELL' if state['side'] == 'long' else 'COVER'
        pnl = (price - state['entry']) / state['entry'] * 100 * (1 if state['side'] == 'long' else -1)
        state.update({'side': None, 'entry': None, 'pnl': state['pnl'] + pnl, 'trades': state['trades'] + 1})
        state['trades_log'].append({'time': candle['time'], 'action': action, 'price': price})
        if mode == 'live' and place_order_fn:            place_order_fn(action)
        if mode in ('live', 'paper') and send_telegram_fn: send_telegram_fn(f"{action} @ {price}  PnL={pnl:+.2f}%")
        logging.info(f"{action} @ {price}  PnL={pnl:+.2f}%")

    if signal == 'LONG' and not state['side']:
        state.update({'side': 'long', 'entry': price})
        state['trades_log'].append({'time': candle['time'], 'action': 'BUY', 'price': price})
        if mode == 'live' and place_order_fn:            place_order_fn('BUY')
        if mode in ('live', 'paper') and send_telegram_fn: send_telegram_fn(f"BUY @ {price}")
        logging.info(f"BUY @ {price}")

    elif signal == 'SHORT' and not state['side']:
        state.update({'side': 'short', 'entry': price})
        state['trades_log'].append({'time': candle['time'], 'action': 'SHORT', 'price': price})
        if mode == 'live' and place_order_fn:            place_order_fn('SHORT')
        if mode in ('live', 'paper') and send_telegram_fn: send_telegram_fn(f"SHORT @ {price}")
        logging.info(f"SHORT @ {price}")
