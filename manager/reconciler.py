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

    Replace this body ONLY for a venue WITHOUT official libs (follow
    references/exchange-connect.md). Keep the ERROR CONTRACT: on any failure
    let the exception PROPAGATE — never catch-and-return {} ("all flat"), or
    reconcile() re-buys the entire target the moment the link recovers, and
    the auto-halt wrapper can only count failures it actually sees.
    """
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
    重新買一次)。只掛不自動解:跟所有 halt 一樣,只有用戶能 resume。"""
    global _consecutive_failures
    try:
        result = get_positions()
        _consecutive_failures = 0
        return result
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
