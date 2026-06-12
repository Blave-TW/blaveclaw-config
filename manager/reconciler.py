import logging, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.portfolio import reconcile, load_portfolio_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

POLL_INTERVAL  = 5    # seconds between polls
THRESHOLD = 10   # minimum diff (in account currency) to place an order


def _active_state_mtimes():
    """Return {strategy_name: mtime} for strategies with non-zero weight."""
    weights = load_portfolio_config().get('weights', {})
    mtimes  = {}
    for name, w in weights.items():
        if w <= 0:
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
send_telegram = _make_sender()


if __name__ == '__main__':
    logging.info(f"Reconciler started (poll={POLL_INTERVAL}s, threshold={THRESHOLD})")
    last_mtimes = {}

    while True:
        current_mtimes = _active_state_mtimes()
        changed = [k for k in current_mtimes if current_mtimes[k] != last_mtimes.get(k)]

        if changed:
            logging.info(f"State changed: {changed} — running reconciliation")
            try:
                orders = reconcile(
                    get_positions_fn=get_positions,
                    place_order_fn=place_order,
                    threshold=THRESHOLD,
                    send_telegram_fn=send_telegram,
                )
                if not orders:
                    logging.info("No orders needed (within threshold)")
                last_mtimes = current_mtimes
            except Exception as e:
                err_msg = f"[reconciler] ERROR: {e}"
                logging.error(err_msg)
                send_telegram(err_msg)

        time.sleep(POLL_INTERVAL)
