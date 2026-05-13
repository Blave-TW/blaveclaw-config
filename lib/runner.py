import json, logging, math, os
import numpy as np
import pandas as pd
import vectorbt as vbt
from dotenv import dotenv_values
from lib.execute import update_state, load_state, save_state, bootstrap
from lib.analysis import reconstruct_arrays_vbt, plot_pnl, plot_pnl_portfolio

_VBT_FREQ = {
    '1m': '1min', '5min': '5min', '15min': '15min',
    '1h': '1h', '4h': '4h', '8h': '8h',
    '1d': '1D', '1w': '1W',
}


def backtest_signals(close, signals, fee, freq, init_cash=100_000):
    """Run a single Type A backtest. Returns vbt Portfolio.

    Execution: signal fires at Close[t] → fills at Close[t] (MOC).
    settlement (-1.0) treated identically to normal exit (0.0).
    """
    pos_sig = signals.copy(); pos_sig[signals == -1.0] = 0.0
    pos     = pos_sig.ffill().fillna(0)
    entries = (pos > 0) & (pos.shift(1, fill_value=0) == 0)
    exits   = (pos == 0) & (pos.shift(1, fill_value=0) > 0)
    size    = signals.where(signals > 0).ffill().fillna(1.0)
    return vbt.Portfolio.from_signals(
        close, entries, exits, size=size,
        size_type='percent', fees=fee, freq=freq, init_cash=init_cash,
    )


def run(config, fetch_data_fn, compute_fn, send_telegram_fn=None):
    """
    Unified runner for Type A and Type C strategies.

    fetch_data_fn(hdrs) → any
        Type A: returns a single DataFrame (OHLCV + indicators)
        Type C: returns any data structure the strategy needs

    compute_fn(data) → pd.Series | tuple(np.ndarray, pd.DataFrame)
        Type A: returns pd.Series of signals
                  positive float → long (size fraction), execute at close[t]  (MOC)
                  0.0            → flat, execute at close[t]  (MOC)
                 -1.0            → settlement, same as 0.0
                  nan            → hold (no action)
        Type C: returns (weights_mat, close_df)
                weights_mat: np.ndarray (n_days, n_stocks), weights based on close[t]
                close_df:    pd.DataFrame of close prices
                Runner shift(1) weights → weights[t] earns return from close[t] to close[t+1]  (MOC)
                DO NOT pre-shift in compute_fn.

    Runner routes by return type:
        pd.Series  → vectorbt from_signals backtest (shift handled internally)
        tuple      → weight-matrix portfolio backtest (shift handled internally)
    """
    mode          = config['MODE']
    strategy_name = config['STRATEGY_NAME']
    fee           = config.get('FEE', 0.0005)
    interval      = config.get('INTERVAL', '1h')

    env  = dotenv_values()
    hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

    os.makedirs(f'strategies/{strategy_name}', exist_ok=True)
    logging.basicConfig(
        filename=f'strategies/{strategy_name}/strategy.log',
        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s'
    )

    data   = fetch_data_fn(hdrs)
    result = compute_fn(data)

    # ── Type A: signal strategy ───────────────────────────────────────────────
    if isinstance(result, pd.Series):
        df      = data
        signals = result

        if mode == 'backtest':
            warmup = config.get('WARMUP', 0)
            if warmup > 0:
                df      = df.iloc[warmup:]
                signals = signals.iloc[warmup:]

            freq = _VBT_FREQ.get(interval, '1h')
            pf   = backtest_signals(df['Close'], signals, fee, freq)

            stats = pf.stats()
            print(stats[['Total Return [%]', 'Sharpe Ratio', 'Max Drawdown [%]', 'Win Rate [%]', 'Total Trades']])
            print(f"Fee Rate: {fee*100:.4f}%  Total Fees: {float(stats['Total Fees Paid'])/100_000*100:.2f}%")

            _skeys = ['Total Return [%]', 'Benchmark Return [%]',
                      'Max Drawdown [%]', 'Total Trades', 'Win Rate [%]',
                      'Best Trade [%]', 'Worst Trade [%]', 'Expectancy',
                      'Sharpe Ratio', 'Sortino Ratio', 'Omega Ratio']
            from lib.analysis import slippage_analysis
            slip = slippage_analysis(df, pf)

            def _v(x):
                return None if (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(float(x), 4)
            slip_stats = {} if slip.empty else {
                'slippage_entry_gap_avg_%':  round(float(slip['entry_gap_%'].mean()), 4),
                'slippage_exit_gap_avg_%':   round(float(slip['exit_gap_%'].mean()),  4),
                'slippage_net_gap_avg_%':    round(float(slip['net_gap_%'].mean()),   4),
                'slippage_cumulative_%':     round(float(slip['net_gap_%'].sum()),    4),
            }
            json.dump(
                {'strategy': strategy_name, 'symbol': config.get('SYMBOL'), 'interval': interval,
                 'start': config.get('START'), 'end': df.index[-1].strftime('%Y-%m-%d'),
                 'fee [%]': round(fee * 100, 4),
                 **{k: _v(stats[k]) for k in _skeys},
                 'Total Fees Paid [%]': round(float(stats['Total Fees Paid']) / 100_000 * 100, 4),
                 **slip_stats},
                open(f'strategies/{strategy_name}/stats.json', 'w'), indent=2
            )

            out_path = f'strategies/{strategy_name}/pnl.png'
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
        weights_mat, close_df, *_opt = result
        open_df = _opt[0] if _opt else None

        warmup = config.get('WARMUP', 0)
        if warmup > 0:
            weights_mat = weights_mat[warmup:]
            close_df    = close_df.iloc[warmup:]

        weights_mat = np.vstack([
            np.zeros((1, weights_mat.shape[1])),
            weights_mat[:-1],
        ])

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
        print(f"  Fee Rate:      {fee*100:.4f}%  Total Fees: {tc_daily.sum()*100:.2f}%")

        slip_stats  = {}
        bench_stats = {}
        if open_df is not None:
            from lib.analysis import slippage_analysis_portfolio
            slip_stats = slippage_analysis_portfolio(close_df, open_df, delta_w)

        from lib.analysis import random_bh_benchmark
        bench_stats, bench_pct = random_bh_benchmark(close_df, total_ret * 100, sharpe)

        def _v(x):
            return None if (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(float(x), 4)
        json.dump(
            {'strategy': strategy_name, 'interval': '1d',
             'start': close_df.index[0].strftime('%Y-%m-%d'),
             'end':   close_df.index[-1].strftime('%Y-%m-%d'),
             'fee': fee,
             'Total Return [%]':      _v(total_ret * 100),
             'Ann. Return [%]':       _v(ann_ret   * 100),
             'Sharpe Ratio':          _v(sharpe),
             'Max Drawdown [%]':      _v(mdd       * 100),
             'Total Fees Paid [%]':   round(float(tc_daily.sum()) * 100, 4),
             **slip_stats, **bench_stats},
            open(f'strategies/{strategy_name}/stats.json', 'w'), indent=2
        )

        out_path = f'strategies/{strategy_name}/pnl.png'
        plot_pnl_portfolio(pf_series, close_df, title=strategy_name, output_path=out_path,
                           bench_pct=bench_pct)

        if send_telegram_fn:
            send_telegram_fn(
                f"回測完成：{strategy_name}\n"
                f"總報酬 {total_ret:.1%}  年化 {ann_ret:.1%}\n"
                f"Sharpe {sharpe:.2f}  MDD {mdd:.1%}"
            )

    else:
        raise TypeError(f"compute_fn must return pd.Series (Type A) or tuple (Type C), got {type(result)}")
