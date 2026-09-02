import hashlib, json, logging, math, os, shutil, time
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
PANES_MAX_LEVELS = 4   # horizontal threshold lines per series
PANES_LEVEL_LABEL_MAX = 16

# Cap for the stats.json `candles` output (Type A backtest OHLCV) — mirrored server-side
# in api/openclaw/agent_strategies.py. Same per-series budget as PANES_MAX_POINTS: daily
# bars = full history, 5min ≈ ten weeks — coordinated with the trades 10,000 tail window.
CANDLES_MAX_COUNT = 20000

# Full-history chart export (strategies/<name>/chart/, Type A backtest only) — the
# stats.json tails above are the first-paint payload; this is the complete version the
# workspace pulls chunk-by-chunk (api/openclaw/agent_chart_data.py). Caps mirrored there.
CHART_CHUNK_BARS = 20000
CHART_MAX_CHUNKS = 50
CHART_LIVE_REFRESH_MIN_AGE = 86400  # live-tick rebuild throttle: at most once a day per strategy



# Keys lib.validation.write_mcpt_to_stats merges into stats.json after a manual MCPT run.
MCPT_KEYS = ('MCPT p-value', 'MCPT Permutations')
# Flat epoch-seconds key stamping when the stats were produced by an explicit backtest.
# The web compares scan.json's generated_at against it: a scan older than the last
# backtest means the parameters may have moved, so scan.current is shown as unknown.
GENERATED_AT_KEY = 'Generated At'


def _carry_over(out_dir, mode):
    """Keys to carry from the existing stats.json into the one about to be rewritten.

    A live / cron tick (mode != 'backtest') rewrites stats.json every bar with the SAME
    code and parameters, so the MCPT p-value computed on them is still valid, and the
    last explicit backtest's 'Generated At' still marks when the parameters last changed
    — both are read off the existing file and kept. An explicit backtest returns {} so
    the MCPT keys are DROPPED (parameters may have changed; the user re-runs MCPT via
    write_mcpt_to_stats) and 'Generated At' is stamped fresh by the caller. A missing /
    unreadable / keyless old file is simply "nothing to carry" — never fatal, a tick
    must not die on it.
    """
    if mode == 'backtest':
        return {}
    try:
        with open(Path(out_dir) / 'stats.json', encoding='utf-8') as f:
            old = json.load(f)
        return {k: old[k] for k in MCPT_KEYS + (GENERATED_AT_KEY,) if k in old}
    except Exception as e:  # absent on first tick, or a half-written file — carry nothing
        logging.debug("stats.json carry-over skipped: %s", e)
        return {}


def _build_levels(raw):
    """opts["levels"] → stats.json panes[].levels (horizontal threshold lines).

    Accepts {label: value} or [value, ...]. Non-finite / non-numeric values are
    skipped (not the whole declaration), labels are truncated, first 4 kept. A
    sentinel "disabled" threshold (e.g. -1e9) is the author's to leave out — the
    runner does not guess which values are off-scale.
    """
    if isinstance(raw, dict):
        items = [(k, v) for k, v in raw.items()]
    elif isinstance(raw, (list, tuple)):
        items = [(None, v) for v in raw]
    else:
        return []
    levels = []
    for label, v in items:
        if len(levels) >= PANES_MAX_LEVELS:
            break
        if isinstance(v, bool):
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(fv):
            continue
        lvl = {'value': round(fv, 6)}
        if isinstance(label, str) and label.strip():
            lvl['label'] = label[:PANES_LEVEL_LABEL_MAX]
        levels.append(lvl)
    return levels


def _build_panes(plot_series, df, max_points=PANES_MAX_POINTS):
    """PLOT_SERIES declaration → stats.json `panes` (workspace indicator overlay).

    plot_series is a dict of display name → spec, where spec is one of:
      "col"                              — column of the backtest df (sub-pane)
      pd.Series                          — reindexed to the df (sub-pane)
      ("col" | pd.Series, {"overlay": True})  — drawn on the price chart instead
      ("col" | pd.Series, {"pane": "<group>"}) — series sharing a group id render in
                                          one sub-pane (e.g. MACD + its signal line)
      ("col" | pd.Series, {"levels": {...} | [...]}) — horizontal threshold lines
                                          in that series' pane (see _build_levels)

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
        if max_points and len(points) > max_points:
            points = points[-max_points:]  # tail — same convention as trades
        entry = {
            'name':    str(name)[:PANES_NAME_MAX],
            'overlay': bool(opts.get('overlay', False)),
            'points':  points,
        }
        pane = opts.get('pane')
        if isinstance(pane, str) and pane.strip() and len(pane) <= PANES_PANE_MAX:
            entry['pane'] = pane
        levels = _build_levels(opts.get('levels'))
        if levels:  # optional field — absent (not empty) when nothing is declared/valid
            entry['levels'] = levels
        panes.append(entry)
    return panes


def _build_candles(df, max_count=CANDLES_MAX_COUNT):
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
    if max_count and len(candles) > max_count:
        candles = candles[-max_count:]  # tail — same convention as trades/panes
    return candles


def _slice_ts(rows, t0, t1, key):
    """rows sorted ascending by key(row) → the contiguous sub-list with t0 <= ts <= t1."""
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if key(rows[mid]) < t0: lo = mid + 1
        else: hi = mid
    start = lo
    hi = len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if key(rows[mid]) <= t1: lo = mid + 1
        else: hi = mid
    return rows[start:lo]


def _write_chart_dir(out_dir, df, candles, panes, trades, symbol, interval):
    """Full-history chart export → strategies/<name>/chart/{manifest.json, chunk-<id>.json}.

    Same element schemas as stats.json's candles / panes[].points / trades, but untruncated
    and split into CHART_CHUNK_BARS-bar chunks (by df.index position, so a pane point or
    trade on a bar whose candle was skipped still lands in exactly one chunk). More than
    CHART_MAX_CHUNKS → keep the newest and flag manifest.truncated. Built in chart.tmp/
    and swapped in whole (the reporter never sees a half-written set; per-chunk sha1 in
    the manifest lets it detect a swap that raced its read). Hash = the chunk content, so
    re-running an identical backtest is a no-op for the uploader.
    """
    chart_dir = out_dir / 'chart'
    if not candles:
        shutil.rmtree(chart_dir, ignore_errors=True)  # no stale chart from a previous run
        return None
    n = len(df)
    n_chunks = -(-n // CHART_CHUNK_BARS)
    first = max(0, n_chunks - CHART_MAX_CHUNKS)
    truncated = first > 0
    tmp_dir = out_dir / 'chart.tmp'
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir)
    pane_pts = [p['points'] for p in panes]
    chunks, hasher = [], hashlib.sha1()
    for cid, ci in enumerate(range(first, n_chunks)):
        lo, hi = ci * CHART_CHUNK_BARS, min((ci + 1) * CHART_CHUNK_BARS, n)
        t0, t1 = int(df.index[lo].timestamp()), int(df.index[hi - 1].timestamp())
        c_rows = _slice_ts(candles, t0, t1, lambda r: r[0])
        body = {
            'candles': c_rows,
            'panes':   [{**p, 'points': _slice_ts(pts, t0, t1, lambda r: r[0])}
                        for p, pts in zip(panes, pane_pts)],
            'trades':  _slice_ts(trades, t0, t1, lambda r: r['ts']),
        }
        raw = json.dumps(body, separators=(',', ':')).encode()
        sha = hashlib.sha1(raw).hexdigest()
        hasher.update(sha.encode())
        with open(tmp_dir / f'chunk-{cid}.json', 'wb') as f:
            f.write(raw)
        chunks.append({'id': cid, 't0': t0, 't1': t1, 'bars': len(c_rows),
                       'sha1': sha, 'bytes': len(raw)})
    manifest = {
        'v': 1, 'hash': hasher.hexdigest(),
        'symbol': symbol, 'interval': interval,
        'start': df.index[first * CHART_CHUNK_BARS].strftime('%Y-%m-%d'),
        'end':   df.index[-1].strftime('%Y-%m-%d'),
        'chunk_bars': CHART_CHUNK_BARS, 'truncated': truncated, 'chunks': chunks,
    }
    with open(tmp_dir / 'manifest.json', 'w') as f:
        json.dump(manifest, f)
    _swap_chart_dir(tmp_dir, chart_dir)
    return manifest


def _swap_chart_dir(tmp_dir, chart_dir, attempts=5):
    """chart.tmp → chart, via chart → chart.old first, so the live set is never half-deleted:
    on Windows a rename fails while the reporter has a chunk open or Defender is scanning
    the fresh files, and a plain rmtree+rename would leave a gutted chart/ behind. Retries
    briefly; if it still fails the old set is put back and the error propagates."""
    old_dir = chart_dir.with_name(chart_dir.name + '.old')
    for attempt in range(attempts):
        try:
            if chart_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)
                os.rename(chart_dir, old_dir)
            os.rename(tmp_dir, chart_dir)
            break
        except OSError:
            if attempt == attempts - 1:
                if old_dir.exists() and not chart_dir.exists():
                    try: os.rename(old_dir, chart_dir)
                    except OSError: pass
                raise
            time.sleep(0.5)
    shutil.rmtree(old_dir, ignore_errors=True)


def _chart_refresh_due(out_dir, tail_first_ts, tail_last_ts):
    """Live tick: should chart/ be rebuilt? stats.json's candle tail keeps advancing while
    chart/ stays frozen at backtest time; once the tail's first bar has moved past the last
    chunk's t1 minus half the tail window, the two would soon stop overlapping and the
    workspace would show a gap scrolling left. Rebuild then (or when no usable manifest),
    throttled by the manifest's mtime so a short-window strategy can't rebuild every tick.
    """
    path = out_dir / 'chart' / 'manifest.json'
    try:
        with open(path) as f:
            last_t1 = json.load(f)['chunks'][-1]['t1']
        mtime = os.path.getmtime(path)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return True
    if tail_first_ts <= last_t1 - (tail_last_ts - tail_first_ts) / 2:
        return False
    return time.time() - mtime >= CHART_LIVE_REFRESH_MIN_AGE


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
        # Defensive import: a workspace whose lib/quality_check.py predates this
        # guard (partial/stale sync) must not crash every backtest — fail open.
        try:
            from lib.quality_check import txf_settlement_findings
        except ImportError:
            logging.warning("lib/quality_check.py is stale — settlement-mask guard skipped")
            txf_settlement_findings = None
        problems = txf_settlement_findings(config['__file__']) if txf_settlement_findings else []
        if problems:
            raise SystemExit(
                '\n'.join(f"❌ Line {p['line']}: {p['msg']}" for p in problems)
                + '\n❌ Backtest refused — apply txf_settlement_mask, then re-run.'
            )

        # Pinned-END enforcement — nothing on the live path overrides END, so a
        # fixed date freezes a deployed strategy's signals at that date forever.
        # Refusing the backtest closes the deployment funnel (the deploy ritual
        # starts with one); live/cron runs are NOT blocked, same fleet-ops
        # reasoning as above.
        try:
            from lib.quality_check import end_pinned_findings
        except ImportError:
            logging.warning("lib/quality_check.py is stale — pinned-END guard skipped")
            end_pinned_findings = None
        problems = end_pinned_findings(config['__file__']) if end_pinned_findings else []
        if problems:
            raise SystemExit(
                '\n'.join(f"❌ Line {p['line']}: {p['msg']}" for p in problems)
                + '\n❌ Backtest refused — set END = None, then re-run.'
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
        trades_full = trades
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
        # Non-blocking, unlike the settlement guard: a missing PLOT_SERIES only costs
        # the workspace its indicator pane, so hint in the output the agent reads
        # after every backtest rather than refuse the run.
        if mode == 'backtest' and config.get('__file__'):
            try:
                from lib.quality_check import plot_series_findings
            except ImportError:  # stale lib/quality_check.py on this workspace
                logging.warning("lib/quality_check.py is stale — PLOT_SERIES hint skipped")
                plot_series_findings = None
            for p in (plot_series_findings(config['__file__']) if plot_series_findings else []):
                logging.warning(p['msg'])
                print(f"  ⚠️ WARNING: {p['msg']}")

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
        stats.update(_carry_over(out_dir, mode))  # live tick keeps MCPT + Generated At; backtest drops/restamps
        stats.setdefault(GENERATED_AT_KEY, int(time.time()))
        json.dump(stats, open(out_dir / 'stats.json', 'w'), indent=2)

        # Full chart export on every user-run backtest; a live/cron tick rewrites stats.json
        # every few minutes and re-serializing years of bars each time would burn the VM for
        # nothing (the uploader is content-hashed, but the build isn't free) — so live only
        # rebuilds when the stats tail is about to outrun chart/ (see _chart_refresh_due).
        if mode == 'backtest' or (candles and _chart_refresh_due(
                out_dir, candles[0][0], int(df.index[-1].timestamp()))):
            try:
                _write_chart_dir(out_dir, df, _build_candles(df, max_count=None),
                                 _build_panes(config.get('PLOT_SERIES'), df, max_points=None),
                                 trades_full, config.get('SYMBOL'), interval)
            except Exception as e:  # the chart is an extra; stats/pnl/notify still ship
                logging.warning("chart export failed: %s", e)

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

        carried = _carry_over(out_dir, mode)  # live tick keeps MCPT + Generated At; backtest drops/restamps
        carried.setdefault(GENERATED_AT_KEY, int(time.time()))
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
             **carried,
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
