import logging, os
import numpy as np
import pandas as pd
import vectorbt as vbt
from datetime import datetime
from dotenv import dotenv_values
from lib.execute import update_state, load_state, save_state, bootstrap
from lib.analysis import reconstruct_arrays_vbt, plot_pnl

_VBT_FREQ = {
    '1m': '1min', '5min': '5min', '15min': '15min',
    '1h': '1h', '4h': '4h', '8h': '8h',
    '1d': '1D', '1w': '1W',
}


def run(config, fetch_data_fn, compute_fn, send_telegram_fn=None):
    """
    Unified runner for Type A and Type C strategies.

    fetch_data_fn(hdrs) → any
        Type A: returns a single DataFrame (OHLCV + indicators)
        Type C: returns any data structure the strategy needs

    compute_fn(data) → pd.Series | tuple(np.ndarray, pd.DataFrame)
        Type A: returns pd.Series of signals (1.0/0.0/nan)
        Type C: returns (weights_mat, close_df)
                weights_mat: np.ndarray (n_days, n_stocks)
                close_df:    pd.DataFrame of close prices

    Runner routes by return type:
        pd.Series  → vectorbt from_signals backtest
        tuple      → weight-matrix portfolio backtest
    """
    mode          = config['MODE']
    strategy_name = config['STRATEGY_NAME']
    fee           = config.get('FEE', 0.0005)
    interval      = config.get('INTERVAL', '1h')

    env  = dotenv_values()
    hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

    os.makedirs(f'strategies/{strategy_name}', exist_ok=True)
    logging.basicConfig(
        filename=f'strategies/{strategy_name}/{strategy_name}.log',
        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s'
    )

    data   = fetch_data_fn(hdrs)
    result = compute_fn(data)

    # ── Type A: signal strategy ───────────────────────────────────────────────
    if isinstance(result, pd.Series):
        df      = data
        signals = result

        if mode == 'backtest':
            pos     = signals.ffill().fillna(0)
            entries = (pos > 0) & (pos.shift(1, fill_value=0) == 0)
            exits   = (pos == 0) & (pos.shift(1, fill_value=0) > 0)
            size    = signals.where(signals > 0).ffill().fillna(1.0)

            freq = _VBT_FREQ.get(interval, '1h')
            pf   = vbt.Portfolio.from_signals(
                df['Open'],
                entries.shift(1).fillna(False),
                exits.shift(1).fillna(False),
                size=size.shift(1).fillna(1.0),
                size_type='percent', fees=fee, freq=freq, init_cash=100_000,
            )

            stats = pf.stats()
            print(stats[['Total Return [%]', 'Sharpe Ratio', 'Max Drawdown [%]', 'Win Rate [%]', 'Total Trades']])

            out_path = f'strategies/{strategy_name}/{strategy_name}_pnl.png'
            result_d = reconstruct_arrays_vbt(df, pf, signals)
            plot_pnl(df, result_d, title=f'{strategy_name}', output_path=out_path)

            if send_telegram_fn:
                send_telegram_fn(
                    f"回測完成：{strategy_name}\n"
                    f"Return {stats['Total Return [%]']:.1f}%  "
                    f"Sharpe {stats['Sharpe Ratio']:.2f}  "
                    f"MDD {stats['Max Drawdown [%]']:.1f}%"
                )
            return

        # Live / paper mode
        all_signals = signals

        def _row_signal(row):
            return float(all_signals.loc[row.name]) if row.name in all_signals.index else float('nan')

        candles = [{'time': int(t.timestamp()), 'close': float(r['Close']),
                    'open': float(r['Open']), 'high': float(r['High']), 'low': float(r['Low'])}
                   for t, r in df.iterrows()]

        state  = load_state(strategy_name) or bootstrap(df, _row_signal)
        candle = candles[-1]
        signal = float(all_signals.iloc[-1])
        logging.info(f"signal={signal:.4f} close={candle['close']}")
        update_state(candle, signal, state, mode,
                     symbol=config.get('SYMBOL', ''),
                     exchange=config.get('EXCHANGE', ''),
                     send_telegram_fn=send_telegram_fn)
        save_state(strategy_name, state)

    # ── Type C: portfolio strategy ────────────────────────────────────────────
    elif isinstance(result, tuple):
        weights_mat, close_df = result

        n         = len(close_df)
        daily_ret = close_df.pct_change().fillna(0).values
        delta_w   = np.diff(weights_mat, axis=0, prepend=weights_mat[:1] * 0)
        tc_daily  = (np.abs(delta_w) * fee).sum(axis=1)
        pf_ret    = (weights_mat * daily_ret).sum(axis=1) - tc_daily
        pf_equity = np.cumprod(1 + pf_ret)

        pf_series = pd.Series(pf_ret, index=close_df.index)
        rets_acc  = pf_series.vbt.returns(freq='1D')
        total_ret = pf_equity[-1] - 1
        ann_ret   = rets_acc.annualized()
        sharpe    = rets_acc.sharpe_ratio()
        mdd       = rets_acc.max_drawdown()

        print(f"  Total Return:  {total_ret:.1%}")
        print(f"  Ann. Return:   {ann_ret:.1%}")
        print(f"  Sharpe Ratio:  {sharpe:.2f}")
        print(f"  Max Drawdown:  {mdd:.1%}")

        # PnL chart
        import matplotlib; matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        idx = close_df.index
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9),
                                        gridspec_kw={'height_ratios': [3, 1]}, sharex=True)
        ax1.plot(idx, pf_equity, color='#2196F3', linewidth=2, label=strategy_name)
        ax1.set_ylabel('Portfolio Value'); ax1.legend(); ax1.grid(alpha=0.3)
        ax1.set_title(f'{strategy_name}')
        dd = pf_equity / np.maximum.accumulate(pf_equity) - 1
        ax2.fill_between(idx, dd, 0, color='#2196F3', alpha=0.4)
        ax2.set_ylabel('Drawdown'); ax2.grid(alpha=0.3)
        plt.tight_layout()
        out_path = f'strategies/{strategy_name}/{strategy_name}_pnl.png'
        plt.savefig(out_path, dpi=150); plt.close()
        print(f'Chart saved: {out_path}')

        if send_telegram_fn:
            send_telegram_fn(
                f"回測完成：{strategy_name}\n"
                f"總報酬 {total_ret:.1%}  年化 {ann_ret:.1%}\n"
                f"Sharpe {sharpe:.2f}  MDD {mdd:.1%}"
            )

    else:
        raise TypeError(f"compute_fn must return pd.Series (Type A) or tuple (Type C), got {type(result)}")
