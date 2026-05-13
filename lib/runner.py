import logging, os
import numpy as np
import pandas as pd
import vectorbt as vbt
from datetime import datetime
from dotenv import dotenv_values
from lib.data import fetch_kline
from lib.execute import update_state, load_state, save_state, bootstrap
from lib.analysis import reconstruct_arrays_vbt, plot_pnl

_VBT_FREQ = {
    '1m': '1min', '5min': '5min', '15min': '15min',
    '1h': '1h', '4h': '4h', '8h': '8h',
    '1d': '1D', '1w': '1W',
}


def run(config, add_indicators_fn, compute_signals_fn, send_telegram_fn=None):
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
        signals = compute_signals_fn(df)  # pd.Series: positive=long, 0.0=flat, nan=hold

        pos     = signals.ffill().fillna(0)
        entries = (pos > 0) & (pos.shift(1, fill_value=0) == 0)
        exits   = (pos == 0) & (pos.shift(1, fill_value=0) > 0)
        size    = signals.where(signals > 0).ffill().fillna(1.0)

        # Signal fires at bar i close → execute at bar i+1 open (realistic)
        freq = _VBT_FREQ.get(interval, '1h')
        pf = vbt.Portfolio.from_signals(
            df['Open'],
            entries.shift(1).fillna(False),
            exits.shift(1).fillna(False),
            size=size.shift(1).fillna(1.0),
            size_type='percent',
            fees=fee, freq=freq,
            init_cash=100_000,
        )

        stats = pf.stats()
        print(stats[['Total Return [%]', 'Sharpe Ratio', 'Max Drawdown [%]', 'Win Rate [%]', 'Total Trades']])

        result = reconstruct_arrays_vbt(df, pf, signals, vol_lookback, periods_per_year)
        plot_pnl(df, result,
                 title=f'{symbol} {strategy_name}',
                 output_path=f'strategies/{strategy_name}/{strategy_name}_pnl.png')
        return

    # Live / paper mode
    candles = [{'time': int(t.timestamp()), 'close': float(r['Close']),
                 'open': float(r['Open']), 'high': float(r['High']), 'low': float(r['Low'])}
                for t, r in df.iterrows()]

    # Compute full signal series; wrap as row-based fn for bootstrap compatibility
    all_signals = compute_signals_fn(df)
    def _row_signal(row):
        return float(all_signals.loc[row.name]) if row.name in all_signals.index else float('nan')

    state  = load_state(strategy_name) or bootstrap(df, _row_signal)
    candle = candles[-1]
    signal = float(all_signals.iloc[-1])
    logging.info(f"signal={signal:.4f} close={candle['close']}")
    update_state(candle, signal, state, mode,
                 symbol=symbol, exchange=exchange,
                 send_telegram_fn=send_telegram_fn)
    save_state(strategy_name, state)
