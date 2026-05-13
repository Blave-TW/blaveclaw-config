import json, logging, math, os



def bootstrap(df, signal_fn):
    """Replay historical candles to find initial position state (no orders placed).
    df: DataFrame with OHLCV + indicator columns (output of add_indicators)
    signal_fn: compute_signal function, accepts a pd.Series row
    """
    state = {'position': 0.0, 'entry': None}
    for ts, row in df.iloc[:-1].iterrows():
        p        = float(row['Close'])
        prev_pos = state['position']
        new_pos  = float(signal_fn(row))
        if math.isnan(new_pos):
            continue

        if prev_pos != 0 and (new_pos == 0 or new_pos * prev_pos < 0):
            state.update({'position': 0.0, 'entry': None})

        if new_pos != 0:
            if state['position'] == 0:
                state.update({'position': new_pos, 'entry': p})
            elif new_pos != prev_pos:
                state['position'] = new_pos

    logging.info(f"Bootstrapped: position={state['position']} entry={state['entry']}")
    return state


def load_state(strategy_name):
    path = f'strategies/{strategy_name}/state.json'
    return json.load(open(path)) if os.path.exists(path) else None


def save_state(strategy_name, state):
    json.dump(state, open(f'strategies/{strategy_name}/state.json', 'w'), indent=2)


def update_state(candle, signal, state, mode, symbol=None, exchange=None, send_telegram_fn=None):
    """Process one candle: update position state only. Orders are placed by the reconciler."""
    price    = candle['close']
    prev_pos = float(state.get('position', 0))
    new_pos  = float(signal)
    if math.isnan(new_pos):
        return                       # nan = hold: keep current position unchanged

    if symbol:   state['symbol']   = symbol
    if exchange: state['exchange'] = exchange

    # Close or flip
    if prev_pos != 0 and (new_pos == 0 or new_pos * prev_pos < 0):
        action = 'SELL' if prev_pos > 0 else 'COVER'
        state.update({'position': 0.0, 'entry': None})
        if mode in ('live', 'paper') and send_telegram_fn:
            send_telegram_fn(f"Signal: {action} @ {price}")
        logging.info(f"{action} @ {price}")

    # Open or scale
    if new_pos != 0:
        if state['position'] == 0:
            action = 'BUY' if new_pos > 0 else 'SHORT'
            state.update({'position': new_pos, 'entry': price})
            if mode in ('live', 'paper') and send_telegram_fn:
                send_telegram_fn(f"Signal: {action} @ {price}")
            logging.info(f"{action} @ {price}")
        elif new_pos != prev_pos:
            state['position'] = new_pos
            logging.info(f"SCALE {prev_pos:+.2f}→{new_pos:+.2f} @ {price}")
