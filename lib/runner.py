import logging, os
import numpy as np
import pandas as pd
from datetime import datetime
from dotenv import dotenv_values
from backtesting import Backtest, Strategy
from lib.data import fetch_kline
from lib.execute import update_state, load_state, save_state, bootstrap
from lib.analysis import reconstruct_arrays, plot_pnl


class _BlaveBase(Strategy):
    _compute_signal = None

    def init(self):
        pass

    def next(self):
        row    = self.data.df.iloc[-1]
        signal = self._compute_signal(row)
        if np.isnan(float(signal)):
            return  # nan = hold: keep current position unchanged
        if signal > 0:
            if self.position.is_short: self.position.close()
            if not self.position.is_long: self.buy()
        elif signal < 0:
            if self.position.is_long: self.position.close()
            if not self.position.is_short: self.sell()
        elif self.position:
            self.position.close()


def run(config, add_indicators_fn, compute_signal_fn, send_telegram_fn=None):
    mode             = config['MODE']
    strategy_name    = config['STRATEGY_NAME']
    symbol           = config['SYMBOL']
    exchange         = config.get('EXCHANGE', '')
    interval         = config['INTERVAL']
    start            = config['START']
    end              = config.get('END')
    fee              = config.get('FEE', 0.0005)
    vol_targeting    = config.get('VOL_TARGETING', False)
    vol_lookback     = config.get('VOL_LOOKBACK', 720)
    periods_per_year = config.get('PERIODS_PER_YEAR', 8760)

    env  = dotenv_values()
    hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

    os.makedirs(f'strategies/{strategy_name}', exist_ok=True)
    logging.basicConfig(
        filename=f'strategies/{strategy_name}/{strategy_name}.log',
        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s'
    )

    today = datetime.utcnow().strftime('%Y-%m-%d')
    df    = fetch_kline(symbol, interval, start, end if mode == 'backtest' else today, hdrs)
    df    = add_indicators_fn(df)

    if vol_targeting:
        log_ret = np.log(df['Close'] / df['Close'].shift(1))
        df['realized_vol'] = log_ret.rolling(vol_lookback).std() * np.sqrt(periods_per_year)

    if mode == 'backtest':
        StratClass = type('BlaveStrategy', (_BlaveBase,), {
            '_compute_signal': staticmethod(compute_signal_fn),
        })
        bt    = Backtest(df, StratClass, cash=100_000, commission=fee, trade_on_close=False)
        stats = bt.run()
        print(stats[['Return [%]', 'Sharpe Ratio', 'Max. Drawdown [%]', 'Win Rate [%]', '# Trades']])
        result = reconstruct_arrays(df, stats)
        plot_pnl(df, result, title=f'{symbol} {strategy_name}',
                 output_path=f'strategies/{strategy_name}/{strategy_name}_pnl.png')
        return

    candles = [{'time': int(t.timestamp()), 'close': float(r['Close']),
                 'open': float(r['Open']), 'high': float(r['High']), 'low': float(r['Low'])}
                for t, r in df.iterrows()]

    state  = load_state(strategy_name) or bootstrap(df, compute_signal_fn)
    candle = candles[-1]
    signal = compute_signal_fn(df.iloc[-1])
    logging.info(f"signal={signal:.4f} close={candle['close']}")
    update_state(candle, signal, state, mode,
                 symbol=symbol, exchange=exchange,
                 send_telegram_fn=send_telegram_fn)
    save_state(strategy_name, state)
