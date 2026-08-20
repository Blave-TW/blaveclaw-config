import logging, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib import guard
from lib.portfolio import reconcile, load_portfolio_config, strategy_amounts

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

POLL_INTERVAL  = 5    # seconds between polls
THRESHOLD = 10   # minimum diff (in account currency) to place an order
                 # (lib/portfolio.spot_scope's threshold default tracks this)
DISCONNECT_HALT_AFTER = 3  # 連續 N 次讀不到持倉=交易所鏈路斷了,自動 HALT
RECONCILE_EVERY_S = 300  # 定期心跳對帳:就算沒有任何 mtime 變動也要每 5 分鐘
                         # 對一次帳——斷鏈、金鑰失效、倉位漂移不能等下一次訊號
                         # 變動(可能一小時後)才被發現


def _active_state_mtimes():
    """{strategy: state.json mtime} for funded strategies, plus the config
    itself — a reconcile is due when a SIGNAL moved or when the user changed
    the AMOUNTS (下單設定儲存); watching only state.json would leave a new
    allocation sitting unapplied until the next strategy tick."""
    mtimes = {}
    cfg_path = 'manager/portfolio_config.json'
    if os.path.exists(cfg_path):
        mtimes['__config__'] = os.path.getmtime(cfg_path)
    # HALT tripping/clearing must also trigger a round: 啟動下單 clears the
    # flag and the user expects orders within seconds, not at the next tick.
    halt_path = 'state/HALT'
    mtimes['__halt__'] = os.path.getmtime(halt_path) if os.path.exists(halt_path) else 0
    # an async execution (TWAP/custom, lib.execute) touches this on completion
    # so the residual gap converges within one poll, not the 5-min heartbeat
    kick_path = 'state/execution/kick'
    mtimes['__execution__'] = os.path.getmtime(kick_path) if os.path.exists(kick_path) else 0
    for name, amt in strategy_amounts().items():
        if amt <= 0:
            continue
        path = f'strategies/{name}/state.json'
        if os.path.exists(path):
            mtimes[name] = os.path.getmtime(path)
    return mtimes


# ── Capital (群益) hand-wired path ───────────────────────────────────────────
# TW brokers are excluded from lib.venue_wiring's auto-wire (_NON_AUTO) because
# the data shape differs from every crypto venue: LOTS not account-currency
# notional, buy/sell not long/short, and the resolved contract code (TM2608)
# differs from the order alias (TM0000) sent on the wire. This block converts
# between the two and activates ONLY when a strategy in portfolio_config
# routes to "capital" (get_positions) / the order carries exchange="capital"
# (place_order) — every other machine (crypto exchanges, the overwhelming
# majority of the fleet) falls straight through to the auto-wire calls below,
# completely untouched.
#
# capital_symbol / resolved_prefix derive MECHANICALLY from the strategy's
# SYMBOL (references/capital-broker.md Step 8) — not user config, so this
# table is read-only fact, not a per-deployment setting. Only TM0000 ->
# TM2608 is LIVE-VERIFIED (2026-08-14); TX00/MTX00's resolved-code prefix is
# inferred from the same alias-stripping convention and unverified — confirm
# on the first live TXF/MXF capital order and update this comment.
# contract_value is kept for reference/parity with the asset_specs table
# (capital-broker.md Step 8) — no longer read by this module: 2026-08-14 lots
# are stored/compared directly (see _capital_get_positions), so no code path
# here converts lots <-> TWD notional anymore.
_CAPITAL_FUTURES_SPEC = {
    "TXF": {"capital_symbol": "TX00",   "resolved_prefix": "TX",  "contract_value": 200},
    "MXF": {"capital_symbol": "MTX00",  "resolved_prefix": "MTX", "contract_value": 50},
    "TMF": {"capital_symbol": "TM0000", "resolved_prefix": "TM",  "contract_value": 10},
}

# reconcile()'s account-currency THRESHOLD is meaningless for lot-scale
# diffs — capital rows skip it entirely (lib.portfolio.compute_diff) — so
# _capital_place_order's round-half-up is the only minimum-order gate for
# capital: a diff under half a lot rounds to 0 and places nothing.


# ── Capital Read-Your-Writes guard ──────────────────────────────────────────
# Race (found live 2026-08-14): lib/capital_worker.py refreshes
# state/capital_account.json every 60s; force_next re-reconciles within 5s of
# a fill. A round landing in that gap reads the PRE-order snapshot, sees the
# position unchanged, and re-sends the same order (margin happened to reject
# the duplicate that day — not a backstop to rely on). _capital_mark_order_sent
# records when THIS process last sent a capital order; _capital_get_positions
# refuses to trust a snapshot older than that mark.
_CAPITAL_LAST_ORDER_PATH = 'state/capital_last_order_at'
_capital_last_order_at = 0.0  # process-local fast path


def _capital_load_last_order_at():
    """Seed the process-local marker from disk at import time — covers a
    watchdog restart (references/manager.md: restarts on crash) landing
    inside the same <60s window as a just-sent order, which a bare in-memory
    variable would silently forget."""
    global _capital_last_order_at
    try:
        with open(_CAPITAL_LAST_ORDER_PATH) as f:
            _capital_last_order_at = float(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        pass


def _capital_mark_order_sent():
    """Call right before the order API call (after all validation gates) —
    covers the call regardless of how it resolves (fill, reject, exception).
    Written both in-process (fast path) and to disk (survives a restart),
    atomic tmp+replace matching lib/capital_worker.py's own snapshot write."""
    global _capital_last_order_at
    _capital_last_order_at = time.time()
    try:
        os.makedirs(os.path.dirname(_CAPITAL_LAST_ORDER_PATH), exist_ok=True)
        tmp = _CAPITAL_LAST_ORDER_PATH + '.tmp'
        with open(tmp, 'w') as f:
            f.write(repr(_capital_last_order_at))
        os.replace(tmp, _CAPITAL_LAST_ORDER_PATH)
    except OSError as e:
        logging.warning(f"[reconciler/capital] failed to persist last-order marker: {e}")


_capital_load_last_order_at()


class CapitalCacheLagError(Exception):
    """state/capital_account.json has not yet been refreshed since this
    process's own last capital order — a Read-Your-Writes guard, NOT a
    connectivity failure. Must not count toward DISCONNECT_HALT_AFTER (see
    _get_positions_guarded) or be treated as a "state consumed" round in the
    main loop (see __main__)."""


def _capital_check_snapshot_caught_up(snapshot_read_at):
    """Raise iff a capital order was sent by this process and the snapshot
    predates it. No-op when no order is pending (_capital_last_order_at==0)
    — the normal, overwhelming-majority-of-rounds path is untouched.

    Ordering contract (caller): call this AFTER the snapshot's own
    freshness/ok check (lib.account_capital._read_snapshot, raised inside
    get_positions()) has already passed — otherwise a genuinely dead worker
    would trip this guard forever instead of surfacing as the real stale-
    snapshot error that counts toward auto-halt.

    Known residual gap (flagged, not silently patched): capital_worker.py
    stamps read_at at WRITE time, not at the start of its COM query cycle
    (query_rights → query_open_interest → write_snapshot takes low seconds).
    An order landing in that narrow sub-window could see a read_at newer
    than the order mark while positions were still queried from the venue
    before the order — this guard would then pass incorrectly. Distinct from
    (and much narrower than) the reported 60s-cadence race; needs a
    cycle-start timestamp in capital_worker.py to close fully — flagged to
    Wei rather than papered over with a guessed grace margin."""
    if snapshot_read_at < _capital_last_order_at:
        raise CapitalCacheLagError(
            f"群益部位快取尚未跟上最近一次下單(快取 read_at={snapshot_read_at:.0f}"
            f",下單於 {_capital_last_order_at:.0f})—— 本輪跳過,等下一輪快取更新")


def _is_capital_routed():
    """One-machine-one-venue (AGENTS.md § Broker Onboarding): true when any
    strategy in portfolio_config["exchanges"] is bound to capital — the signal
    get_positions() uses to pick the TW-futures snapshot over the crypto
    auto-wire (get_positions takes no per-call venue argument)."""
    return any(v == 'capital' for v in load_portfolio_config().get('exchanges', {}).values())


def _capital_get_positions():
    """Actual capital futures positions as {symbol: {'side':'long'|'short',
    'size': lots}} — 'size' is a LOT COUNT (2026-08-14: lots stored/compared
    directly end-to-end, no price round-trip — see references/manager.md §
    amounts semantics; the old lots->TWD->lots conversion introduced rounding
    error whenever the index moved between the state.json snapshot and this
    read, e.g. a constant 1-lot signal like tmf_always_hold could drift off 1
    lot for no reason other than the index ticking). Reads EVERY open futures
    position on the account (not just currently-configured strategies) so
    removing a strategy from portfolio_config still generates a close order —
    futures parity with lib.venue_wiring.spot_scope's exit-on-removal
    behaviour. Error contract unchanged: any failure (worker down/stale
    snapshot) PROPAGATES — never catch-and-return {} (see get_positions
    docstring).

    Read-Your-Writes guard (2026-08-14): after the snapshot's own freshness
    check passes, also refuses a snapshot older than this process's last
    capital order (_capital_check_snapshot_caught_up) — raises
    CapitalCacheLagError in that narrow post-order window instead of
    returning stale positions."""
    from lib.account_capital import get_positions as _acct_positions, get_snapshot_read_at
    raw = _acct_positions({})  # env unused — reads state/capital_account.json
    _capital_check_snapshot_caught_up(get_snapshot_read_at())
    out = {}
    for resolved_sym, pos in raw.items():
        resolved = str(resolved_sym).upper()
        for canon, spec in _CAPITAL_FUTURES_SPEC.items():
            if resolved.startswith(spec['resolved_prefix']):
                # account_capital.get_positions() already normalizes to
                # 'long'/'short' (account_TEMPLATE.py's contract, fixed
                # 2026-08-14 alongside this call site — pos['side'] is NOT
                # 'buy'/'sell' here; do not re-translate or long positions
                # silently read as short).
                out[canon] = {
                    'side': pos['side'],
                    'size': float(pos['size']),
                    'exchange': 'capital',
                }
                break
        else:
            logging.warning(f"[reconciler/capital] position {resolved_sym!r} matched no "
                            f"known futures alias (TXF/MXF/TMF) — ignored")
    return out


def _capital_place_order(symbol, signed_diff, asset_spec=None, reduce_only=False):
    """place_order body for a capital-routed order (exchange == 'capital').
    signed_diff is a LOT COUNT (2026-08-14: amounts[strategy] for
    futures_contracts strategies IS lots — see lib/portfolio.strategy_amounts
    — so aggregate_portfolio's target/actual are already lots and no
    lots<->TWD conversion is needed here): > 0 buy, < 0 sell. Calls
    lib.order_capital.place_futures_market_order — never hand-write SKCOM
    calls (references/capital-broker.md).

    Rounding: round-half-up the lot magnitude (Wei 2026-08-14 — fixed a bug
    where floor-then-reject-under-1-lot made the [0.5, 1.0) diff band never
    trade: it passed a >=0.5 gate but then floored to 0 and was rejected
    anyway, so a vol-scaled/fractional capital target could sit forever
    without crossing a whole lot). math.floor(raw_lots + 0.5) is used
    instead of Python's round() because round() does banker's rounding
    (0.5 ties go to even, i.e. down to 0 here) — the opposite of what's
    wanted. A diff below 0.5 lots still rounds to 0 and correctly places
    nothing.

    NOTE: this is the diff-execution gate only. It is unrelated to the
    separate, deliberately floor/round-down convention used for crypto
    futures contract-quantity sizing (e.g. lib/order_binance.py,
    lib/order_okx.py, lib/order_bingx.py's _floor_to_step) — that
    "never risk more than intended" convention is untouched by this fix."""
    import math
    from dotenv import dotenv_values
    from lib.order_capital import place_futures_market_order

    spec = _CAPITAL_FUTURES_SPEC.get(str(symbol).upper())
    if not spec:
        raise RuntimeError(f"capital: {symbol!r} is not a supported TW futures symbol "
                           f"(TXF/MXF/TMF only — securities reconciliation is not wired)")
    configured_alias = (asset_spec or {}).get('capital_symbol')
    if configured_alias not in (None, spec['capital_symbol']):
        raise RuntimeError(
            f"capital: {symbol} portfolio_config asset_spec.capital_symbol="
            f"{configured_alias!r} disagrees with the documented alias "
            f"{spec['capital_symbol']!r} (capital-broker.md Step 8) — fix portfolio_config")

    raw_lots = abs(signed_diff)
    lots = math.floor(raw_lots + 0.5)  # round-half-up; NOT round() (banker's rounding)
    if lots < 1:
        return False  # below half a lot — nothing to trade

    action = 'buy' if signed_diff > 0 else 'sell'
    intent = 'reduce' if reduce_only else 'entry'
    env = dotenv_values()
    # Read-Your-Writes marker — set before the call so it covers this attempt
    # regardless of outcome (fill, reject, or exception below).
    _capital_mark_order_sent()
    result = place_futures_market_order(env, spec['capital_symbol'], action, lots, intent)
    return {
        'avg_price':       result.get('avg_fill_price') or 0.0,
        'executed_qty':    result.get('fill_qty') or 0.0,
        'exchange':        'capital',
        'resolved_symbol': result.get('symbol'),
        'status':          result.get('status'),
    }


def get_positions():
    """
    Actual open positions: {symbol: {'side': 'long'|'short'|None, 'size': float}}
    with 'size' in account currency (USD), and spot inventory under
    "SYMBOL@spot" keys (lib/portfolio.market_key).

    OFFICIAL VENUES AUTO-WIRE — DO NOT HAND-WIRE THEM: when the bound venue
    ships both lib/account_{id}.py and lib/order_{id}.py (Binance / BingX /
    OKX), lib/venue_wiring routes everything — swap positions, spot inventory
    (MARKET="spot" strategies via spot_scope, incl. exit-on-removal), and the
    reduce-leg lot handling. Rebinding to another official venue needs no
    reconciler change at all.

    CAPITAL (群益) IS HAND-WIRED (see _capital_get_positions above): when any
    strategy routes to "capital", 'size' is a LOT COUNT, not USD/TWD notional
    (2026-08-14) — matches portfolio_config["amounts"] for
    asset_specs[strategy]["type"] == "futures_contracts" strategies, which is
    itself lots, not account currency (see lib/portfolio.strategy_amounts).

    Replace this body ONLY for a venue WITHOUT official libs (follow
    references/exchange-connect.md). Keep the ERROR CONTRACT: on any failure
    let the exception PROPAGATE — never catch-and-return {} ("all flat"), or
    reconcile() re-buys the entire target the moment the link recovers, and
    the auto-halt wrapper can only count failures it actually sees.
    """
    if _is_capital_routed():
        return _capital_get_positions()
    from lib.venue_wiring import auto_get_positions
    return auto_get_positions()


def place_order(symbol, signed_diff, asset_spec=None, reduce_only=False,
                exchange=None, contributors=None):
    """
    Order closing the target/actual gap. signed_diff is account
    currency (USD): > 0 buy, < 0 sell. `symbol` may carry a market suffix
    ("BTCUSDT@spot") — split with lib.portfolio.split_key.

    OFFICIAL VENUES AUTO-WIRE (see get_positions) — lib.execute.dispatch_order
    resolves the user's per-strategy execution style (下單方式: market / TWAP /
    custom, portfolio_config["execution"]) and routes market legs straight
    through lib/venue_wiring (spot buys sized in quote currency, spot sells
    capped at inventory, swap converted at the live mark with reduce legs
    ceiled to a whole lot and capped at the position). TWAP / custom legs run
    in a background thread — this returns False while one is in flight and the
    reconcile loop keeps serving every other symbol.

    CAPITAL (群益) IS HAND-WIRED (see _capital_place_order above): routed by
    exchange == 'capital' (reconcile() passes portfolio_config's exchange
    label through per-order — see references/manager.md § portfolio_config.json
    Exchange Routing). signed_diff is a LOT COUNT here (2026-08-14, no
    lots<->TWD conversion), round-half-up to whole lots, no TWAP/custom support.

    Replace this body ONLY for a venue WITHOUT official libs. Contract:
      return False  = intentionally skipped (below exchange minimum, or the
                      leg is executing asynchronously) — no Telegram here,
                      no phantom trade
      return dict   = exchange-confirmed fills ({'avg_price','executed_qty',
                      'exchange', ...}) — report fills, never intent
      raise         = real failure (network, rejection) — surfaces to the user
    Qty precision: floor to the instrument's step via the order lib's
    format_qty (Decimal, never float division — measured: 0.01/0.1 floors a
    whole lot short); asset_spec passes through from portfolio_config for
    non-fractional instruments (futures contracts etc.).
    """
    if exchange == 'capital':
        return _capital_place_order(symbol, signed_diff, asset_spec=asset_spec,
                                    reduce_only=reduce_only)
    from lib.execute import dispatch_order
    return dispatch_order(symbol, signed_diff, asset_spec=asset_spec,
                          reduce_only=reduce_only, exchange=exchange,
                          contributors=contributors)


# 這層包裝是基礎設施,交易所無關——實作 get_positions() 時不用碰它。
_consecutive_failures = 0


def _get_positions_guarded():
    """Auto-halt on exchange disconnect (references/manager.md § Auto-halt).

    get_positions() 失敗=交易所鏈路本身不通(壞/撤銷金鑰、IP 白名單、斷線),
    不是單筆下單被拒(那由 reconcile() 逐單處理、不掛 halt)。連續
    DISCONNECT_HALT_AFTER 次就 trip_halt——看不到真實持倉還繼續開新倉,
    等於矇著眼下單;金鑰消失期間尤其危險(連線恢復那刻會把整份 target
    重新買一次)。只掛不自動解:跟所有 halt 一樣,只有用戶能 resume。

    EXEMPTION (2026-08-14): CapitalCacheLagError is the one exception type
    excluded from the counter — it means "our own snapshot hasn't caught up
    yet" (bounded ≤60s, self-resolving), not "the link is down". This is a
    deliberate, narrowly-scoped deviation from references/manager.md's stated
    "no shared exception type needed" design — update that doc if this
    exemption list ever grows."""
    global _consecutive_failures
    try:
        result = get_positions()
        _consecutive_failures = 0
        return result
    except CapitalCacheLagError as e:
        # Read-Your-Writes guard tripped, not a connectivity failure (see
        # CapitalCacheLagError docstring) — leave _consecutive_failures
        # untouched (neither incremented nor reset) so a genuinely concurrent
        # disconnect elsewhere still counts correctly; this self-resolves
        # within one more worker cycle (≤60s) without help from this counter.
        logging.info(f"[reconciler/capital] {e}")
        raise
    except Exception as e:
        _consecutive_failures += 1
        logging.error(
            f"get_positions failed ({_consecutive_failures}/{DISCONNECT_HALT_AFTER}): {e}")
        if _consecutive_failures >= DISCONNECT_HALT_AFTER and not guard.halted():
            guard.trip_halt(
                f"exchange unreachable ({_consecutive_failures} consecutive "
                f"get_positions failures)", "reconciler")
            send_telegram(
                f"🚨 HALT engaged: get_positions() failed {_consecutive_failures} "
                f"times — exchange may be unreachable")
        raise


from lib.notify import make_sender as _make_sender

# Telegram is optional: a web-workspace user may never pair a bot, and the
# reconciler must trade for them anyway. No config → log instead of notify;
# the workspace page is their surface for state.
try:
    send_telegram = _make_sender()
except Exception as _e:
    logging.warning(f"telegram notify unavailable ({_e}) — falling back to log-only")

    def send_telegram(msg):
        logging.warning(f"[notify-unavailable] {msg}")


HEARTBEAT_PATH = Path('state/heartbeat/reconciler')


if __name__ == '__main__':
    logging.info(f"Reconciler started (poll={POLL_INTERVAL}s, threshold={THRESHOLD})")

    # A crash mid-chase strands a resting limit order on the venue (market/TWAP
    # slices die clean — only chase posts resting orders). Sweep our own
    # fingerprinted orders once at startup; best-effort, never blocks the loop.
    try:
        from lib.venue_wiring import sweep_orphan_orders
        _swept = sweep_orphan_orders()
        if _swept:
            send_telegram(f"♻️ cancelled {_swept} resting order(s) left by a previous run")
    except Exception as _e:
        logging.warning(f"[reconciler] orphan sweep skipped: {_e}")

    # A previous process that died mid-TWAP/chase/custom may have filled at the
    # exchange without logging — under self_ledger that understates the book
    # and the next round would re-buy it. reap_dead_inflight() trips HALT and
    # notifies instead of trading on a book known to be missing fills
    # (audit P1 #1). Best-effort like the sweep above.
    try:
        from lib.execute import reap_dead_inflight
        reap_dead_inflight()
    except Exception as _e:
        logging.warning(f"[reconciler] dead-inflight reap skipped: {_e}")

    last_mtimes = {}
    last_reconcile_at = 0.0
    force_next = False  # 下單後強制再對帳一輪:把成交後的實際部位寫進快照,
                        # 不然「實際/差額」會停在下單前的狀態直到下次訊號變動

    while True:
        # heartbeat for manager/healthcheck.py — a stale file means this daemon died
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.touch()

        try:
            # Inside the try: this json-loads portfolio_config.json, which the
            # web command listener rewrites — a mid-write read must be a skipped
            # round, not a daemon crash-loop.
            current_mtimes = _active_state_mtimes()
        except Exception as e:
            logging.error(f"[reconciler] state scan failed: {e}")
            time.sleep(POLL_INTERVAL)
            continue
        changed = [k for k in current_mtimes if current_mtimes[k] != last_mtimes.get(k)]
        heartbeat_due = time.time() - last_reconcile_at > RECONCILE_EVERY_S

        if changed or force_next or heartbeat_due:
            logging.info(f"State changed: {changed} — running reconciliation")
            try:
                orders = reconcile(
                    get_positions_fn=_get_positions_guarded,
                    place_order_fn=place_order,
                    threshold=THRESHOLD,
                    send_telegram_fn=send_telegram,
                )
                if not orders:
                    logging.info("Converged — nothing filled this round")
                # reconcile() returns only orders that actually FILLED ≥1 leg —
                # so a persistent failure does not become a 5-second retry
                # storm; failures wait for the next state change / heartbeat.
                force_next = bool(orders)
                last_mtimes = current_mtimes
                last_reconcile_at = time.time()
            except CapitalCacheLagError as e:
                # Read-Your-Writes guard, not a real failure (see class
                # docstring) — deliberately the OPPOSITE handling of the
                # generic except below: do NOT advance last_mtimes /
                # last_reconcile_at, so whatever triggered this round
                # (changed state, or force_next from the order that caused
                # the lag) fires again next poll tick instead of being
                # silently deferred up to RECONCILE_EVERY_S. No Telegram —
                # this is an expected, self-resolving (≤60s) condition, not
                # something to page the user for.
                logging.info(f"[reconciler] round skipped: {e}")
            except Exception as e:
                err_msg = f"[reconciler] ERROR: {e}"
                logging.error(err_msg)
                send_telegram(err_msg)
                # A persistent failure (dead key, network) must retreat to the
                # heartbeat cadence, not retry+Telegram every poll tick. BOTH
                # lines are needed: last_reconcile_at throttles the heartbeat
                # branch, and last_mtimes must also advance or the `changed`
                # branch keeps firing — auto-halt's own trip writes state/HALT,
                # whose fresh mtime would otherwise re-trigger a failing round
                # (and a Telegram error) every 5 seconds, forever. A REAL new
                # state change still produces a newer mtime and fires at once.
                last_mtimes = current_mtimes
                last_reconcile_at = time.time()

        time.sleep(POLL_INTERVAL)
