import logging, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.portfolio import reconcile, load_portfolio_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

POLL_INTERVAL  = 5    # seconds between polls
THRESHOLD_USDT = 10   # minimum diff to place an order


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
    Return {symbol: {'side': 'long'|'short'|None, 'size_usdt': float}}

    Example — Binance USDT-M futures:
      GET /fapi/v2/positionRisk  (signed)
      Filter rows where positionAmt != 0
      side = 'long' if positionAmt > 0 else 'short'
      size_usdt = abs(positionAmt) * markPrice

    Example — Bybit linear:
      GET /v5/position/list?category=linear
      side = row['side'].lower()
      size_usdt = float(row['positionValue'])
    """
    raise NotImplementedError("implement exchange-specific position query")


def place_order(symbol, signed_diff_usdt):
    """
    Place a market order to close the gap between target and actual.

    signed_diff_usdt > 0 → buy  (increase long / reduce short)
    signed_diff_usdt < 0 → sell (increase short / reduce long)

    Steps:
      1. Get current price for symbol
      2. qty = abs(signed_diff_usdt) / price
      3. side = BUY if signed_diff_usdt > 0 else SELL
      4. Place market order on exchange
    """
    raise NotImplementedError("implement exchange-specific order placement")


from lib.notify import make_sender as _make_sender
send_telegram = _make_sender()


if __name__ == '__main__':
    logging.info(f"Reconciler started (poll={POLL_INTERVAL}s, threshold=${THRESHOLD_USDT})")
    last_mtimes = {}

    while True:
        current_mtimes = _active_state_mtimes()
        changed = [k for k in current_mtimes if current_mtimes[k] != last_mtimes.get(k)]

        if changed:
            logging.info(f"State changed: {changed} — running reconciliation")
            orders = reconcile(
                get_positions_fn=get_positions,
                place_order_fn=place_order,
                threshold_usdt=THRESHOLD_USDT,
                send_telegram_fn=send_telegram,
            )
            if not orders:
                logging.info("No orders needed (within threshold)")
            last_mtimes = current_mtimes

        time.sleep(POLL_INTERVAL)
