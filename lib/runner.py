import json, logging, math, os
import numpy as np
import pandas as pd
import vectorbt as vbt
from numba import njit
from vectorbt.portfolio.enums import SizeType, Direction
import vectorbt.portfolio.nb as pnb
from dotenv import dotenv_values
from lib.execute import update_state, load_state, save_state, bootstrap
from lib.analysis import plot_pnl, plot_pnl_portfolio, compute_slippage

_VBT_FREQ = {
    '1m': '1min', '5min': '5min', '15min': '15min',
    '1h': '1h', '4h': '4h', '8h': '8h',
    '1d': '1D', '1w': '1W',
}


@njit
def _order_func_nb(c, sig, fee_val):
    """Target-percent order: signal = signed position fraction."""
    return pnb.order_nb(
        size=sig[c.i],
        size_type=SizeType.TargetPercent,
        direction=Direction.Both,
        fees=fee_val,
    )


def backtest_signals(close, signals, fee, freq, init_cash=100_000):
    """Backtest using from_order_func + TargetPercent.

    Signal convention:
      positive float → long position fraction  (e.g.  1.0 = full long)
      negative float → short position fraction (e.g. -1.0 = full short)
      0.0            → flat
      nan            → hold (carry forward previous position)
    """
    sig_arr = signals.ffill().fillna(0).values.astype(float)
    return vbt.Portfolio.from_order_func(
        close, _order_func_nb, sig_arr, fee,
        freq=_VBT_FREQ.get(freq, '1h'), init_cash=init_cash,
    )



def run(config, fetch_data_fn, compute_fn, send_telegram_fn=None):
    """
    Unified runner for Type A and Type C strategies.

    compute_fn(data) → pd.Series | tuple
      Type A: pd.Series of signals
        positive float → long position fraction
        negative float → short position fraction
        0.0            → flat
        nan            → hold
      Type C: (weights_mat, close_df[, open_df])
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

            # Drop bars with invalid prices (e.g. futures overnight gaps)
            valid = df['Close'].notna() & (df['Close'] > 0)
            if not valid.all():
                df      = df[valid]
                signals = signals.reindex(df.index).ffill()

            pf  = backtest_signals(df['Close'], signals, fee, interval)
            pos = signals.ffill().fillna(0)

            delta_w_a            = np.diff(pos.values, prepend=0.0)
            slip_ret, slip_stats = compute_slippage(df['Close'], df['Open'], delta_w_a)

            adj_rets   = pd.Series(pf.returns().values + slip_ret.values, index=df.index)
            adj_acc    = adj_rets.vbt.returns(freq=_VBT_FREQ.get(interval, '1h'))
            adj_equity = adj_rets.add(1).cumprod() * pf.value().iloc[0]

            adj_total_ret = float(np.prod(1 + adj_rets) - 1) * 100
            adj_mdd       = float(abs(adj_acc.max_drawdown()) * 100)
            adj_sharpe    = float(adj_acc.sharpe_ratio())
            adj_sortino   = float(adj_acc.sortino_ratio())
            adj_omega     = float(adj_acc.omega_ratio())
            _orig         = pf.stats()

            def _v(x):
                if x is None: return None
                if isinstance(x, float) and (math.isnan(x) or math.isinf(x)): return None
                return round(float(x), 4)

            print(f"  Total Return (adj): {adj_total_ret:.2f}%  Sharpe: {adj_sharpe:.2f}  MDD: {adj_mdd:.2f}%")
            print(f"  Fee Rate: {fee*100:.4f}%  Total Fees: {float(_orig['Total Fees Paid'])/100_000*100:.2f}%")

            result_d = {
                'strat_ret':    adj_rets.values,
                'position':     pos.values,
                'realized_vol': df['realized_vol'].values if 'realized_vol' in df.columns
                                else np.full(len(df), np.nan),
                'cum':          (adj_equity / adj_equity.iloc[0]).values,
            }

            from lib.pnl import daily_returns_typeA
            d_dates, d_rets = daily_returns_typeA(adj_rets)

            _bench = _orig['Benchmark Return [%]'] if 'Benchmark Return [%]' in _orig.index else None
            json.dump(
                {'strategy': strategy_name, 'symbol': config.get('SYMBOL'), 'interval': interval,
                 'start': config.get('START'), 'end': df.index[-1].strftime('%Y-%m-%d'),
                 'fee [%]': round(fee * 100, 4),
                 'Total Return [%]':     _v(adj_total_ret),
                 'Benchmark Return [%]': _v(_bench),
                 'Max Drawdown [%]':     _v(adj_mdd),
                 'Sharpe Ratio':         _v(adj_sharpe),
                 'Sortino Ratio':        _v(adj_sortino),
                 'Omega Ratio':          _v(adj_omega),
                 'Total Fees Paid [%]':  round(float(_orig['Total Fees Paid']) / 100_000 * 100, 4),
                 **slip_stats,
                 'daily_dates': d_dates, 'daily_returns': d_rets,
                 'order_params': {'symbol': config.get('SYMBOL'), 'exchange': config.get('EXCHANGE'),
                                  'interval': interval, 'current_position': 0.0}},
                open(f'strategies/{strategy_name}/stats.json', 'w'), indent=2
            )

            plot_pnl(df, result_d, title=strategy_name,
                     output_path=f'strategies/{strategy_name}/pnl.png')

            if send_telegram_fn:
                from lib.notify import send_photo
                send_photo(f'strategies/{strategy_name}/pnl.png')
                send_telegram_fn(
                    f"回測完成：{strategy_name}\n"
                    f"Return {adj_total_ret:.1f}%  "
                    f"Sharpe {adj_sharpe:.2f}  "
                    f"MDD {adj_mdd:.1f}%"
                )
            return

        # ── Live / paper mode ──────────────────────────────────────────────────
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

        _ohlcv = {'Open', 'High', 'Low', 'Close', 'Volume', 'instrument_id'}
        ctx = {k: round(float(v), 6) for k, v in df.iloc[-1].items()
               if k not in _ohlcv and not (isinstance(v, float) and math.isnan(v))}

        update_state(candle, signal, state, mode,
                     symbol=config.get('SYMBOL', ''),
                     exchange=config.get('EXCHANGE', ''),
                     send_telegram_fn=send_telegram_fn,
                     strategy_name=strategy_name,
                     context=ctx)
        save_state(strategy_name, state)

        from lib.pnl import update_stats_live
        update_stats_live(strategy_name, state, config)

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
        slip_stats  = {}
        bench_stats = {}
        if open_df is not None:
            slip_ret_c, slip_stats = compute_slippage(close_df, open_df, delta_w)
            pf_ret_adj = pf_ret + slip_ret_c.values
        else:
            pf_ret_adj = pf_ret

        pf_equity = np.cumprod(1 + pf_ret_adj)
        pf_series = pd.Series(pf_ret_adj, index=close_df.index)
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

        from lib.analysis import random_bh_benchmark
        bench_stats, bench_pct = random_bh_benchmark(close_df, total_ret * 100, sharpe)

        def _v(x):
            if x is None: return None
            if isinstance(x, float) and (math.isnan(x) or math.isinf(x)): return None
            return round(float(x), 4)

        from lib.pnl import daily_returns_typeC
        d_dates, d_rets = daily_returns_typeC(pf_series)

        json.dump(
            {'strategy': strategy_name, 'interval': '1d',
             'start': close_df.index[0].strftime('%Y-%m-%d'),
             'end':   close_df.index[-1].strftime('%Y-%m-%d'),
             'fee': fee,
             'Total Return [%]':    _v(total_ret * 100),
             'Ann. Return [%]':     _v(ann_ret   * 100),
             'Sharpe Ratio':        _v(sharpe),
             'Max Drawdown [%]':    _v(mdd       * 100),
             'Total Fees Paid [%]': round(float(tc_daily.sum()) * 100, 4),
             **slip_stats, **bench_stats,
             'daily_dates': d_dates, 'daily_returns': d_rets,
             'order_params': None},
            open(f'strategies/{strategy_name}/stats.json', 'w'), indent=2
        )

        plot_pnl_portfolio(pf_series, close_df, title=strategy_name,
                           output_path=f'strategies/{strategy_name}/pnl.png',
                           bench_pct=bench_pct)

        if send_telegram_fn:
            from lib.notify import send_photo
            send_photo(f'strategies/{strategy_name}/pnl.png')
            send_telegram_fn(
                f"回測完成：{strategy_name}\n"
                f"總報酬 {total_ret:.1%}  年化 {ann_ret:.1%}\n"
                f"Sharpe {sharpe:.2f}  MDD {mdd:.1%}"
            )

    else:
        raise TypeError(f"compute_fn must return pd.Series (Type A) or tuple (Type C), got {type(result)}")
