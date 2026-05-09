# Strategy: [strategy name]
# Symbol:   BTCUSDT
# Interval: 1h
# Logic:    [entry/exit rules]

import logging, os, sys
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "skills" / "blave-quant"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from backtesting import Backtest, Strategy
from dotenv import dotenv_values
from lib.data import fetch_kline
from lib.execute import execute, load_state, save_state, bootstrap
from lib.analysis import reconstruct_arrays, plot_pnl

# --- Config ---
MODE          = "backtest"   # "backtest" | "paper" | "live"
STRATEGY_NAME = "[strategy_name]"
SYMBOL        = "BTCUSDT"
INTERVAL      = "1h"
START         = "2024-01-01"
END           = None
FEE           = 0.0005
BUDGET_USDT   = 1_000

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

def place_order(side):
    pass  # implement using exchange API — read skills/blave-quant/references/<exchange>-skill.md


# ─────────────────────────────────────────────────────────────
# FILL IN: Add indicator columns to df
# Used in live/paper mode and bootstrap.
# ─────────────────────────────────────────────────────────────
def add_indicators(df):
    # df['SMA20'] = df['Close'].rolling(20).mean()
    return df


# ─────────────────────────────────────────────────────────────
# FILL IN: Signal logic
# row: pd.Series with Close + your indicator columns
# Returns: "LONG" | "SHORT" | "FLAT"
# ─────────────────────────────────────────────────────────────
def compute_signal(row) -> str:
    close = row['Close']
    # sma = row['SMA20']
    ...


# ─────────────────────────────────────────────────────────────
# FILL IN: Backtest wrapper
# init()  — precompute indicators with self.I()
# next()  — build a row and call compute_signal
# ─────────────────────────────────────────────────────────────
class BlaveStrategy(Strategy):
    def init(self):
        # self.sma20 = self.I(lambda x: pd.Series(x).rolling(20).mean().values, self.data.Close)
        pass

    def next(self):
        row = pd.Series({
            'Close': self.data.Close[-1],
            # 'SMA20': self.sma20[-1],
        })
        signal = compute_signal(row)
        if signal == 'LONG':
            if self.position.is_short: self.position.close()
            if not self.position.is_long: self.buy()
        elif signal == 'SHORT':
            if self.position.is_long: self.position.close()
            if not self.position.is_short: self.sell()
        elif signal == 'FLAT' and self.position:
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
    logging.info(f"signal={signal} close={candle['close']}")
    execute(candle, signal, state, MODE, place_order_fn=place_order, send_telegram_fn=send_telegram)
    save_state(STRATEGY_NAME, state)


if __name__ == '__main__':
    main()
