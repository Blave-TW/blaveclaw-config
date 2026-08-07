import glob, inspect, json, logging, os, re
from datetime import datetime

from lib import guard


def _append_reconciler_log(order):
    # Best-effort like the other two writers: a full disk (measured on the
    # fleet) must not abort the reconcile loop mid-round — the order already
    # happened, losing the log line is the lesser failure.
    try:
        os.makedirs('manager', exist_ok=True)
        entry = {'ts': datetime.utcnow().isoformat(), **order}
        with open('manager/orders.jsonl', 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except OSError as e:
        logging.error(f"orders.jsonl append failed: {e}")


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


def _record_order_error(symbol, exchange, error):
    """Last few order failures, for the workspace page — a reconciler that
    fails silently in a tmux log is indistinguishable from one that never
    tried (measured UX complaint). Best-effort; keeps the newest 5."""
    try:
        path = 'manager/order_errors.json'
        try:
            with open(path) as f:
                rows = json.load(f)
        except (OSError, ValueError):
            rows = []
        rows.append({'ts': datetime.utcnow().isoformat(), 'symbol': symbol,
                     'exchange': exchange, 'error': str(error)[:200]})
        with open(path, 'w') as f:
            json.dump(rows[-5:], f, indent=2)
    except Exception as e:
        logging.warning(f'failed to record order error: {e}')


# ── market dimension (spot vs swap) ─────────────────────────────────────────
# A strategy declares MARKET = "spot" in its strategy.py (same constant the
# platform's portfolio_reporter reads); undeclared = "swap" — every pre-2026-08
# fleet strategy is a perp strategy, so the default keeps them unchanged.
# Spot and swap flows through the SAME reconcile pipeline, distinguished by the
# target/actual KEY: swap keys stay the plain canonical symbol ("BTCUSDT",
# backward compatible with every existing snapshot/consumer), spot keys carry
# an "@spot" suffix ("BTCUSDT@spot"). place_order wirings split the key via
# split_key() and route to the venue's spot or perp execution.

_MARKET_RE = re.compile(r'^\s*MARKET\s*=\s*["\']([a-z]+)["\']', re.M)
SPOT_SUFFIX = '@spot'


def strategy_market(name):
    """'spot' | 'swap' from the strategy.py MARKET constant (default swap)."""
    try:
        with open(f'strategies/{name}/strategy.py') as f:
            m = _MARKET_RE.search(f.read())
        return m.group(1) if m else 'swap'
    except OSError:
        return 'swap'


def market_key(symbol, market):
    """Canonical reconcile key: plain symbol for swap, symbol@spot for spot."""
    return symbol + SPOT_SUFFIX if market == 'spot' else symbol


def split_key(key):
    """'BTCUSDT@spot' -> ('BTCUSDT', 'spot'); 'BTCUSDT' -> ('BTCUSDT', 'swap')."""
    key = str(key)
    if key.endswith(SPOT_SUFFIX):
        return key[:-len(SPOT_SUFFIX)], 'spot'
    return key, 'swap'


def spot_symbols():
    """Plain symbols whose SPOT inventory the reconciler must read = symbols of
    funded spot strategies (amount != 0 — position 0 still needs the inventory
    visible, or a target that drops to 0 could never sell down). SCOPING RULE:
    only these ever enter `actual` — personal coins in any other symbol must
    never grow sell orders."""
    config = load_portfolio_config()
    amounts = strategy_amounts(config)
    exchanges = config.get('exchanges', {})
    out = set()
    for name, state in load_all_states().items():
        sym = (state.get('symbol') or '').replace('-', '').upper()
        if sym and exchanges.get(name) and float(amounts.get(name, 0)) != 0 \
                and strategy_market(name) == 'spot':
            out.add(sym)
    return out


_SPOT_SCOPE_PATH = 'manager/spot_scope.json'


# threshold default MUST track manager/reconciler.py THRESHOLD (both 10):
# a scope threshold above the reconcile one strands inventory in scope forever
# (rows below reconcile threshold never sell, never leave scope) — audit L1
def spot_scope(inventory_value_fn, threshold=10):
    """{symbol: usd_value} of every spot inventory the reconciler must treat
    as `actual` — and the rule that makes REMOVING a spot strategy exit its
    position instead of orphaning it (futures parity: strategy leaves →
    position closes).

    Scope = funded spot strategies (spot_symbols) ∪ previously-managed symbols
    persisted in manager/spot_scope.json. A symbol leaves the persisted scope
    only when its inventory value drops below the reconcile threshold — so
    after removal the actual-only row keeps generating sell orders until the
    inventory is gone. Personal coins in symbols no strategy targets never
    enter; but a targeted symbol's spot inventory is ONE pool — coins the
    user holds in the same symbol are co-managed with the strategy's (incl.
    the removal sell-down). Say so when a user asks; it is the accepted
    trade-off of inventory-based reconciling (audit M5).
    inventory_value_fn(symbol) -> USD value; its errors PROPAGATE
    (a failed read silently dropping a symbol from scope would strand
    inventory — same error contract as get_positions)."""
    targeted = spot_symbols()
    try:
        with open(_SPOT_SCOPE_PATH) as f:
            prev = set(json.load(f))
    except (OSError, ValueError):
        prev = set()
    values = {}
    for sym in sorted(targeted | prev):
        values[sym] = float(inventory_value_fn(sym))
    keep = targeted | {s for s, v in values.items() if v >= threshold}
    try:  # best-effort persist — next round recomputes from targets anyway
        os.makedirs('manager', exist_ok=True)
        with open(_SPOT_SCOPE_PATH, 'w') as f:
            json.dump(sorted(keep), f)
    except OSError as e:
        logging.warning(f'spot_scope persist failed: {e}')
    return values


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


def strategy_amounts(config=None):
    """{strategy: dollars} — the per-strategy sizing base.

    `amounts` is canonical (2026-08-03: 金額是介面也是儲存 — what the user
    typed is what sizes positions, and it never drifts with equity). Configs
    from before the change have no `amounts`; for those, fall back to the old
    account_value × leverage × weight expression so existing deployments keep
    trading identically until they are re-saved from the web.
    """
    config = config if config is not None else load_portfolio_config()
    amounts = config.get('amounts')
    if isinstance(amounts, dict):
        return {k: float(v) for k, v in amounts.items()}
    account_value = float(config.get('account_value', 0))
    leverage      = float(config.get('leverage', 1.0))
    weights       = config.get('weights', {}) or {}
    return {k: account_value * leverage * float(w) for k, w in weights.items()}


def aggregate_portfolio():
    """
    Aggregate all strategy states into net target positions using portfolio config.

    target[symbol] = Σ(amount_i × position_i)  (in account currency)

    amount_i is the strategy's dollar allocation (portfolio_config["amounts"],
    see strategy_amounts) — "what this strategy trades with at position=1";
    position is the strategy's signal and may be a fractional scaling factor
    (vol-scaled strategies emit 0.5, 1.8, …), not just ±1.

    Returns {symbol: {'side': 'long'|'short'|None, 'size': float,
                       'exchange': str, 'asset_spec': dict|None}}

    exchange and asset_spec are taken from portfolio_config — not from state.json.
    asset_spec: None = fractional sizing (qty = abs(signed_diff) / price). Example for futures:
      {"type": "futures_contracts", "contract_value": 200,
       "currency": "TWD", "lot_size": 1}

    Strategies with no amount, missing symbol, or missing exchange in config are skipped.
    """
    config        = load_portfolio_config()
    amounts       = strategy_amounts(config)
    exchanges     = config.get('exchanges', {})
    asset_specs   = config.get('asset_specs', {})
    states        = load_all_states()
    totals        = {}

    for name, state in states.items():
        symbol     = state.get('symbol')
        # canonical symbol key: dashless uppercase (BTCUSDT) — strategies write
        # Binance-style, OKX reports dashed; without one canon the reconciler
        # sees "BTCUSDT target" and "BTC-USDT actual" as two symbols and churns
        symbol     = symbol.replace('-', '').upper() if symbol else symbol
        exchange   = exchanges.get(name)
        position   = float(state.get('position', 0))
        amount     = float(amounts.get(name, 0))
        asset_spec = asset_specs.get(name)

        if not symbol or not exchange or amount == 0:
            continue

        contribution = amount * position

        # spot and swap are different inventories — same symbol, different key,
        # so a spot strategy and a perp strategy on BTCUSDT never net against
        # each other (they'd converge the WRONG account's position)
        market = strategy_market(name)
        key = market_key(symbol, market)

        if key not in totals:
            totals[key] = {'signed': 0.0, 'exchange': exchange,
                           'asset_spec': asset_spec, 'market': market,
                           'contributors': []}
        totals[key]['signed'] += contribution
        totals[key]['contributors'].append({
            'strategy':          name,
            'position':          position,
            'amount':            amount,
            'contribution': round(contribution, 4),
        })

    result = {}
    for key, data in totals.items():
        s = data['signed']
        if data['market'] == 'spot' and s < 0:
            # spot cannot short — a net-negative spot target is clamped to
            # flat, loudly: the strategy author meant short exposure the venue
            # cannot express, silence would misreport what is being traded
            logging.warning(
                f"[portfolio] {key}: net target {s:.2f} is SHORT on a spot "
                f"market — clamped to 0 (spot cannot short)")
            s = 0.0
        result[key] = {
            'side':         'long' if s > 0 else ('short' if s < 0 else None),
            'size':    abs(s),
            'exchange':     data['exchange'],
            'asset_spec':   data['asset_spec'],
            'market':       data['market'],
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
            'market':           split_key(symbol)[1],
            'signed_diff': diff,
            # Symbols present only in `actual` (closing a strategy that left the
            # portfolio) have no target entry. Auto-wired get_positions does
            # NOT tag rows with a venue, so this is often None there — the
            # order log's legs carry the venue the wiring actually routed to,
            # which is the truthful record anyway.
            'exchange':         t.get('exchange') or a.get('exchange'),
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

    Returns the orders that got AT LEAST ONE confirmed fill this round — not
    every order attempted. Callers use truthiness to schedule an immediate
    convergence re-run (force_next); returning failed orders too would turn a
    persistent failure (bad key, insufficient margin) into a poll-interval
    retry storm — failures instead wait for the next state change / heartbeat.
    """
    config  = load_portfolio_config()
    msgs    = config.get('messages', {})

    def _msg(key, default, **kw):
        return msgs.get(key, default).format(**kw)

    target = aggregate_portfolio()
    actual = get_positions_fn()
    # Defense in depth: target keys are canonical (dashless uppercase) since
    # aggregate; an account lib / hand-written get_positions returning venue
    # format ('BTC-USDT') would otherwise split one instrument into two rows —
    # buy leg on one, close leg on the other, churning fees every round (and
    # under HALT the close leg still passes: it would quietly flatten a real
    # position). Normalize here so no wiring mistake can reach compute_diff.
    def _canon_key(k):
        sym, market = split_key(str(k))
        return market_key(sym.replace('-', '').upper(), market)
    actual = {_canon_key(k): v for k, v in (actual or {}).items()}
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
        _has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in _place_params.values()
        )
        _supports_reduce_only = 'reduce_only' in _place_params or _has_var_kw
        # exchange passthrough lets the wiring refuse a target routed to a
        # DIFFERENT live venue (venue_wiring audit H3) — older hand-wired
        # place_order fns don't take it, same signature-sniff as reduce_only
        _supports_exchange = 'exchange' in _place_params or _has_var_kw
        # contributors passthrough lets lib.execute resolve the per-strategy
        # execution style (市價/TWAP/custom) for this netted order — again
        # optional, so hand-wired place_order fns keep working untouched
        _supports_contributors = 'contributors' in _place_params or _has_var_kw
    except (TypeError, ValueError):
        _supports_reduce_only = False
        _supports_exchange = False
        _supports_contributors = False

    def _call_place(symbol, sub_diff, asset_spec, reduce_only, exchange=None,
                    contributors=None):
        # Returns False if place_order_fn skipped the order (e.g. below exchange minimum).
        # Returns None/truthy on success. Propagates exceptions on failure.
        kw = {}
        if _supports_reduce_only:
            kw['reduce_only'] = reduce_only
        if _supports_exchange:
            kw['exchange'] = exchange
        if _supports_contributors:
            kw['contributors'] = contributors
        if kw:
            return place_order_fn(symbol, sub_diff, asset_spec, **kw)
        return place_order_fn(symbol, sub_diff, asset_spec)

    executed = []  # orders with ≥1 confirmed fill — the return value
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
            # Same-side shrink (incl. full close) is ALSO reduce-only. On a
            # one-way account a plain opposite-side order nets, so this flag
            # was cosmetic — on a hedge-mode account (OKX 雙向) it is the whole
            # difference between reducing the long and opening a fresh short
            # (measured live: a $44 "reduce" opened $44 of shorts, twice).
            shrink = (a_signed != 0 and abs(t_signed) < abs(a_signed)
                      and t_signed * a_signed >= 0)
            sub_orders = [(diff, shrink, abs(t_signed) > abs(a_signed))]

        failed = False
        legs = []  # per-leg exchange-confirmed fills for orders.jsonl / the web 交易歷史
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
                placed = _call_place(symbol, sub_diff, asset_spec, reduce_only,
                                     exchange=order.get('exchange'),
                                     contributors=order.get('contributors'))
            except Exception as e:
                log_msg = f"order error {symbol}: {e}"
                logging.error(log_msg)
                _record_order_error(symbol, order.get('exchange'), e)
                if send_telegram_fn:
                    send_telegram_fn(_msg('order_error', '⚠️ Order failed {symbol}: {error}',
                                         symbol=symbol, error=e))
                failed = True
                break

            if placed is False:
                # place_order skipped: qty below exchange minimum, or the leg was
                # handed to / deferred behind an async executor (lib.execute) —
                # either way nothing filled synchronously, nothing to log here
                logging.info(f"[reconcile] {symbol} skipped by place_order — "
                             f"below minimum or async execution in flight")
                continue

            # 進出場價格:order libs return the exchange-confirmed fill — record
            # it per leg (a flip = close leg 出場價 + open leg 進場價). Older
            # place_order_fn returning bare None still logs the leg, just bare.
            leg = {'signed_diff': round(sub_diff, 2), 'reduce_only': reduce_only}
            if isinstance(placed, dict):
                for src, dst in (('avg_price', 'fill_price'),
                                 ('executed_qty', 'executed_qty'),
                                 ('exchange', 'exchange')):
                    if placed.get(src) is not None:
                        leg[dst] = placed[src]
            legs.append(leg)

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

        # Log whenever ANYTHING filled — a flip whose close leg executed but
        # whose entry leg then failed (or was halted) moved real money; hiding
        # it because the order "failed" would desync the history from the
        # exchange. `failed` marks the partial. Nothing filled + nothing
        # failed = every leg skipped below-min: no phantom entry.
        if legs:
            direction = 'BUY' if diff > 0 else 'SELL'
            entry = {
                'action':      direction,
                'symbol':      symbol,
                'signed_diff': diff,
                # place_order knows which venue it actually routed to — trust
                # the fill over the config when both exist
                'exchange':    next((l.get('exchange') for l in legs if l.get('exchange')),
                                    order.get('exchange')),
                'asset_spec':  asset_spec,
                'contributors': order.get('contributors', []),
                'legs':        legs,
            }
            if failed:
                entry['failed'] = True
            _append_reconciler_log(entry)
            executed.append(order)

    return executed
