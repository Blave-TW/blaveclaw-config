import json, logging, math, os


def load_state(strategy_name):
    path = f'strategies/{strategy_name}/state.json'
    return json.load(open(path)) if os.path.exists(path) else None


def save_state(strategy_name, state):
    json.dump(state, open(f'strategies/{strategy_name}/state.json', 'w'), indent=2)


def update_state(candle, signal, state, mode, symbol=None, exchange=None,
                 send_telegram_fn=None):
    """Process one candle: update position state only. Orders are placed by the reconciler."""
    price    = candle['close']
    prev_pos = float(state.get('position', 0))
    new_pos  = float(signal)
    if symbol:   state['symbol']   = symbol
    if exchange: state['exchange'] = exchange
    if math.isnan(new_pos):
        return                       # nan = hold: keep current position unchanged

    def _log(action):
        logging.info(f"{action} @ {price}")
        if mode == 'live' and send_telegram_fn:
            send_telegram_fn(f"Signal: {action} @ {price}")

    # Close or flip
    if prev_pos != 0 and (new_pos == 0 or new_pos * prev_pos < 0):
        action = 'SELL' if prev_pos > 0 else 'COVER'
        state['position'] = 0.0
        _log(action)

    # Open or scale
    if new_pos != 0:
        if state['position'] == 0:
            action = 'BUY' if new_pos > 0 else 'SHORT'
            state['position'] = new_pos
            _log(action)
        elif new_pos != prev_pos:
            state['position'] = new_pos
            logging.info(f"SCALE {prev_pos:+.2f}→{new_pos:+.2f} @ {price}")
