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


def _write_reconcile_snapshot(target, actual, orders, ledger=None):
    """Record what this reconcile actually saw, for anything that needs to show
    live positions without querying the exchange itself.

    Targets are cheap to recompute anywhere (aggregate_portfolio is pure local
    arithmetic); exchange positions are not — they need the user's keys and a
    round-trip. So this is the only place `actual` is ever observed, and without
    persisting it the workspace could only ever show half the picture.

    `ledger` (self_ledger.ledger_positions() output, or None when the feature is
    off) is recorded alongside `actual` so a future workspace view can show
    "bot's own book" vs "exchange's real position" side by side instead of only
    ever seeing whichever one reconcile() happened to diff against.

    Best-effort: a failure here must never stop a reconcile that already placed
    orders.
    """
    try:
        os.makedirs('manager', exist_ok=True)
        doc = {
            'ts':     datetime.utcnow().isoformat(),
            'target': target,
            'actual': actual,
            'orders': orders,
        }
        if ledger is not None:
            doc['ledger'] = ledger
        with open('manager/last_reconcile.json', 'w') as f:
            json.dump(doc, f, indent=2)
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


# ── self-ledger (bot's own tracked position, decoupled from the exchange's
#    raw account read — see portfolio_config.json["self_ledger"]) ──────────
# Root problem: get_positions_fn() reads the ACCOUNT's real position, which on
# a single-account setup includes whatever the user opened by hand — reconcile()
# then reads that manual position as "already have it" and trades against it
# (adds to it, or closes it down to target). self_ledger fixes this not by
# reading the account differently, but by not reading it AT ALL for diffing:
# the bot instead tracks what IT has bought/sold from its own order log
# (manager/orders.jsonl, already written by _append_reconciler_log below) and
# diffs target against that running total. A position opened outside this
# process is invisible to the ledger by construction, so it can never be
# absorbed. Trade-off (see references/manager.md § self_ledger): the ledger can
# drift from the real account (missed fills, manual trades on the same symbol)
# — this only fixes "bot ignores what it doesn't own", not "bot always knows
# the true account state"; margin/liquidation checks still need the real
# get_positions_fn() read, never the ledger.
_LEDGER_SEED_PATH = 'manager/ledger_seed.json'
_ORDERS_LOG_PATH  = 'manager/orders.jsonl'


def _load_ledger_seed():
    """{'seeded_at': iso_str, 'symbols': {symbol: {'size': signed_float,
    'ts': iso_str}}} — 'seeded_at' is the whole-account cutoff from
    seed_ledger(): orders.jsonl entries at/before it are excluded for EVERY
    symbol, including symbols with no per-symbol row (a flat account seeds an
    empty symbols map, but its history must still be cut off — measured live
    2026-08-20: without this, a flat-account seed summed the box's prior
    test-trade history into a phantom position). A per-symbol 'ts'
    (zero_ledger_symbols) overrides the global cutoff for that symbol.
    Corrupt/missing file reads as no seed at all: every entry in orders.jsonl
    counts, from the start of the file."""
    empty = {'seeded_at': '', 'symbols': {}}
    try:
        with open(_LEDGER_SEED_PATH) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return empty
        return {
            'seeded_at': str(raw.get('seeded_at') or ''),
            'symbols': {k: {'size': float(v.get('size', 0) or 0),
                            'ts': str(v.get('ts') or '')}
                        for k, v in (raw.get('symbols') or {}).items()},
        }
    except (OSError, ValueError, AttributeError):
        return empty


def _save_ledger_seed(seed):
    # tmp+replace (same convention as lib/guard.py, manager/reconciler.py's
    # last-order marker): flatten() writes this file while the reconciler's
    # 5s poll may be mid-read — a half-written file parses as "no seed at
    # all", which ledger_positions() degrades into summing the entire
    # orders.jsonl from the top (a much larger phantom ledger), and that
    # mis-read would land exactly during a panic close-all.
    os.makedirs('manager', exist_ok=True)
    tmp = _LEDGER_SEED_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(seed, f, indent=2)
    os.replace(tmp, _LEDGER_SEED_PATH)


def seed_ledger(get_positions_fn, absorb=False):
    """One-time baseline for the self-ledger. Two modes — picking the wrong
    one on an account that HOLDS positions trades real money, so the default
    is the one that never touches what the user already owns:

    Default (absorb=False, "fresh start"): everything currently on the
    account belongs to the USER — the bot starts from a zero book and only
    ever manages what it buys from now on. THE mode for the feature's target
    user (has manual positions, wants the bot to leave them alone). Caveat
    the caller must surface: if the BOT also holds live positions at seed
    time, they become the user's — the bot will re-buy its full target on
    top (doubled exposure). Flatten the bot's own positions first.

    absorb=True: the account's current position is 100% BOT-owned as of now.
    Only correct on an account with NO manual positions mixed in (e.g.
    migrating a bot-only machine without closing anything) — on a mixed
    account this adopts the user's manual positions into the bot's book, and
    the bot will later trade them away. get_positions_fn is only called in
    this mode.

    Either mode REPLACES THE WHOLE FILE and forgets every fill recorded
    before now — a deliberate, explicit action, never called automatically by
    reconcile(). To reset only SOME symbols (e.g. after a flatten), use
    zero_ledger_symbols() instead.

    Returns the seeded {symbol: signed_size} ({} for fresh start) for the
    caller to show the user.
    """
    now = datetime.utcnow().isoformat()
    symbols = {}
    if absorb:
        for symbol, pos in (get_positions_fn() or {}).items():
            size = float(pos.get('size', 0) or 0)
            side = pos.get('side')
            signed = size if side == 'long' else (-size if side == 'short' else 0.0)
            symbols[symbol] = {'size': signed, 'ts': now}
    # 'seeded_at' cuts off history for EVERY symbol, incl. ones with no row —
    # pre-seed orders.jsonl history must never leak into the ledger
    _save_ledger_seed({'seeded_at': now, 'symbols': symbols})
    return {k: v['size'] for k, v in symbols.items()}


def zero_ledger_symbols(symbols):
    """Reset the self-ledger baseline to flat, timestamped now, for exactly
    these symbols — every OTHER symbol's seed and accumulated history is left
    untouched. Use after closing a real position OUTSIDE the normal target-vs-
    ledger diff (flatten/close-all: manager/flatten.py) — those closes get
    logged to manager/orders.jsonl too, and without this they would be summed
    into ledger_positions() as if they were an ordinary bot trade, driving the
    ledger to a phantom position it never actually held (see
    references/manager.md § self_ledger — the flatten interaction). No-op
    (creates a zeroed entry) for a symbol with no prior seed. Safe to call even
    when self_ledger is currently off — the seed file just sits unused until
    it's turned on, and by then this IS the correct baseline.
    """
    if not symbols:
        return
    seed = _load_ledger_seed()
    now = datetime.utcnow().isoformat()
    for symbol in symbols:
        seed['symbols'][symbol] = {'size': 0.0, 'ts': now}
    _save_ledger_seed(seed)


def ledger_positions():
    """The bot's OWN tracked position per symbol: each symbol's seed_ledger()/
    zero_ledger_symbols() baseline plus every fill logged for it since (manager/
    orders.jsonl `legs[].signed_diff`, summed) — never the exchange's raw
    account position.

    A symbol with no seed at all (self_ledger turned on but seed_ledger() never
    run) sums EVERY orders.jsonl entry for it from the start of the file —
    on a flat account that is harmlessly correct, but on an account that
    already holds a live bot position it is WRONG unless every fill really is
    in that file (seed_ledger() MUST run first on an account with an existing
    position, or reconcile() re-buys the whole target on top of what already
    exists — see references/manager.md § self_ledger).

    Returns the same shape get_positions_fn() returns:
    {symbol: {'side': 'long'|'short'|None, 'size': float}}.
    """
    seed = _load_ledger_seed()
    totals = {symbol: row['size'] for symbol, row in seed['symbols'].items()}

    try:
        with open(_ORDERS_LOG_PATH) as f:
            lines = f.readlines()
    except OSError:
        lines = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        symbol = entry.get('symbol')
        if not symbol:
            continue
        # entries at/before this symbol's cutoff are already inside its
        # baseline. Per-symbol ts (zero_ledger_symbols) wins over the global
        # seeded_at, so zeroing one symbol never discards another symbol's
        # history — and seeded_at covers symbols flat at seed time, whose
        # pre-seed history must be excluded too (measured live 2026-08-20).
        cutoff = seed['symbols'].get(symbol, {}).get('ts') or seed['seeded_at']
        if cutoff and str(entry.get('ts') or '') <= cutoff:
            continue
        for leg in entry.get('legs') or []:
            try:
                totals[symbol] = totals.get(symbol, 0.0) + float(leg.get('signed_diff') or 0)
            except (TypeError, ValueError):
                continue

    return {
        symbol: {'side': 'long' if size > 0 else ('short' if size < 0 else None),
                 'size': abs(size)}
        for symbol, size in totals.items()
        if abs(size) > 1e-9
    }


# ── signal gate (啟動,等新訊號才進場 — resume_wait) ─────────────────────────
# state/signal_gate.json = {strategy: position_value_recorded_at_resume}.
# Written by the runtime's resume_wait command right before it clears HALT.
# While a funded strategy's CURRENT state.json position equals its recorded
# value, that strategy has "no new signal yet": its symbol is excluded from
# reconciling entirely — no catch-up entry, and no close-on-removal against
# whatever the ledger/book already holds. The moment the position value
# differs, the gate lifts PERMANENTLY (removed from the file) and the strategy
# trades normally from then on. MultiCharts-style: the start button never
# places orders; only a signal change does.
_SIGNAL_GATE_PATH = 'state/signal_gate.json'


def _load_signal_gate():
    try:
        with open(_SIGNAL_GATE_PATH) as f:
            raw = json.load(f)
        return {str(k): float(v) for k, v in (raw or {}).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _save_signal_gate(gate):
    try:
        os.makedirs(os.path.dirname(_SIGNAL_GATE_PATH), exist_ok=True)
        tmp = _SIGNAL_GATE_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(gate, f, indent=2)
        os.replace(tmp, _SIGNAL_GATE_PATH)
    except OSError as e:
        logging.warning(f'signal_gate persist failed: {e}')


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
    gate          = _load_signal_gate()
    lifted        = set()

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

        # signal gate (resume_wait): the strategy's signal hasn't changed
        # since the user started with 「等新訊號才進場」 — mark it; the moment
        # the value differs the gate lifts PERMANENTLY. Checked only for
        # funded strategies (the ones that could trade at all).
        gated = False
        if name in gate:
            # "new signal" = the DIRECTION changed (enter, exit, or flip —
            # sign including zero), not any value change: a vol-scaled
            # strategy re-computing 0.5→0.52 same-direction is a sizing
            # adjustment, not a new trading decision, and must not lift the
            # wait (audit #6, Wei 2026-08-20). For ±1/0 strategies this is
            # identical to exact-value comparison.
            def _sign(x):
                return 1 if x > 0 else (-1 if x < 0 else 0)
            if _sign(position) != _sign(gate[name]):
                logging.info(f"[portfolio] signal gate lifted for {name} "
                             f"({gate[name]:g}→{position:g}) — trading normally")
                lifted.add(name)
            else:
                gated = True

        contribution = amount * position

        # spot and swap are different inventories — same symbol, different key,
        # so a spot strategy and a perp strategy on BTCUSDT never net against
        # each other (they'd converge the WRONG account's position)
        market = strategy_market(name)
        key = market_key(symbol, market)

        if key not in totals:
            totals[key] = {'signed': 0.0, 'exchange': exchange,
                           'asset_spec': asset_spec, 'market': market,
                           'contributors': [], 'gated': False}
        totals[key]['signed'] += contribution
        # ANY gated funded contributor gates the whole symbol — with mixed
        # gating, trading the un-gated part would immediately "correct" the
        # gated strategy's absent contribution, defeating the wait
        totals[key]['gated'] = totals[key]['gated'] or gated
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
            'gated':        data['gated'],
        }
    if lifted:
        # merge-on-save (audit #7): re-load the file and remove ONLY the lifted
        # names — dumping our stale in-memory copy would clobber a gate that
        # resume_wait rewrote mid-round (lost update: the fresh gate entry
        # vanishes and that strategy catches up next round against the user's
        # explicit choice).
        fresh = _load_signal_gate()
        changed = False
        for name in lifted:
            if name in fresh:
                del fresh[name]
                changed = True
        if changed:
            _save_signal_gate(fresh)
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

    `threshold` is account-currency scale (crypto notional) and meaningless
    for a symbol whose 'size' is a LOT COUNT (asset_specs[strategy]["type"] ==
    "futures_contracts", e.g. capital/TW futures — see strategy_amounts):
    diffs there are single/low-double-digit lots, so a currency threshold of
    10 would silently swallow every order, including a close-on-removal
    (target absent, asset_spec None — checked via `exchange` too). Those rows
    skip `threshold` entirely; their own place_order_fn applies the real
    minimum (e.g. reconciler.py's _capital_place_order round-half-up gate).
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
        if diff == 0:
            continue
        asset_spec = t.get('asset_spec')
        is_lot_based = ((asset_spec or {}).get('type') == 'futures_contracts'
                         or t.get('exchange') == 'capital' or a.get('exchange') == 'capital')
        if not is_lot_based and abs(diff) < threshold:
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

    # TOCTOU guard (audit #4): a round that STARTED under HALT must never open
    # exposure, even if the user clears the halt mid-round — this round's gate/
    # target were computed from pre-resume state (e.g. resume_wait writes the
    # signal gate and THEN clears HALT; a round already past its gate read
    # would otherwise place the exact catch-up orders the user opted out of).
    # Clearing HALT changes the reconciler's watched __halt__ mtime, so a
    # clean round follows within one poll tick — the cost is one round's delay.
    halted_at_start = guard.halted()

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

    # self_ledger (portfolio_config.json): diff — and every flip/reduce-only
    # decision in the loop below — against the bot's OWN tracked position
    # (lib.portfolio.ledger_positions), not the raw exchange read, so a
    # position the user opened by hand is never read as "already have it" and
    # never split into a reduce-only close leg against it. `actual` (the real
    # exchange read) still feeds the snapshot below for drift comparison / a
    # future workspace view — it is only skipped as the DIFFING input here.
    # See references/manager.md § self_ledger.
    ledger = None
    if config.get('self_ledger'):
        # Fail loud without a seed (audit P1-6, mandatory since users enable
        # this themselves): an unseeded ledger silently sums the ENTIRE
        # orders.jsonl history — on any machine with prior trading that is a
        # plausible-looking but wrong book, and the bot would trade to
        # "correct" it. Raising here surfaces as the reconciler's normal
        # error path (Telegram + retreat to heartbeat), no orders placed.
        if not _load_ledger_seed()['seeded_at']:
            raise RuntimeError(
                "self_ledger is on but manager/ledger_seed.json has no baseline — "
                "run `python3 manager/seed_ledger.py` first (see "
                "references/manager.md § self_ledger); refusing to trade on an "
                "unseeded ledger")
        ledger = {_canon_key(k): v for k, v in (ledger_positions() or {}).items()}
    diff_actual = ledger if ledger is not None else actual

    # signal gate (resume_wait): a gated symbol is excluded from BOTH sides of
    # the diff — no catch-up entry (absent target would be wrong: it exists,
    # it's gated), and no close-on-removal against whatever the book already
    # holds. The gate lifts inside aggregate_portfolio() the moment the
    # strategy's signal changes; from that round on the symbol diffs normally.
    gated_symbols = {k for k, v in target.items() if v.get('gated')}
    full_target = target  # snapshot keeps gated rows (flagged 'gated': True)
    if gated_symbols:
        logging.info(f"[reconcile] signal-gated (waiting for a new signal): "
                     f"{sorted(gated_symbols)}")
        target = {k: v for k, v in target.items() if k not in gated_symbols}
        diff_actual = {k: v for k, v in diff_actual.items() if k not in gated_symbols}

    orders = compute_diff(target, diff_actual, threshold)

    # Written before placing, so it records the state that WAS acted on. A
    # reconcile that crashes mid-loop still leaves the observation behind.
    # full_target, not the filtered dict (audit #8): a gated strategy's target
    # must stay visible to the workspace view — carrying 'gated': True — not
    # vanish while it waits for its signal.
    _write_reconcile_snapshot(full_target, actual, orders, ledger=ledger)

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
        # Same lot-based exemption as compute_diff() (account-currency
        # threshold=10 is meaningless for a symbol sized in LOTS, e.g. capital/
        # TW futures) — this is a SECOND, independent threshold gate a few
        # lines below (per sub-order leg), missed when compute_diff() was
        # patched: a 1-lot diff passed compute_diff() only to be silently
        # `continue`d here, so place_order_fn was never even called (measured
        # live 2026-08-14 — "Converged, nothing filled" with zero attempts).
        is_lot_based = ((asset_spec or {}).get('type') == 'futures_contracts'
                         or order.get('exchange') == 'capital')
        leg_threshold = 0 if is_lot_based else threshold

        # Detect position flip: split into reduce-only close + directional open
        # to avoid simultaneous long+short on hedge-mode exchanges. Must use
        # diff_actual (ledger, when self_ledger is on) — not actual — or a
        # manual position visible only in the real exchange read would get
        # classified as a flip and generate a reduce-only close leg against it,
        # exactly the touch self_ledger exists to prevent.
        a        = diff_actual.get(symbol, {})
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
            if abs(sub_diff) < leg_threshold:
                continue

            # Kill switch, enforced here rather than only in lib/order_*.py: every
            # reconciler goes through this function, including the hand-written
            # place_order_fn of an exchange that has no official lib/order_* module.
            # Deliberately silent on Telegram — the user tripped the halt, and a
            # denial every poll is the noise they were trying to stop.
            if is_entry and (halted_at_start or guard.halted()):
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
                # self_ledger accounting (2026-08-20 audit P0-2): sub_diff is
                # the PRE-rounding request — capital rounds to a whole lot,
                # crypto floors to a qty step — so it can differ from what
                # actually filled. ledger_positions() sums this field directly,
                # so a signed_diff that doesn't match the real fill drifts the
                # ledger by the rounding gap every round, forever (the next
                # round diffs against the already-wrong ledger, so it never
                # self-corrects). Prefer the exchange-confirmed fill — same
                # convention lib.execute._finish() already uses for the async
                # TWAP/chase/custom path; sub_diff stays the fallback only
                # when executed_qty is unavailable (older/hand-wired
                # place_order_fn implementations).
                # Defensive: the order is ALREADY filled at this point — a bad
                # type from a hand-written place_order_fn must degrade to the
                # sub_diff fallback, never raise and drop the whole leg from
                # orders.jsonl (a filled trade the ledger never hears about).
                try:
                    executed_qty = leg.get('executed_qty')
                    if executed_qty is not None:
                        sign = 1.0 if sub_diff >= 0 else -1.0
                        if is_lot_based:
                            # capital/futures_contracts: executed_qty IS lots,
                            # already the unit compute_diff() worked in
                            filled_signed = sign * abs(float(executed_qty))
                        elif leg.get('fill_price'):
                            # crypto: executed_qty is BASE-currency qty — convert to
                            # account-currency notional to match sub_diff's unit
                            filled_signed = sign * abs(float(executed_qty)) * float(leg['fill_price'])
                        else:
                            filled_signed = None
                        if filled_signed is not None:
                            leg['signed_diff'] = round(filled_signed, 2)
                except (TypeError, ValueError) as e:
                    logging.warning(f"[reconcile] {symbol}: fill-based signed_diff "
                                    f"unavailable ({e}) — recording the requested "
                                    f"amount instead")
            legs.append(leg)

            # is_lot_based rows (capital/TW futures) are sized in LOTS, not
            # account-currency notional — the $-formatted default badly
            # understates a real index-futures trade (would send "Bought TXF
            # $1.00" for a 1-lot TXF entry). Same flag as compute_diff()'s
            # threshold exemption above.
            unit = '{amount:g} lots' if is_lot_based else '${amount:.2f}'
            if reduce_only:
                key     = 'order_close_long'  if sub_diff < 0 else 'order_close_short'
                default = (f'📉 Closed long {{symbol}} {unit}' if sub_diff < 0 else
                           f'📈 Closed short {{symbol}} {unit}')
            else:
                key     = 'order_buy' if sub_diff > 0 else 'order_sell'
                default = (f'📈 Bought {{symbol}} {unit}' if sub_diff > 0 else
                           f'📉 Sold {{symbol}} {unit}')

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
