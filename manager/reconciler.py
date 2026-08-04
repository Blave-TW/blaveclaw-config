import logging, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.portfolio import reconcile, load_portfolio_config, strategy_amounts

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

POLL_INTERVAL  = 5    # seconds between polls
THRESHOLD = 10   # minimum diff (in account currency) to place an order
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
    for name, amt in strategy_amounts().items():
        if amt <= 0:
            continue
        path = f'strategies/{name}/state.json'
        if os.path.exists(path):
            mtimes[name] = os.path.getmtime(path)
    return mtimes


def get_positions():
    """
    Query exchange for actual open positions.
    Return {symbol: {'side': 'long'|'short'|None, 'size': float}}

    'size' is the position value expressed in account currency (e.g. USD).
    Convert native qty to account currency before returning:
      contracts × price_in_account_ccy   (futures)
      qty × mark_price                   (crypto perps)
      lots × price × fx_rate             (forex)

    Example — Binance USDT-M futures:
      GET /fapi/v2/positionRisk  (signed)
      Filter rows where positionAmt != 0
      side = 'long' if positionAmt > 0 else 'short'
      size = abs(positionAmt) * markPrice

    Example — Bybit linear:
      GET /v5/position/list?category=linear
      side = row['side'].lower()
      size = float(row['positionValue'])
    """
    raise NotImplementedError("implement exchange-specific position query")


def place_order(symbol, signed_diff, asset_spec=None):
    """
    Place a market order to close the gap between target and actual.

    signed_diff > 0 → buy  (increase long / reduce short)
    signed_diff < 0 → sell (increase short / reduce long)
    signed_diff is in account currency (e.g. USD).

    CRITICAL — return value convention:
      return False   if the order was intentionally skipped (qty below exchange minimum,
                     below one-lot threshold, etc.). This suppresses Telegram notification
                     and orders.jsonl logging — the user will NOT see a phantom trade.
      return None    (or any truthy value) on successful order placement.
      raise          on unexpected errors (network failure, API rejection, etc.).

    QTY PRECISION — REQUIRED before placing any order:
      Exchanges reject orders whose qty violates the symbol's step size, so fetch
      the instrument's trading rules FIRST (cache them at startup):
        Binance futures: GET /fapi/v1/exchangeInfo → LOT_SIZE.stepSize, minQty, MIN_NOTIONAL
        OKX:             GET /api/v5/public/instruments → lotSz, minSz
        Bybit:           GET /v5/market/instruments-info → lotSizeFilter.qtyStep, minOrderQty
      Then floor qty to the step using Decimal — float arithmetic produces
      0.10000000000000003-style artifacts that exchanges reject:
        from decimal import Decimal, ROUND_DOWN
        qty = float(Decimal(str(raw_qty)).quantize(Decimal(step_str), rounding=ROUND_DOWN))
      Send qty as a plain decimal string — never scientific notation like 1e-05.
      If the floored qty < minQty or qty*price < minNotional → return False.

    asset_spec: passed through from portfolio_config["asset_specs"]. Use it to convert
    signed_diff (account currency) into the instrument's native qty.

      asset_spec is None  →  default (fractional qty, no lot constraint):
        price = get_mark_price(symbol)
        qty   = abs(signed_diff) / price
        if qty < EXCHANGE_MINIMUM: return False  # ← must return False, not bare return
        side  = BUY if signed_diff > 0 else SELL

      asset_spec example for futures contracts (e.g. 台指期):
        {"type": "futures_contracts", "contract_value": 200,
         "currency": "TWD", "lot_size": 1}
        price_twd = get_futures_price(symbol)
        fx        = get_fx_rate("TWD")           # TWD per 1 account-currency unit
        notional  = price_twd * asset_spec["contract_value"] / fx
        contracts = round(abs(signed_diff) / notional)
        if contracts == 0: return False          # below one-lot threshold
        side = BUY if signed_diff > 0 else SELL
    """
    raise NotImplementedError("implement exchange-specific order placement")


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
                    get_positions_fn=get_positions,
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
                # heartbeat cadence, not retry+Telegram every poll tick — the
                # mtime trigger stays armed, so a real state change still
                # reconciles immediately.
                last_reconcile_at = time.time()

        time.sleep(POLL_INTERVAL)
