# Strategy: [strategy name]
# Symbol:   BTCUSDT
# Interval: 1h
# Logic:    [entry/exit rules]

import logging, os, sys
import numpy as np
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "skills" / "blave-quant"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from backtesting import Backtest, Strategy
from dotenv import dotenv_values
from lib.data import fetch_kline
from lib.execute import update_state, load_state, save_state, bootstrap
from lib.analysis import reconstruct_arrays, plot_pnl

# --- Config ---
MODE             = "backtest"   # "backtest" | "paper" | "live"
STRATEGY_NAME    = "[strategy_name]"
SYMBOL           = "BTCUSDT"
EXCHANGE         = "binance"    # exchange identifier for reconciler
INTERVAL         = "1h"
START            = "2024-01-01"
END              = None
FEE              = 0.0005
BUDGET_USDT      = 1_000        # backtest cash
VOL_TARGETING    = False        # set True to size position by realized volatility
TARGET_VOL       = 0.10         # (VOL_TARGETING only) annualized target vol, e.g. 0.10 = 10%
VOL_LOOKBACK     = 720          # (VOL_TARGETING only) lookback candles, e.g. 720 = 30d on 1h
PERIODS_PER_YEAR = 8760         # (VOL_TARGETING only) 1h=8760  4h=2190  1d=365
VOL_CAP          = 2.0          # (VOL_TARGETING only) max scale factor

_env  = dotenv_values()
_HDRS = {'api-key': _env.get('blave_api_key', ''), 'secret-key': _env.get('blave_secret_key', '')}

# --- Logging ---
os.makedirs(f'strategies/{STRATEGY_NAME}', exist_ok=True)
logging.basicConfig(
    filename=f'strategies/{STRATEGY_NAME}/{STRATEGY_NAME}.log',
    level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s'
)

def send_telegram(msg):
    pass


# ─────────────────────────────────────────────────────────────
# FILL IN: Add indicator columns to df
# realized_vol is computed here and used in compute_signal.
# ─────────────────────────────────────────────────────────────
def add_indicators(df):
    if VOL_TARGETING:
        log_ret = np.log(df['Close'] / df['Close'].shift(1))
        df['realized_vol'] = log_ret.rolling(VOL_LOOKBACK).std() * np.sqrt(PERIODS_PER_YEAR)
    # df['SMA20'] = df['Close'].rolling(20).mean()
    return df


# ─────────────────────────────────────────────────────────────
# FILL IN: Signal logic
# Returns float: 1.0=full long  -1.0=full short  0.0=flat
# With VOL_TARGETING=True, output is scaled by realized vol so the
# position contributes a consistent annualized volatility to the portfolio.
# ─────────────────────────────────────────────────────────────
def compute_signal(row) -> float:
    # 1. direction: 1.0 long / -1.0 short / 0.0 flat
    direction = 0.0
    # if row['Close'] > row['SMA20']:
    #     direction = 1.0
    # elif row['Close'] < row['SMA20']:
    #     direction = -1.0

    if direction == 0.0 or not VOL_TARGETING:
        return direction

    # 2. vol scaling (only when VOL_TARGETING=True)
    realized_vol = row.get('realized_vol')
    if not realized_vol or np.isnan(realized_vol) or realized_vol <= 0:
        return 0.0

    return direction * min(TARGET_VOL / realized_vol, VOL_CAP)


# ─────────────────────────────────────────────────────────────
# FILL IN: Backtest wrapper
# Backtest uses direction only (full position); vol scaling applies in live mode.
# ─────────────────────────────────────────────────────────────
class BlaveStrategy(Strategy):
    def init(self):
        if VOL_TARGETING:
            log_ret = pd.Series(self.data.Close).pct_change().apply(np.log1p)
            self.rvol = self.I(
                lambda: log_ret.rolling(VOL_LOOKBACK).std() * np.sqrt(PERIODS_PER_YEAR),
                name='realized_vol'
            )
        # self.sma20 = self.I(lambda x: pd.Series(x).rolling(20).mean().values, self.data.Close)

    def next(self):
        row = pd.Series({
            'Close':        self.data.Close[-1],
            'realized_vol': self.rvol[-1] if VOL_TARGETING else None,
            # 'SMA20': self.sma20[-1],
        })
        signal = compute_signal(row)
        if signal > 0:
            if self.position.is_short: self.position.close()
            if not self.position.is_long: self.buy()
        elif signal < 0:
            if self.position.is_long: self.position.close()
            if not self.position.is_short: self.sell()
        elif self.position:
            self.position.close()


def main():
    from datetime import datetime
    today = datetime.utcnow().strftime('%Y-%m-%d')
    end   = END if MODE == 'backtest' else today
    df    = fetch_kline(SYMBOL, INTERVAL, START, end, _HDRS)
    df    = add_indicators(df)

    if MODE == 'backtest':
        bt    = Backtest(df, BlaveStrategy, cash=BUDGET_USDT, commission=FEE, trade_on_close=True)
        stats = bt.run()
        print(stats[['Return [%]', 'Sharpe Ratio', 'Max. Drawdown [%]', 'Win Rate [%]', '# Trades']])
        result = reconstruct_arrays(df, stats)
        chart_path = plot_pnl(df, result, title=f'{SYMBOL} {STRATEGY_NAME}', output_path=f'strategies/{STRATEGY_NAME}/{STRATEGY_NAME}_pnl.png')
        # send_telegram_image(chart_path)  # uncomment and implement
        return

    candles = [{'time': int(t.timestamp()), 'close': float(r['Close']),
                 'open': float(r['Open']), 'high': float(r['High']), 'low': float(r['Low'])}
                for t, r in df.iterrows()]

    state  = load_state(STRATEGY_NAME) or bootstrap(df, compute_signal)
    candle = candles[-1]
    signal = compute_signal(df.iloc[-1])
    logging.info(f"signal={signal:.4f} close={candle['close']}")
    update_state(candle, signal, state, MODE,
            symbol=SYMBOL, exchange=EXCHANGE,
            send_telegram_fn=send_telegram)
    save_state(STRATEGY_NAME, state)


if __name__ == '__main__':
    main()
