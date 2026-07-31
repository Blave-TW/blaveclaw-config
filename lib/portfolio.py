import glob, inspect, json, logging, os
from datetime import datetime

from lib import guard


def _append_reconciler_log(order):
    os.makedirs('manager', exist_ok=True)
    entry = {'ts': datetime.utcnow().isoformat(), **order}
    with open('manager/orders.jsonl', 'a') as f:
        f.write(json.dumps(entry) + '\n')


def _write_reconcile_snapshot(target, actual, orders):
    """Record what this reconcile actually saw, for anything that needs to show
    live positions without querying the exchange itself.

    Targets are cheap to recompute anywhere (aggregate_portfolio is pure local
    arithmetic); exchange positions are not — they need the user's keys and a
    round-trip. So this is the only place `actual` is ever observed, and without
    persisting it the workspace could only ever show half the picture.

    Best-effort: a failure here must never stop a reconcile that already placed
    orders.
    """
    try:
        os.makedirs('manager', exist_ok=True)
        with open('manager/last_reconcile.json', 'w') as f:
            json.dump({
                'ts':     datetime.utcnow().isoformat(),
                'target': target,
                'actual': actual,
                'orders': orders,
            }, f, indent=2)
    except Exception as e:
        logging.warning(f'failed to write manager/last_reconcile.json: {e}')


def load_portfolio_config():
    """Load portfolio_config.json from manager/ directory."""
    path = 'manager/portfolio_config.json'
    if not os.path.exists(path):
        return {'account_value': 0, 'weights': {}, 'exchanges': {}, 'asset_specs': {}}
    with open(path) as f:
        return json.load(f)


def portfolio_members():
    """Strategy names that belong to the portfolio — the keys of
    portfolio_config["exchanges"].

    There is deliberately no separate membership list. `exchanges` is already
    the hand-maintained record of "this strategy is deployed to trade on X",
    already the thing manager.py never overwrites, and already what decides
    whether a strategy trades. Making it decide weighting too means one list
    instead of two that can disagree.

    Returns None when nothing has been routed yet — "no list has been drawn up"
    rather than "the list is empty". Callers then weight everything, which is
    the behaviour that existed before members did, so a fresh machine still
    works before the user has deployed anything.

    Why it matters: weights sum to 1 across whatever goes into the optimiser.
    A backtest-only experiment left in that pool takes a share of the capital
    purely by existing, and the strategies actually trading get sized down for
    it — silently, since nothing anywhere reports that split.
    """
    exchanges = load_portfolio_config().get('exchanges') or {}
    return set(exchanges) or None


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

    exchange and asset_spec are taken from portfolio_config — not from state.json.
    asset_spec: None = fractional sizing (qty = abs(signed_diff) / price). Example for futures:
      {"type": "futures_contracts", "contract_value": 200,
       "currency": "TWD", "lot_size": 1}

    Strategies not listed in weights, missing symbol, or missing exchange in config are skipped.
    """
    config        = load_portfolio_config()
    account_value = float(config.get('account_value', 0))
    leverage      = float(config.get('leverage', 1.0))
    weights       = config.get('weights', {})
    exchanges     = config.get('exchanges', {})
    asset_specs   = config.get('asset_specs', {})
    states        = load_all_states()
    totals        = {}

    for name, state in states.items():
        symbol     = state.get('symbol')
        exchange   = exchanges.get(name)
        position   = float(state.get('position', 0))
        weight     = float(weights.get(name, 0))
        asset_spec = asset_specs.get(name)

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
      asset_spec → passed through from portfolio_config for place_order to use
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

    place_order_fn(symbol, signed_diff, asset_spec, reduce_only=False):
      signed_diff > 0 → buy  (increase long / reduce short)
      signed_diff < 0 → sell (increase short / reduce long)
      reduce_only=True → close-only leg of a position flip; pass to exchange reduce-only flag.
      asset_spec: dict from portfolio_config["asset_specs"], or None for default (fractional qty, no lot constraint).
        Use it to convert signed_diff → native qty/contracts/lots.

    Position flips (long→short or short→long) are split into two calls:
      1. place_order_fn(symbol, -actual, asset_spec, reduce_only=True)   ← close existing
      2. place_order_fn(symbol, target,  asset_spec, reduce_only=False)  ← open new
    This prevents simultaneous long+short on hedge-mode exchanges (OKX 兩倉模式, etc.).
    If place_order_fn does not accept reduce_only, the kwarg is silently dropped.

    Kill switch: while state/HALT exists, legs that ADD exposure are denied here
    (audited to state/audit.jsonl) before place_order_fn is called. Legs that
    reduce or close a position always go through — see lib/guard.py.

    Returns list of orders placed.
    """
    config  = load_portfolio_config()
    msgs    = config.get('messages', {})

    def _msg(key, default, **kw):
        return msgs.get(key, default).format(**kw)

    target = aggregate_portfolio()
    actual = get_positions_fn()
    orders = compute_diff(target, actual, threshold)

    # Written before placing, so it records the state that WAS acted on. A
    # reconcile that crashes mid-loop still leaves the observation behind.
    _write_reconcile_snapshot(target, actual, orders)

    # Checked once via signature inspection (not a runtime try/except TypeError) so a
    # TypeError raised *after* place_order_fn already submitted the order — e.g. while
    # processing the exchange response — can't be misread as "no reduce_only support"
    # and trigger a second, duplicate submission.
    try:
        _place_params = inspect.signature(place_order_fn).parameters
        _supports_reduce_only = 'reduce_only' in _place_params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in _place_params.values()
        )
    except (TypeError, ValueError):
        _supports_reduce_only = False

    def _call_place(symbol, sub_diff, asset_spec, reduce_only):
        # Returns False if place_order_fn skipped the order (e.g. below exchange minimum).
        # Returns None/truthy on success. Propagates exceptions on failure.
        if _supports_reduce_only:
            return place_order_fn(symbol, sub_diff, asset_spec, reduce_only=reduce_only)
        return place_order_fn(symbol, sub_diff, asset_spec)

    for order in orders:
        symbol     = order['symbol']
        diff       = order['signed_diff']
        asset_spec = order.get('asset_spec')

        # Detect position flip: split into reduce-only close + directional open
        # to avoid simultaneous long+short on hedge-mode exchanges.
        a        = actual.get(symbol, {})
        a_signed = (a.get('size', 0)  if a.get('side') == 'long'  else
                    -a.get('size', 0) if a.get('side') == 'short' else 0)
        t_signed = a_signed + diff

        # Third element is is_entry — whether the leg ADDS exposure, which is the
        # only thing state/HALT blocks. A flip's second leg always does; a plain
        # adjustment only when it grows the position (|target| > |actual|), so a
        # halt never traps a position that is being reduced.
        if a_signed != 0 and t_signed != 0 and t_signed * a_signed < 0:
            sub_orders = [(-a_signed, True, False), (t_signed, False, True)]
        else:
            sub_orders = [(diff, False, abs(t_signed) > abs(a_signed))]

        failed = False
        for sub_diff, reduce_only, is_entry in sub_orders:
            if abs(sub_diff) < threshold:
                continue

            # Kill switch, enforced here rather than only in lib/order_*.py: every
            # reconciler goes through this function, including the hand-written
            # place_order_fn of an exchange that has no official lib/order_* module.
            # Deliberately silent on Telegram — the user tripped the halt, and a
            # denial every poll is the noise they were trying to stop.
            if is_entry and guard.halted():
                guard.audit('order_denied_halt', symbol=symbol, signed_diff=sub_diff,
                            exchange=order.get('exchange'), source='reconcile')
                logging.warning(
                    f"[reconcile] {symbol} entry {sub_diff:+.2f} denied — state/HALT is set "
                    f"({guard.halt_info()})"
                )
                failed = True
                break

            try:
                placed = _call_place(symbol, sub_diff, asset_spec, reduce_only)
            except Exception as e:
                log_msg = f"order error {symbol}: {e}"
                logging.error(log_msg)
                if send_telegram_fn:
                    send_telegram_fn(_msg('order_error', '⚠️ Order failed {symbol}: {error}',
                                         symbol=symbol, error=e))
                failed = True
                break

            if placed is False:
                # place_order skipped (e.g. qty below exchange minimum) — no notification or log
                logging.info(f"[reconcile] {symbol} skipped by place_order — below exchange minimum")
                continue

            if reduce_only:
                key     = 'order_close_long'  if sub_diff < 0 else 'order_close_short'
                default = '📉 Closed long {symbol} ${amount:.2f}' if sub_diff < 0 else '📈 Closed short {symbol} ${amount:.2f}'
            else:
                key     = 'order_buy' if sub_diff > 0 else 'order_sell'
                default = '📈 Bought {symbol} ${amount:.2f}' if sub_diff > 0 else '📉 Sold {symbol} ${amount:.2f}'

            log_dir = 'BUY' if sub_diff > 0 else 'SELL'
            logging.info(f"{log_dir}{'(reduce)' if reduce_only else ''} {symbol} {abs(sub_diff):.2f}")
            if send_telegram_fn:
                send_telegram_fn(_msg(key, default, symbol=symbol, amount=abs(sub_diff)))

        if not failed:
            direction = 'BUY' if diff > 0 else 'SELL'
            _append_reconciler_log({
                'action':      direction,
                'symbol':      symbol,
                'signed_diff': diff,
                'exchange':    order.get('exchange'),
                'asset_spec':  asset_spec,
                'contributors': order.get('contributors', []),
            })

    return orders
