import json, logging, math, os
from pathlib import Path
import numpy as np
import pandas as pd
from dotenv import dotenv_values
from lib.execute import update_state, load_state, save_state
from lib.analysis import plot_pnl, plot_pnl_portfolio, precise_pnl, compute_stats

_REPO_ROOT = Path(__file__).parent.parent


def run(config, fetch_data_fn, compute_fn, send_telegram_fn=None):
    """
    Unified runner for Type A and Type C strategies.

    compute_fn(data) → pd.Series | (pd.Series, exec_at_close) | (weights, price_df[, exec_at_close])

      Type A:
        pd.Series of signals  — positive=long, negative=short, 0=flat, nan=hold
        optional tuple (signals, exec_at_close) where exec_at_close is a bool Series/array

      Type C:
        (weights_mat, price_df[, exec_at_close])
        price_df: MultiIndex DataFrame with 'close' and optionally 'open' as top-level keys
        exec_at_close: optional bool array (n,) in original space
    """
    mode          = config['MODE']
    strategy_name = config['STRATEGY_NAME']
    fee           = config.get('FEE', 0.0005)
    interval      = config.get('INTERVAL', '1h')

    env  = dotenv_values()
    hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

    out_dir = _REPO_ROOT / 'strategies' / strategy_name
    os.makedirs(out_dir, exist_ok=True)
    logging.basicConfig(
        filename=str(out_dir / 'strategy.log'),
        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s'
    )

    data   = fetch_data_fn(hdrs)
    result = compute_fn(data)

    # Unpack optional exec_at_close for Type A: (signals, exec_at_close) → signals
    exec_at_close_orig = None
    if isinstance(result, tuple) and isinstance(result[0], pd.Series):
        signals_raw, *_rest = result
        exec_at_close_orig  = _rest[0] if _rest else None
        result              = signals_raw

    # ── Type A: signal strategy ───────────────────────────────────────────────
    if isinstance(result, pd.Series):
        df      = data
        signals = result

        # ── Full PnL computation (always) ──────────────────────────────────────
        warmup = config.get('WARMUP', 0)
        if warmup > 0:
            df      = df.iloc[warmup:]
            signals = signals.iloc[warmup:]

        # Drop bars with invalid prices (e.g. futures overnight gaps)
        valid = df['Close'].notna() & (df['Close'] > 0)
        if not valid.all():
            df      = df[valid]
            signals = signals.reindex(df.index).ffill()

        n   = len(df)
        pos = signals.ffill().fillna(0).values  # shape (n,)

        # 2-lag weight arrays
        w_curr      = np.empty(n)
        w_curr[0]   = 0.0
        w_curr[1:]  = pos[:-1]
        w_prev      = np.zeros(n)
        if n >= 2:
            w_prev[2:] = pos[:-2]

        # exec_at_close mask (original space → shift +1 to align with w_curr/w_prev)
        if exec_at_close_orig is not None:
            if hasattr(exec_at_close_orig, 'reindex'):
                ea = exec_at_close_orig.reindex(df.index).fillna(False).values.astype(bool)
            else:
                ea = np.asarray(exec_at_close_orig, dtype=bool)[-n:]
        elif 'instrument_id' in df.columns:
            ea = (df['instrument_id'] != df['instrument_id'].shift(-1)).fillna(False).values.astype(bool)
        else:
            ea = np.zeros(n, dtype=bool)

        exec_shifted      = np.zeros(n, dtype=bool)
        exec_shifted[1:]  = ea[:-1]

        close_v = df['Close'].values
        open_v  = df['Open'].values

        pf_ret, overnight, delta_w, tc_daily = precise_pnl(
            close_v, open_v, w_curr, w_prev, exec_shifted, fee
        )

        pf_series = pd.Series(pf_ret, index=df.index)
        sharpe, sortino, omega, mdd_raw, _ = compute_stats(pf_ret, df.index)

        total_ret  = float(np.prod(1 + np.nan_to_num(pf_ret)) - 1) * 100
        mdd        = abs(mdd_raw) * 100
        bench_ret  = (close_v[-1] / close_v[0] - 1) * 100
        total_fees = float(tc_daily.sum()) * 100

        def _v(x):
            if x is None: return None
            if isinstance(x, float) and (math.isnan(x) or math.isinf(x)): return None
            return round(float(x), 4)

        print(f"  Total Return: {total_ret:.2f}%  Sharpe: {sharpe:.2f}  MDD: {mdd:.2f}%")
        print(f"  Fee Rate: {fee*100:.4f}%  Total Fees: {total_fees:.2f}%")

        equity   = np.cumprod(1 + np.nan_to_num(pf_ret))
        result_d = {
            'strat_ret':    pf_ret,
            'position':     w_curr,
            'realized_vol': df['realized_vol'].values if 'realized_vol' in df.columns
                            else np.full(n, np.nan),
            'cum':          equity / equity[0],
        }

        from lib.pnl import daily_returns_typeA
        d_dates, d_rets = daily_returns_typeA(pf_series)

        json.dump(
            {'strategy': strategy_name, 'symbol': config.get('SYMBOL'), 'interval': interval,
             'start': config.get('START'), 'end': df.index[-1].strftime('%Y-%m-%d'),
             'fee [%]': round(fee * 100, 4),
             'Total Return [%]':     _v(total_ret),
             'Benchmark Return [%]': _v(bench_ret),
             'Max Drawdown [%]':     _v(mdd),
             'Sharpe Ratio':         _v(sharpe),
             'Sortino Ratio':        _v(sortino),
             'Omega Ratio':          _v(omega),
             'Total Fees Paid [%]':  round(total_fees, 4),
             'daily_dates': d_dates, 'daily_returns': d_rets,
             },
            open(out_dir / 'stats.json', 'w'), indent=2
        )

        plot_pnl(df, result_d, title=strategy_name,
                 output_path=str(out_dir / 'pnl.png'))

        if mode == 'backtest':
            if send_telegram_fn:
                from lib.notify import send_photo
                send_photo(str(out_dir / 'pnl.png'))
                send_telegram_fn(
                    f"回測完成：{strategy_name}\n"
                    f"Return {total_ret:.1f}%  "
                    f"Sharpe {sharpe:.2f}  "
                    f"MDD {mdd:.1f}%"
                )
            return

        # ── Live mode ──────────────────────────────────────────────────────────
        candles = [{'time': int(t.timestamp()), 'close': float(r['Close']),
                    'open': float(r['Open']), 'high': float(r['High']), 'low': float(r['Low'])}
                   for t, r in df.iterrows()]

        state  = load_state(strategy_name) or {
            'position': float(signals.ffill().fillna(0).iloc[-1]),
        }
        candle = candles[-1]
        signal = float(signals.iloc[-1])
        logging.info(f"signal={signal:.4f} close={candle['close']}")

        update_state(candle, signal, state, mode,
                     symbol=config.get('SYMBOL', ''),
                     send_telegram_fn=send_telegram_fn)
        save_state(strategy_name, state)


    # ── Type C: portfolio strategy ────────────────────────────────────────────
    elif isinstance(result, tuple) and isinstance(result[0], np.ndarray):
        weights_orig, price_df, *_opt = result
        exec_at_close_orig_c = np.asarray(_opt[0], dtype=bool) if _opt else None

        warmup = config.get('WARMUP', 0)
        if warmup > 0:
            weights_orig         = weights_orig[warmup:]
            price_df             = price_df.iloc[warmup:]
            if exec_at_close_orig_c is not None:
                exec_at_close_orig_c = exec_at_close_orig_c[warmup:]

        close_df = price_df['close']
        open_df  = price_df['open'] if 'open' in price_df.columns.get_level_values(0) else None

        n, k = weights_orig.shape

        # 2-lag weight arrays
        w_curr = np.vstack([np.zeros((1, k)), weights_orig[:-1]])   # shift 1: w_curr[t] = orig[t-1]
        w_prev = np.vstack([np.zeros((2, k)), weights_orig[:-2]])   # shift 2: w_prev[t] = orig[t-2]

        # exec_at_close mask (original space → shift +1)
        if exec_at_close_orig_c is not None:
            exec_shifted_c      = np.zeros(n, dtype=bool)
            exec_shifted_c[1:]  = exec_at_close_orig_c[:-1]
        else:
            exec_shifted_c = np.zeros(n, dtype=bool)

        close_v = close_df.values
        open_v  = open_df.values if open_df is not None else close_v

        pf_ret, overnight, delta_w, tc_daily = precise_pnl(
            close_v, open_v, w_curr, w_prev, exec_shifted_c, fee
        )

        bench_stats = {}

        pf_equity = np.cumprod(1 + pf_ret)
        pf_series = pd.Series(pf_ret, index=close_df.index)
        total_ret = pf_equity[-1] - 1
        sharpe, _, _, mdd, ann_ret = compute_stats(pf_ret, close_df.index)

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
             **bench_stats,
             'daily_dates': d_dates, 'daily_returns': d_rets,
             },
            open(out_dir / 'stats.json', 'w'), indent=2
        )

        plot_pnl_portfolio(pf_series, close_df, title=strategy_name,
                           output_path=str(out_dir / 'pnl.png'),
                           bench_pct=bench_pct)

        if send_telegram_fn:
            from lib.notify import send_photo
            send_photo(str(out_dir / 'pnl.png'))
            send_telegram_fn(
                f"回測完成：{strategy_name}\n"
                f"總報酬 {total_ret:.1%}  年化 {ann_ret:.1%}\n"
                f"Sharpe {sharpe:.2f}  MDD {mdd:.1%}"
            )

    else:
        raise TypeError(f"compute_fn must return pd.Series (Type A) or tuple (Type C), got {type(result)}")
