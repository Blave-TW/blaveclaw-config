import logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from lib.portfolio import reconcile

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


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
    orders = reconcile(
        get_positions_fn=get_positions,
        place_order_fn=place_order,
        threshold_usdt=10,
        send_telegram_fn=send_telegram,
    )
    print(f"Reconciled {len(orders)} order(s)")
    for o in orders:
        direction = 'BUY' if o['signed_diff_usdt'] > 0 else 'SELL'
        print(f"  {direction} {o['symbol']} ${abs(o['signed_diff_usdt']):.0f}")
