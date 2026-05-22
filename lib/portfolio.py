import glob, json, logging, os
from datetime import datetime


def _append_reconciler_log(order):
    os.makedirs('manager', exist_ok=True)
    entry = {'ts': datetime.utcnow().isoformat(), **order}
    with open('manager/orders.jsonl', 'a') as f:
        f.write(json.dumps(entry) + '\n')


def load_portfolio_config():
    """Load portfolio_config.json from manager/ directory."""
    path = 'manager/portfolio_config.json'
    if not os.path.exists(path):
        return {'account_value': 0, 'weights': {}}
    with open(path) as f:
        return json.load(f)


def load_all_states():
    """Load all strategy state files. Returns {strategy_name: state_dict}."""
    states = {}
    for path in glob.glob('strategies/*/state.json'):
        name = os.path.basename(os.path.dirname(path))
        try:
            with open(path) as f:
                states[name] = json.load(f)
        except Exception as e:
            logging.warning(f"Failed to load state {path}: {e}")
    return states


def aggregate_portfolio():
    """
    Aggregate all strategy states into net target positions using portfolio config.

    target[symbol] = account_value × Σ(weight_i × position_i)  (in account currency)

    Returns {symbol: {'side': 'long'|'short'|None, 'size': float,
                       'exchange': str, 'asset_spec': dict|None}}

    asset_spec is taken from state.json and passed through to place_order unchanged.
    None means default: qty = abs(signed_diff) / price (fractional, no lot constraint). Example for futures:
      {"type": "futures_contracts", "contract_value": 200,
       "currency": "TWD", "lot_size": 1}

    Strategies not listed in weights or missing symbol/exchange are skipped.
    """
    config        = load_portfolio_config()
    account_value = float(config.get('account_value', 0))
    leverage      = float(config.get('leverage', 1.0))
    weights       = config.get('weights', {})
    states        = load_all_states()
    totals        = {}

    for name, state in states.items():
        symbol     = state.get('symbol')
        exchange   = state.get('exchange')
        position   = float(state.get('position', 0))
        weight     = float(weights.get(name, 0))
        asset_spec = state.get('asset_spec')

        if not symbol or not exchange or weight == 0:
            continue

        contribution = account_value * leverage * weight * position

        if symbol not in totals:
            totals[symbol] = {'signed': 0.0, 'exchange': exchange,
                              'asset_spec': asset_spec, 'contributors': []}
        totals[symbol]['signed'] += contribution
        totals[symbol]['contributors'].append({
            'strategy':          name,
            'position':          position,
            'weight':            weight,
            'contribution': round(contribution, 4),
        })

    result = {}
    for symbol, data in totals.items():
        s = data['signed']
        result[symbol] = {
            'side':         'long' if s > 0 else ('short' if s < 0 else None),
            'size':    abs(s),
            'exchange':     data['exchange'],
            'asset_spec':   data['asset_spec'],
            'contributors': data['contributors'],
        }
    return result


def compute_diff(target, actual, threshold=10):
    """
    Compute required position adjustments.
    target:  output of aggregate_portfolio()
    actual:  {symbol: {'side': 'long'|'short'|None, 'size': float}}
    Returns: list of {symbol, signed_diff, exchange, asset_spec}
      signed_diff > 0 → need to buy
      signed_diff < 0 → need to sell/short
      asset_spec → passed through from state.json for place_order to use
    """
    orders = []
    all_symbols = set(target) | set(actual)

    for symbol in all_symbols:
        t = target.get(symbol, {'side': None, 'size': 0, 'exchange': None, 'asset_spec': None})
        a = actual.get(symbol, {'side': None, 'size': 0})

        t_signed = (t['size']         if t.get('side') == 'long'  else
                    -t['size']        if t.get('side') == 'short' else 0)
        a_signed = (a.get('size', 0)  if a.get('side') == 'long'  else
                    -a.get('size', 0) if a.get('side') == 'short' else 0)

        diff = t_signed - a_signed
        if abs(diff) < threshold:
            continue

        orders.append({
            'symbol':           symbol,
            'signed_diff': diff,
            'exchange':         t.get('exchange'),
            'asset_spec':       t.get('asset_spec'),
            'contributors':     t.get('contributors', []),
        })

    return orders


def reconcile(get_positions_fn, place_order_fn, threshold=10, send_telegram_fn=None):
    """
    Full reconciliation cycle:
      1. aggregate_portfolio() → target (weighted positions × account value, in account currency)
      2. get_positions_fn()    → actual exchange positions (in account currency)
      3. compute_diff()        → place orders

    place_order_fn(symbol, signed_diff, asset_spec):
      signed_diff > 0 → buy  (increase long / reduce short)
      signed_diff < 0 → sell (increase short / reduce long)
      asset_spec: dict from state.json, or None for default (fractional qty, no lot constraint).
        Use it to convert signed_diff → native qty/contracts/lots.

    Returns list of orders placed.
    """
    target = aggregate_portfolio()
    actual = get_positions_fn()
    orders = compute_diff(target, actual, threshold)

    for order in orders:
        symbol     = order['symbol']
        diff       = order['signed_diff']
        asset_spec = order.get('asset_spec')
        try:
            place_order_fn(symbol, diff, asset_spec)
        except Exception as e:
            err_msg = f"[reconciler] ERROR {symbol}: {e}"
            logging.error(err_msg)
            if send_telegram_fn:
                send_telegram_fn(err_msg)
            continue
        direction = 'BUY' if diff > 0 else 'SELL'
        msg = f"[reconciler] {direction} {symbol} {abs(diff):.2f}"
        logging.info(msg)
        if send_telegram_fn:
            send_telegram_fn(msg)
        _append_reconciler_log({
            'action':           direction,
            'symbol':           symbol,
            'signed_diff': diff,
            'exchange':         order.get('exchange'),
            'asset_spec':       asset_spec,
            'contributors':     order.get('contributors', []),
        })

    return orders
