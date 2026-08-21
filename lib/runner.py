import json, logging, math, os
from pathlib import Path
import numpy as np
import pandas as pd
from dotenv import dotenv_values
from lib.execute import update_state, load_state, save_state
from lib.analysis import plot_pnl, plot_pnl_portfolio, precise_pnl, compute_stats

_REPO_ROOT = Path(__file__).parent.parent

# Per-event trade log cap for a single backtest — mirrors api/openclaw/agent_strategies.py's
# TRADES_MAX_COUNT. Bounding it here too (source) rather than only on the api side means a
# live-mode strategy that rewrites its full trades history every scheduler tick can't grow
# past what the api will accept anyway, and it stops one dense strategy's (pre-truncation)
# trades array from blowing the api's overall report body-size check for everyone else.
TRADES_MAX_COUNT = 10000

# Caps for the PLOT_SERIES → stats.json `panes` output — mirrored server-side in
# api/openclaw/agent_strategies.py. Bounded at the source for the same reason as
# TRADES_MAX_COUNT above: live mode rewrites stats.json every tick, so an unbounded
# declaration would inflate every report, not just one backtest.
PANES_MAX_SERIES = 4
PANES_MAX_POINTS = 20000
PANES_NAME_MAX   = 64
PANES_PANE_MAX   = 32  # pane group id length cap

# Cap for the stats.json `candles` output (Type A backtest OHLCV) — mirrored server-side
# in api/openclaw/agent_strategies.py. Same per-series budget as PANES_MAX_POINTS: daily
# bars = full history, 5min ≈ ten weeks — coordinated with the trades 10,000 tail window.
CANDLES_MAX_COUNT = 20000


def _build_panes(plot_series, df):
    """PLOT_SERIES declaration → stats.json `panes` (workspace indicator overlay).

    plot_series is a dict of display name → spec, where spec is one of:
      "col"                              — column of the backtest df (sub-pane)
      pd.Series                          — reindexed to the df (sub-pane)
      ("col" | pd.Series, {"overlay": True})  — drawn on the price chart instead
      ("col" | pd.Series, {"pane": "<group>"}) — series sharing a group id render in
                                          one sub-pane (e.g. MACD + its signal line)

    Timestamps use the same basis as trades[].ts (df.index[t].timestamp()) so the
    frontend aligns both against one axis. Non-finite points are skipped rather
    than zero-filled — a gap is honest, a fake 0 draws a misleading spike.
    """
    if not isinstance(plot_series, dict):
        return []
    panes = []
    for name, spec in plot_series.items():
        if len(panes) >= PANES_MAX_SERIES:
            break
        opts = {}
        if isinstance(spec, tuple) and spec:
            opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            spec = spec[0]
        if isinstance(spec, str):
            if spec not in df.columns:
                continue
            series = df[spec]
        elif isinstance(spec, pd.Series):
            series = spec.reindex(df.index)
        else:
            continue
        points = []
        for t, v in zip(df.index, series.to_numpy()):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(fv):
                continue
            points.append([int(t.timestamp()), round(fv, 6)])
        if not points:
            continue
        if len(points) > PANES_MAX_POINTS:
            points = points[-PANES_MAX_POINTS:]  # tail — same convention as trades
        entry = {
            'name':    str(name)[:PANES_NAME_MAX],
            'overlay': bool(opts.get('overlay', False)),
            'points':  points,
        }
        pane = opts.get('pane')
        if isinstance(pane, str) and pane.strip() and len(pane) <= PANES_PANE_MAX:
            entry['pane'] = pane
        panes.append(entry)
    return panes


def _build_candles(df):
    """Backtest df → stats.json `candles`: [[ts, open, high, low, close, volume], ...].

    The exact bars the backtest computed on (post-warmup, post-filter slice — the same
    df trades/panes are built from), so the frontend chart draws what the stats actually
    saw instead of re-fetching a kline endpoint that can diverge (adjusted stock prices,
    stitched futures contracts, warmup slicing). Same ts basis as trades[].ts and
    panes[].points. Bars with a non-finite/non-positive OHLC value are skipped — a gap
    is honest, a fake bar isn't. Volume is null when the df has no Volume column;
    a non-finite volume on an otherwise good bar also drops the bar.
    """
    cols = ('Open', 'High', 'Low', 'Close')
    if not all(c in df.columns for c in cols):
        return []
    try:
        o, h, l, c = (df[col].to_numpy(dtype=float) for col in cols)
        vol = df['Volume'].to_numpy(dtype=float) if 'Volume' in df.columns else None
    except (TypeError, ValueError):
        return []  # non-numeric column — no candles rather than a crashed run
    candles = []
    for i, t in enumerate(df.index):
        bar = (float(o[i]), float(h[i]), float(l[i]), float(c[i]))
        if not all(math.isfinite(x) and x > 0 for x in bar):
            continue
        if vol is None:
            v = None
        else:
            v = float(vol[i])
            if not math.isfinite(v):
                continue
        candles.append([int(t.timestamp()),
                        round(bar[0], 6), round(bar[1], 6), round(bar[2], 6), round(bar[3], 6),
                        v])
    if len(candles) > CANDLES_MAX_COUNT:
        candles = candles[-CANDLES_MAX_COUNT:]  # tail — same convention as trades/panes
    return candles


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
    # BLAVE_MODE lets the platform's signal-refresh cron run a strategy live
    # without editing the user's MODE constant (the file may still say
    # "backtest" while its signals feed the 下單設定 table). A cron-driven
    # run is also QUIET: no chart re-render, no chart/report pushed into the
    # chat or Telegram — an hourly pnl.png would spam the conversation and
    # re-upload images every report; the tick's job is the signal, nothing else.
    quiet         = bool(os.environ.get('BLAVE_MODE'))
    mode          = os.environ.get('BLAVE_MODE') or config['MODE']
    strategy_name = config['STRATEGY_NAME']
    fee           = config.get('FEE', 0.0005)
    interval      = config.get('INTERVAL', '1h')

    # TAIFEX settlement-mask enforcement — an unmasked backtest on the unadjusted
    # continuous series books roll gaps as fake PnL, so refuse to produce stats at
    # all rather than deliver silently-wrong numbers (AGENTS.md › Backtest Output;
    # detection lives in lib/quality_check.py). Checked before the data fetch so a
    # doomed run costs nothing. Live/cron runs are NOT blocked here: stopping an
    # already-deployed strategy's signal feed is a fleet-ops decision, not this
    # guard's call.
    if mode == 'backtest' and config.get('__file__'):
        from lib.quality_check import txf_settlement_findings
        problems = txf_settlement_findings(config['__file__'])
        if problems:
            raise SystemExit(
                '\n'.join(f"❌ Line {p['line']}: {p['msg']}" for p in problems)
                + '\n❌ Backtest refused — apply txf_settlement_mask, then re-run.'
            )

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
        mdd        = -abs(mdd_raw) * 100  # drawdown is a loss from peak → always ≤ 0
        bench_ret  = (close_v[-1] / close_v[0] - 1) * 100
        total_fees = float(tc_daily.sum()) * 100
        n_trades   = int(np.count_nonzero(np.nan_to_num(delta_w)))

        def _v(x):
            if x is None: return None
            if isinstance(x, float) and (math.isnan(x) or math.isinf(x)): return None
            return round(float(x), 4)

        # Per-event trade log for the workspace overlay. One row per nonzero delta_w[t],
        # each carrying `position` = the post-event position (w_curr[t]) — authoritative
        # for the frontend's flat/flip/open classification. Cumsum-rebuilding from the
        # 4dp-rounded deltas is NOT equivalent: the quantization residual (~1e-4 per
        # event) dwarfs any zero-epsilon, so vol-scaled continuous-weight strategies
        # never read as flat, and any event dropped below makes the rebuilt curve
        # drift permanently. Price alignment MUST mirror precise_pnl's own exec_shifted
        # logic (same t index, same close_v[t-1]/open_v[t] choice) or the overlay marks
        # won't land on the bar precise_pnl actually priced the trade at.
        #
        # delta_w_clean (nan_to_num'd) is only used to locate nonzero indices — the values
        # actually written per event come from the raw (un-cleaned) arrays and go through
        # _v() below, so a genuine nan/inf delta/price is dropped instead of silently
        # surviving as a large-but-finite number that math.isinf() can no longer catch.
        delta_w_clean = np.nan_to_num(delta_w)
        trades = []
        # Pre-event position (w_prev[t], the same chain precise_pnl differenced) per
        # retained event — kept parallel to `trades` so tail-truncation below can
        # anchor the surviving events for frontends still on the legacy cumsum
        # fallback (data produced before per-event `position` existed).
        pre_positions = []
        for t in np.flatnonzero(delta_w_clean):
            dw_raw = float(delta_w[t])
            dw     = _v(dw_raw)
            if dw is None:
                continue  # nan/inf delta — drop the event rather than write a bad value

            price_raw = close_v[t - 1] if exec_shifted[t] else open_v[t]
            price     = _v(price_raw)
            if price is None or price <= 0:
                continue  # nan/inf/non-positive price (e.g. futures overnight gap) — drop the event

            position = _v(w_curr[t])
            if position is None:
                continue  # nan/inf post-event position — drop the event, same as delta/price above

            trades.append({
                # epoch seconds, not isoformat() — matches the live-mode candles payload
                # below; isoformat() is tz-naive for crypto but tz-aware for TXF/twstock,
                # which JS `new Date()` parses inconsistently (local time vs. UTC offset).
                'ts':        int(df.index[t].timestamp()),
                'price':     price,
                'direction': 'buy' if dw_raw > 0 else 'sell',
                'delta':     dw,
                'position':  position,
            })
            pre_positions.append(float(w_prev[t]))
        if len(trades) > TRADES_MAX_COUNT:
            trades        = trades[-TRADES_MAX_COUNT:]  # tail — most recent trades matter first
            pre_positions = pre_positions[-TRADES_MAX_COUNT:]
        # Position before the first retained event: start + Σ(retained deltas) walks the
        # same values w_curr held. Always written so the frontend needn't special-case
        # truncation; null (nan/inf via _v) means "no anchor — fall back".
        trades_start_position = _v(pre_positions[0]) if pre_positions else 0.0

        print(f"  Total Return: {total_ret:.2f}%  Sharpe: {sharpe:.2f}  MDD: {mdd:.2f}%")
        print(f"  Fee Rate: {fee*100:.4f}%  Total Fees: {total_fees:.2f}%  Trades: {n_trades}")
        if n_trades == 0:
            print("  ⚠️ WARNING: 0 trades — the entry condition never fired; "
                  "all stats are meaningless. Check thresholds against the data's actual range.")

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

        stats = {'strategy': strategy_name, 'symbol': config.get('SYMBOL'), 'interval': interval,
                 'start': config.get('START'), 'end': df.index[-1].strftime('%Y-%m-%d'),
                 'fee [%]': round(fee * 100, 4),
                 'Total Return [%]':     _v(total_ret),
                 'Benchmark Return [%]': _v(bench_ret),
                 'Max Drawdown [%]':     _v(mdd),
                 'Sharpe Ratio':         _v(sharpe),
                 'Sortino Ratio':        _v(sortino),
                 'Omega Ratio':          _v(omega),
                 'Total Fees Paid [%]':  round(total_fees, 4),
                 'Trades':               n_trades,
                 'daily_dates': d_dates, 'daily_returns': d_rets,
                 'trades': trades,
                 'trades_start_position': trades_start_position,
                 }
        panes = _build_panes(config.get('PLOT_SERIES'), df)
        if panes:  # optional field — absent (not empty) when nothing is declared/valid
            stats['panes'] = panes
        candles = _build_candles(df)
        if candles:  # optional field — same absent-not-empty convention as panes
            stats['candles'] = candles
        json.dump(stats, open(out_dir / 'stats.json', 'w'), indent=2)

        if not quiet:
            plot_pnl(df, result_d, title=strategy_name,
                     output_path=str(out_dir / 'pnl.png'))
            # Mirror the chart into the web workspace chat (no-op off web); separate
            # from the Telegram gate below so it shows regardless of send_telegram_fn.
            from lib.notify import report_photo_web
            report_photo_web(str(out_dir / 'pnl.png'))

        if mode == 'backtest':
            if send_telegram_fn:
                from lib.notify import send_photo
                send_photo(str(out_dir / 'pnl.png'))
                send_telegram_fn(
                    f"回測完成：{strategy_name}\n"
                    f"Return {total_ret:.1f}%  "
                    f"Sharpe {sharpe:.2f}  "
                    f"MDD {mdd:.1f}%  "
                    f"Trades {n_trades}"
                    + ("\n⚠️ 0 筆交易——進場條件從未觸發，數字無意義" if n_trades == 0 else "")
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

        n_trades = int(np.count_nonzero(np.nan_to_num(delta_w)))

        print(f"  Total Return:  {total_ret:.1%}")
        print(f"  Ann. Return:   {ann_ret:.1%}")
        print(f"  Sharpe Ratio:  {sharpe:.2f}")
        print(f"  Max Drawdown:  {mdd:.1%}")
        print(f"  Fee Rate:      {fee*100:.4f}%  Total Fees: {tc_daily.sum()*100:.2f}%  Trades: {n_trades}")
        if n_trades == 0:
            print("  ⚠️ WARNING: 0 trades — the weight vector never changed; "
                  "all stats are meaningless. Check thresholds against the data's actual range.")

        from lib.analysis import random_bh_benchmark
        bench_stats, bench_pct = random_bh_benchmark(close_df, total_ret * 100, sharpe)

        def _v(x):
            if x is None: return None
            if isinstance(x, float) and (math.isnan(x) or math.isinf(x)): return None
            return round(float(x), 4)

        from lib.pnl import daily_returns_typeC
        d_dates, d_rets = daily_returns_typeC(pf_series)

        json.dump(
            {'strategy': strategy_name, 'interval': interval,
             'start': close_df.index[0].strftime('%Y-%m-%d'),
             'end':   close_df.index[-1].strftime('%Y-%m-%d'),
             'fee': fee,
             'Total Return [%]':    _v(total_ret * 100),
             'Ann. Return [%]':     _v(ann_ret   * 100),
             'Sharpe Ratio':        _v(sharpe),
             'Max Drawdown [%]':    _v(mdd       * 100),
             'Total Fees Paid [%]': round(float(tc_daily.sum()) * 100, 4),
             'Trades':              n_trades,
             **bench_stats,
             'daily_dates': d_dates, 'daily_returns': d_rets,
             },
            open(out_dir / 'stats.json', 'w'), indent=2
        )

        if not quiet:
            plot_pnl_portfolio(pf_series, close_df, title=strategy_name,
                               output_path=str(out_dir / 'pnl.png'),
                               bench_pct=bench_pct)
            # Mirror into the web workspace chat (no-op off web), regardless of the
            # Telegram gate below.
            from lib.notify import report_photo_web
            report_photo_web(str(out_dir / 'pnl.png'))

        if send_telegram_fn and not quiet:
            from lib.notify import send_photo
            send_photo(str(out_dir / 'pnl.png'))
            send_telegram_fn(
                f"回測完成：{strategy_name}\n"
                f"總報酬 {total_ret:.1%}  年化 {ann_ret:.1%}\n"
                f"Sharpe {sharpe:.2f}  MDD {mdd:.1%}  Trades {n_trades}"
                + ("\n⚠️ 0 筆交易——權重從未變動，數字無意義" if n_trades == 0 else "")
            )

    else:
        raise TypeError(f"compute_fn must return pd.Series (Type A) or tuple (Type C), got {type(result)}")
