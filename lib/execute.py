import importlib.util
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state(strategy_name):
    path = f'strategies/{strategy_name}/state.json'
    return json.load(open(path)) if os.path.exists(path) else None


def save_state(strategy_name, state):
    json.dump(state, open(f'strategies/{strategy_name}/state.json', 'w'), indent=2)


def update_state(candle, signal, state, mode, symbol=None, send_telegram_fn=None):
    """Process one candle: update position state only. Orders are placed by the reconciler."""
    price    = candle['close']
    prev_pos = float(state.get('position', 0))
    new_pos  = float(signal)
    if symbol:
        state['symbol'] = symbol
    if math.isnan(new_pos):
        return  # nan = hold: keep current position unchanged

    def _log(action):
        logging.info(f"{action} @ {price}")
        if mode == 'live' and send_telegram_fn:
            send_telegram_fn(f"Signal: {action} @ {price}")

    # Close or flip
    if prev_pos != 0 and (new_pos == 0 or new_pos * prev_pos < 0):
        action = 'SELL' if prev_pos > 0 else 'COVER'
        state['position'] = 0.0
        _log(action)

    # Open or scale
    if new_pos != 0:
        if state['position'] == 0:
            action = 'BUY' if new_pos > 0 else 'SHORT'
            state['position'] = new_pos
            _log(action)
        elif new_pos != prev_pos:
            state['position'] = new_pos
            logging.info(f"SCALE {prev_pos:+.2f}→{new_pos:+.2f} @ {price}")


# ---------------------------------------------------------------------------
# TWAP execution
# ---------------------------------------------------------------------------

def run_twap(
    symbol,
    side,
    total_qty,
    duration_min,
    n_slices,
    place_slice_fn,
    twap_key,
    signal_price=None,
    send_telegram_fn=None,
    stop_event=None,
    notify_slices=True,
):
    """
    Execute a TWAP order. Exchange-agnostic.

    Args:
        symbol         : trading symbol (e.g. 'BTCUSDT')
        side           : 'buy' | 'sell'
        total_qty      : total quantity to execute (same unit as place_slice_fn expects)
        duration_min   : total TWAP window in minutes
        n_slices       : number of equal-sized slices
        place_slice_fn : callable(symbol, side, qty) -> {'fill_price': float, 'fill_qty': float}
                         Must raise on failure — caller is responsible for exchange-specific
                         retry / order-type logic. Exceptions are caught, logged, and recorded;
                         execution continues on the next slice.
        twap_key       : execution key for the log path: manager/twap/{twap_key}.jsonl.
                         Orders are netted per symbol (no single strategy name exists at
                         execution time), so this is a symbol+direction key like
                         'btcusdt_long' / 'btcusdt_short', NOT a strategy name.
                         Logs live under manager/ (never strategies/, which is for strategy.py).
        stop_event     : optional threading.Event — checked before every slice and
                         during inter-slice waits; once set, remaining slices are
                         skipped and the summary carries aborted=True. The residual
                         is NOT re-ordered here — the caller's reconcile loop picks
                         up the remaining target/actual gap.
        notify_slices  : False silences per-slice success Telegrams (a 60-slice TWAP
                         must not send 60 messages) — start, errors and the summary
                         still notify.
        signal_price   : price at signal time (optional) — used to compute slippage vs signal.
                         For buy:  slippage = (vwap - signal_price) / signal_price * 10000 bps
                         For sell: slippage = (signal_price - vwap) / signal_price * 10000 bps
                         Positive = worse fill than signal price.
        send_telegram_fn: optional callable(str) for per-slice + summary Telegram updates

    Returns:
        summary dict (same record written to twap log with type='summary'):
        {
          'type': 'summary',
          'twap_key', 'symbol', 'side',
          'total_target', 'total_filled', 'n_filled',
          'vwap', 'signal_price', 'slippage_bps',
          'duration_min', 'n_slices', 'aborted',
          'start_ts', 'end_ts'
        }

    Log schema (manager/twap/{twap_key}.jsonl, one JSON per line):
        Slice record   — type='slice':   ts, twap_key, symbol, side,
                                         slice_n, of_n, target_qty,
                                         fill_qty, fill_price, elapsed_s,
                                         slippage_bps (null if no signal_price),
                                         error (only on failure)
        Summary record — type='summary': aggregated stats for the full TWAP run

    Impact analysis: use load_twap_log(twap_key) to read slices + summaries,
    then compare slippage_bps across runs with different duration_min / n_slices.
    """
    log_path = f"manager/twap/{twap_key}.jsonl"
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    interval_s = (duration_min * 60) / n_slices
    slice_qty  = total_qty / n_slices
    start_ts   = datetime.now(timezone.utc).isoformat()
    start_time = time.time()
    fills      = []
    consec_err = 0
    aborted    = False

    msg = f"TWAP start: {side.upper()} {total_qty} {symbol} | {n_slices} slices over {duration_min}m"
    logging.info(msg)
    if send_telegram_fn:
        send_telegram_fn(msg)

    for i in range(n_slices):
        if stop_event is not None and stop_event.is_set():
            aborted = True
            break
        slice_start = time.time()
        ts = datetime.now(timezone.utc).isoformat()
        record = {
            "ts": ts, "type": "slice", "twap_key": twap_key,
            "symbol": symbol, "side": side,
            "slice_n": i + 1, "of_n": n_slices,
            "target_qty": round(slice_qty, 8),
        }

        try:
            result      = place_slice_fn(symbol, side, slice_qty)
            fill_price  = float(result["fill_price"])
            fill_qty    = float(result["fill_qty"])
            elapsed_s   = round(time.time() - start_time, 1)

            if signal_price:
                raw = (fill_price - signal_price) / signal_price * 10000
                slippage_bps = round(raw if side == "buy" else -raw, 2)
            else:
                slippage_bps = None

            record.update({
                "fill_qty": fill_qty, "fill_price": fill_price,
                "elapsed_s": elapsed_s, "slippage_bps": slippage_bps,
            })
            fills.append({"fill_price": fill_price, "fill_qty": fill_qty})
            consec_err = 0

            slip_str  = f" | slip={slippage_bps:+.1f}bps" if slippage_bps is not None else ""
            slice_msg = f"TWAP {i+1}/{n_slices}: {fill_qty} @ {fill_price}{slip_str}"
            logging.info(slice_msg)
            if notify_slices and send_telegram_fn:
                send_telegram_fn(slice_msg)

        except Exception as e:
            record["error"] = str(e)
            consec_err += 1
            logging.error(f"TWAP slice {i+1}/{n_slices} error: {e}")
            if send_telegram_fn:
                send_telegram_fn(f"TWAP {i+1}/{n_slices} ERROR: {e}")

        with open(log_path, "a") as f:
            f.write(json.dumps(record) + "\n")

        # 3 consecutive failed slices = the venue is rejecting this order shape
        # (halt, minimum, dead key) — the remaining slices would fail the same
        # way and each one costs a Telegram; stop and let reconcile re-diff.
        if consec_err >= 3:
            aborted = True
            abort_msg = f"TWAP aborted: {consec_err} consecutive slice failures"
            logging.error(abort_msg)
            if send_telegram_fn:
                send_telegram_fn(abort_msg)
            break

        if i < n_slices - 1:
            delay = max(0.0, interval_s - (time.time() - slice_start))
            if stop_event is not None:
                stop_event.wait(delay)
            else:
                time.sleep(delay)

    # Build summary
    total_filled = sum(r["fill_qty"] for r in fills)
    if total_filled > 0:
        vwap = round(sum(r["fill_price"] * r["fill_qty"] for r in fills) / total_filled, 8)
    else:
        vwap = None

    if signal_price and vwap:
        raw = (vwap - signal_price) / signal_price * 10000
        summary_slip = round(raw if side == "buy" else -raw, 2)
    else:
        summary_slip = None

    end_ts  = datetime.now(timezone.utc).isoformat()
    summary = {
        "ts": end_ts, "type": "summary", "twap_key": twap_key,
        "symbol": symbol, "side": side,
        "total_target": round(total_qty, 8),
        "total_filled": round(total_filled, 8),
        "n_filled": len(fills),
        "vwap": vwap, "signal_price": signal_price, "slippage_bps": summary_slip,
        "duration_min": duration_min, "n_slices": n_slices,
        "aborted": aborted,
        "start_ts": start_ts, "end_ts": end_ts,
    }

    with open(log_path, "a") as f:
        f.write(json.dumps(summary) + "\n")

    slip_str = f" | slip={summary_slip:+.1f}bps" if summary_slip is not None else ""
    done_word = "stopped" if aborted else "done"
    done_msg = f"TWAP {done_word}: {side.upper()} {total_filled}/{total_qty} {symbol} | VWAP={vwap}{slip_str}"
    logging.info(done_msg)
    if send_telegram_fn:
        send_telegram_fn(done_msg)

    return summary


def load_twap_log(twap_key):
    """
    Read all TWAP records for an execution key (e.g. 'btcusdt_long'). Returns
    (slices, summaries).

    Use for impact analysis — compare vwap vs signal_price across runs,
    or plot slippage_bps vs duration_min to find optimal TWAP parameters.
    """
    log_path = f"manager/twap/{twap_key}.jsonl"
    if not os.path.exists(log_path):
        return [], []

    slices, summaries = [], []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception as e:
                logging.warning(f"twap_log parse error: {e}")
                continue
            (slices if rec.get("type") == "slice" else summaries).append(rec)

    return slices, summaries


# ---------------------------------------------------------------------------
# Execution dispatch (下單方式) — reconciler-facing
# ---------------------------------------------------------------------------
# The web 下單設定 stores a per-strategy execution style in
# portfolio_config.json["execution"]:
#     {"<strategy>": {"type": "market"}
#                  | {"type": "twap",   "duration_min": 30}
#                  | {"type": "chase"}
#                  | {"type": "custom", "module": "<manager/executors/*.py>"}}
# No entry = market. dispatch_order() is the single entry point the reconciler
# template routes place_order through (official auto-wired venues only — TW
# brokers keep their hand-wired signed-diff reconcilers and never come here).
#
# Market orders place synchronously, exactly the pre-execution behaviour.
# TWAP / custom run in a DAEMON THREAD so a 30-minute window never blocks the
# reconcile loop (other symbols keep trading, heartbeat stays fresh):
#   - while a symbol's execution is in flight, further legs for that symbol
#     return False (the reconcile "intentionally skipped" contract) — the
#     residual gap is simply re-observed next round;
#   - a target that FLIPS direction mid-flight sets the stop event; the thread
#     exits before its next slice and the completion kick re-reconciles;
#   - completion touches state/execution/kick (watched by the reconciler) so
#     convergence resumes within one poll instead of the 5-minute heartbeat;
#   - a reconciler restart drops the in-memory registry and any thread with it:
#     the residual gap re-dispatches fresh on the next round — self-healing.

_EXECUTORS_DIR = "manager/executors"
_KICK_PATH = "state/execution/kick"
_MIN_SLICE_USD = 20.0        # floor only — the real per-venue minimum (min qty
                             # × mark can be $100+ on BTC perps) is looked up at
                             # dispatch via _venue_min_slice_usd
_RESIDUAL_USD = 10.0         # tracks manager/reconciler.THRESHOLD — a gap
                             # smaller than this is not worth an order
_OVERDUE_GRACE_S = 900
_REDUCE_ESCAPE_S = 600       # overdue + stopped this long, a REDUCE leg may
                             # abandon the wedged run — closes must never be
                             # trapped behind a stuck executor

_inflight = {}               # symbol key -> run dict (thread/stop/sign/...)
_inflight_lock = threading.Lock()

_notify_fn = None


def _get_notify():
    """Same optional-Telegram contract as the reconciler: no pairing → log.
    The returned callable NEVER raises — a paired sender's send failure (429,
    network) must not abort an execution mid-flight or skip its accounting."""
    global _notify_fn
    if _notify_fn is None:
        try:
            from lib.notify import make_sender
            raw = make_sender()

            def _safe(msg):
                try:
                    raw(msg)
                except Exception as e:
                    logging.warning(f"[execute] notify failed ({e}): {msg}")
            _notify_fn = _safe
        except Exception as e:
            logging.warning(f"[execute] telegram unavailable ({e}) — log only")
            _notify_fn = lambda m: logging.warning(f"[notify-unavailable] {m}")
    return _notify_fn


def _touch_kick():
    try:
        os.makedirs(os.path.dirname(_KICK_PATH), exist_ok=True)
        with open(_KICK_PATH, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError as e:
        logging.warning(f"[execute] kick touch failed: {e}")


def _venue_mark_price(symbol):
    """Best-effort arrival price from the bound venue lib (None if unavailable).
    Used as run_twap's signal_price so slippage-vs-arrival lands in the twap log,
    and exposed to custom executors as ctx.mark_price()."""
    try:
        import importlib as _il
        from lib import venue_wiring as vw
        from lib.portfolio import split_key
        env = vw.read_env()
        vid = vw.detect_venue(env)
        if not vid:
            return None
        order = _il.import_module(f"lib.order_{vid}")
        sym, market = split_key(symbol)
        if market == "spot":
            return float(order.get_spot_price(env, sym))
        return float(order.get_mark_price(env, sym))
    except Exception as e:
        logging.debug(f"[execute] mark price unavailable for {symbol}: {e}")
        return None


def resolve_execution(contributors, config=None):
    """The execution spec for one netted order. Orders are netted per symbol
    (multiple contributing strategies, no single name at execution time), so
    the largest-|contribution| strategy's spec decides; that strategy having
    no entry means market, even if a smaller contributor configured TWAP."""
    if not contributors:
        return {"type": "market"}
    try:
        from lib.portfolio import load_portfolio_config
        cfg = (config if config is not None else load_portfolio_config()).get("execution") or {}
    except Exception as e:
        # mid-write read (the web listener rewrites portfolio_config.json) —
        # defer the leg (None → skip) instead of trading a WRONG style once
        logging.warning(f"[execute] execution config unreadable ({e}) — leg deferred")
        return None
    top = max(contributors, key=lambda c: abs(float(c.get("contribution", 0) or 0)))
    spec = cfg.get(top.get("strategy"))
    return spec if isinstance(spec, dict) else {"type": "market"}


def _venue_min_slice_usd(symbol):
    """Smallest USD a slice can be on the bound venue: our floor, the
    instrument's min qty valued at mark (BTC perps: $100+ — the floor alone
    caused an abort/re-dispatch loop), and its min notional. Best-effort: a
    failed lookup returns the floor — a too-small slice then just falls back
    to one market order at placement instead of blocking dispatch."""
    floor = _MIN_SLICE_USD
    try:
        import importlib as _il
        from lib import venue_wiring as vw
        from lib.portfolio import split_key
        env = vw.read_env()
        vid = vw.detect_venue(env)
        if not vid:
            return floor
        order = _il.import_module(f"lib.order_{vid}")
        sym, market = split_key(symbol)
        if market == "spot":
            return floor  # spot minimums are far below the floor everywhere we ship
        r = order.get_contract_rules(env, sym)
        mark = float(order.get_mark_price(env, sym))
        min_qty_usd = (float(r.get("min_qty") or 0)
                       * float(r.get("contract_value") or 1) * mark)
        # 1.05: the mark moves between sizing and placement — a slice sized
        # exactly at the minimum floors to zero the moment price ticks up
        return max(floor, min_qty_usd * 1.05, float(r.get("min_notional") or 0))
    except Exception as e:
        logging.warning(f"[execute] venue min lookup failed for {symbol} ({e}) — "
                        f"using ${floor} floor")
        return floor


def _fallback_market(symbol, reason, signed_diff, asset_spec, reduce_only, exchange):
    """The user picked a style we cannot run. Doing nothing strands the
    position; silently doing market instead is the worse silence — so market
    order + a loud Telegram, every time it happens."""
    msg = f"⚠️ {symbol}: execution style unusable ({reason}) — placed a market order instead"
    logging.error(f"[execute] {msg}")
    try:
        _get_notify()(msg)
    except Exception:
        pass
    from lib.venue_wiring import auto_place_order
    return auto_place_order(symbol, signed_diff, asset_spec, reduce_only, exchange)


def dispatch_order(symbol, signed_diff, asset_spec=None, reduce_only=False,
                   exchange=None, contributors=None):
    """Reconciler place_order entry point for auto-wired venues.

    Same contract as place_order: False = intentionally skipped (below
    minimum, or the leg was handed to / deferred behind an async executor),
    dict = exchange-confirmed fills (synchronous market path only), raise =
    real failure. Async executions report their own fills to Telegram,
    manager/twap/*.jsonl and manager/orders.jsonl on completion.
    """
    from lib.venue_wiring import auto_place_order

    key = str(symbol)
    new_sign = 1 if signed_diff > 0 else -1
    abandoned = False
    with _inflight_lock:
        run = _inflight.get(key)
        if run and run["thread"].is_alive():
            now = time.time()
            if now > run["deadline"] + _OVERDUE_GRACE_S:
                run["stop"].set()
                run.setdefault("stop_at", now)
                if not run["overdue"]:
                    run["overdue"] = True
                    msg = (f"⚠️ {symbol}: {run['style']} execution overdue — stop "
                           f"requested; orders for this symbol wait until it exits")
                    logging.error(f"[execute] {msg}")
                    _get_notify()(msg)
            elif not run["reduce_only"] and new_sign != run["sign"]:
                # signal flipped while an ENTRY execution runs: stop before the
                # next slice; the completion kick re-diffs what actually filled.
                # A reduce (close) leg in flight is left alone — an opposite-
                # sign request there is just the flip's second leg queueing up.
                if not run["stop"].is_set():
                    logging.info(f"[execute] {key}: target flipped — stopping "
                                 f"in-flight {run['style']}")
                run["stop"].set()
                run.setdefault("stop_at", now)
            # Escape hatch, clocked from WHEN STOP WAS REQUESTED (not from the
            # deadline — a wedged 24h custom run must not trap a close for a
            # day): a run that ignored its stop for _REDUCE_ESCAPE_S is wedged;
            # abandon its registry entry and let the CLOSE through. At most one
            # already-in-flight slice can still land, which the next reconcile
            # nets back out.
            if (not abandoned and reduce_only and run["stop"].is_set()
                    and now - run.get("stop_at", now) > _REDUCE_ESCAPE_S):
                abandoned = True
                _inflight.pop(key, None)
                msg = (f"⚠️ {symbol}: abandoning wedged {run['style']} run to "
                       f"let a close order through")
                logging.error(f"[execute] {msg}")
                _get_notify()(msg)
            if not abandoned:
                if run["stop"].is_set():
                    logging.info(f"[execute] {key}: {run['style']} stopping — leg deferred")
                else:
                    logging.info(f"[execute] {key}: {run['style']} in flight — leg deferred")
                return False
        elif run:  # finished thread that has not been reaped
            _inflight.pop(key, None)

    spec = resolve_execution(contributors)
    if spec is None:  # config mid-write — skip, the next round re-reads
        return False
    stype = str(spec.get("type", "market"))

    if stype == "market":
        return auto_place_order(symbol, signed_diff, asset_spec, reduce_only, exchange)

    if stype == "twap":
        try:
            duration_min = min(max(int(spec.get("duration_min")), 1), 1440)
        except (TypeError, ValueError):
            return _fallback_market(symbol, f"bad twap duration {spec.get('duration_min')!r}",
                                    signed_diff, asset_spec, reduce_only, exchange)
        n_slices = int(min(duration_min, abs(signed_diff) // _venue_min_slice_usd(symbol)))
        if n_slices <= 1:
            # too small to slice — a straight market order IS its TWAP
            return auto_place_order(symbol, signed_diff, asset_spec, reduce_only, exchange)
        return _launch(key, f"twap:{duration_min}m", _twap_thread,
                       (symbol, signed_diff, asset_spec, reduce_only, exchange,
                        contributors, duration_min, n_slices),
                       signed_diff, reduce_only, deadline_s=duration_min * 60)

    if stype == "chase":
        from lib.venue_wiring import auto_limit_toolkit
        try:
            tools = auto_limit_toolkit(symbol, reduce_only=reduce_only)
        except Exception as e:
            return _fallback_market(symbol, f"limit toolkit failed: {e}",
                                    signed_diff, asset_spec, reduce_only, exchange)
        if tools is None:
            return _fallback_market(symbol, "venue has no limit layer yet",
                                    signed_diff, asset_spec, reduce_only, exchange)
        return _launch(key, "chase", _chase_thread,
                       (symbol, signed_diff, asset_spec, reduce_only, exchange,
                        contributors, tools),
                       signed_diff, reduce_only,
                       deadline_s=_CHASE_TIMEOUT_S + 60)

    if stype == "custom":
        module = str(spec.get("module") or "")
        if not re.fullmatch(r"[a-z0-9_]{1,64}", module):
            return _fallback_market(symbol, f"bad custom module name {module!r}",
                                    signed_diff, asset_spec, reduce_only, exchange)
        path = os.path.join(_EXECUTORS_DIR, module + ".py")
        if not os.path.isfile(path):
            return _fallback_market(symbol, f"{path} not found",
                                    signed_diff, asset_spec, reduce_only, exchange)
        try:
            mspec = importlib.util.spec_from_file_location(f"executors.{module}", path)
            mod = importlib.util.module_from_spec(mspec)
            mspec.loader.exec_module(mod)
            fn = getattr(mod, "execute")
        except Exception as e:
            return _fallback_market(symbol, f"custom executor {module} failed to load: {e}",
                                    signed_diff, asset_spec, reduce_only, exchange)
        return _launch(key, f"custom:{module}", _custom_thread,
                       (symbol, signed_diff, asset_spec, reduce_only, exchange,
                        contributors, module, fn),
                       signed_diff, reduce_only, deadline_s=24 * 3600)

    return _fallback_market(symbol, f"unknown execution type {stype!r}",
                            signed_diff, asset_spec, reduce_only, exchange)


def _reap_own(key):
    """Registry cleanup that only removes the CALLING thread's own entry — an
    abandoned (wedged) thread waking up late must not pop the entry of the
    replacement run that took over its symbol."""
    with _inflight_lock:
        run = _inflight.get(key)
        if run and run["thread"] is threading.current_thread():
            _inflight.pop(key, None)


def _launch(key, style, target, args, signed_diff, reduce_only, deadline_s):
    stop = threading.Event()
    t = threading.Thread(target=target, args=args + (stop,),
                         daemon=True, name=f"exec-{key}")
    with _inflight_lock:
        _inflight[key] = {
            "thread": t, "stop": stop, "style": style,
            "sign": 1 if signed_diff > 0 else -1, "reduce_only": reduce_only,
            "deadline": time.time() + deadline_s, "overdue": False,
        }
        # started inside the lock: registered-but-not-alive must never be
        # observable, or a concurrent dispatch would reap it as finished
        t.start()
    logging.info(f"[execute] {key}: {style} dispatched (${abs(signed_diff):,.2f})")
    return False


def _make_slice_fn(symbol, asset_spec, reduce_only, exchange, side, stop, venue_seen,
                   why=None):
    """USD-denominated market slice via the venue wiring. fill_qty is returned
    in USD (not base units) so run_twap's target/filled accounting stays in
    one currency. `why` (optional dict) records the stop reason so the caller
    can pick the right recovery (below_min → one market shot; halt → wait)."""
    from lib import guard
    from lib.venue_wiring import auto_place_order
    is_entry = not reduce_only

    def _slice(_sym, _side, usd):
        if stop.is_set():
            raise RuntimeError("execution stopped")
        if is_entry and guard.halted():
            if why is not None:
                why["stop"] = "halt"
            stop.set()
            raise RuntimeError("state/HALT set — stopping execution")
        signed = usd if side == "buy" else -usd
        placed = auto_place_order(symbol, signed, asset_spec, reduce_only, exchange)
        if placed is False:
            if why is not None:
                why["stop"] = "below_min"
            stop.set()
            raise RuntimeError("slice below exchange minimum — stopping execution")
        price = float(placed.get("avg_price") or 0)
        if not price:
            raise RuntimeError(f"venue returned no fill price: {placed}")
        venue_seen["id"] = placed.get("exchange") or venue_seen["id"]
        return {"fill_price": price,
                "fill_qty": float(placed.get("executed_qty") or 0) * price}

    return _slice


def _finish(symbol, signed_diff, asset_spec, reduce_only, exchange, contributors,
            style, filled_usd, vwap, aborted):
    """Async completion: one orders.jsonl entry (the web 交易歷史 source of
    truth) mirroring the synchronous reconcile entry shape."""
    from lib.portfolio import _append_reconciler_log, _record_order_error
    if filled_usd <= 0:
        _record_order_error(symbol, exchange, f"{style}: no slices filled")
        return
    leg = {"signed_diff": round(filled_usd if signed_diff > 0 else -filled_usd, 2),
           "reduce_only": reduce_only, "exchange": exchange}
    if vwap:
        leg["fill_price"] = vwap
        leg["executed_qty"] = round(filled_usd / vwap, 8)
    entry = {
        "action": "BUY" if signed_diff > 0 else "SELL",
        "symbol": symbol,
        "signed_diff": round(signed_diff, 2),
        "exchange": exchange,
        "asset_spec": asset_spec,
        "contributors": contributors or [],
        "legs": [leg],
        "execution": style,
    }
    if aborted or abs(signed_diff) - filled_usd > _RESIDUAL_USD:
        entry["failed"] = True  # partial — the residual re-reconciles
    _append_reconciler_log(entry)


def _twap_thread(symbol, signed_diff, asset_spec, reduce_only, exchange,
                 contributors, duration_min, n_slices, stop):
    from lib.venue_wiring import auto_place_order
    key = str(symbol)
    style = f"twap:{duration_min}m"
    side = "buy" if signed_diff > 0 else "sell"
    total = abs(round(signed_diff, 2))
    twap_key = f"{symbol.replace('@', '_').lower()}_{'long' if signed_diff > 0 else 'short'}"
    venue_seen = {"id": exchange}
    why = {}
    filled = 0.0
    notify = _get_notify()
    try:
        summary = run_twap(
            symbol, side, total, duration_min, n_slices,
            _make_slice_fn(symbol, asset_spec, reduce_only, exchange, side, stop,
                           venue_seen, why),
            twap_key, signal_price=_venue_mark_price(symbol),
            send_telegram_fn=notify, stop_event=stop, notify_slices=False)
        filled = float(summary.get("total_filled") or 0)
        vwap = summary.get("vwap")
        aborted = bool(summary.get("aborted"))
        # A slice under the venue minimum means NO slice of this order will
        # ever fit (sizing missed the real minimum) — one market shot for the
        # remainder instead of an abort/re-dispatch loop that never converges.
        remaining = total - filled
        if why.get("stop") == "below_min" and remaining > _RESIDUAL_USD:
            placed = auto_place_order(symbol, remaining if signed_diff > 0 else -remaining,
                                      asset_spec, reduce_only, exchange)
            if isinstance(placed, dict):
                price = float(placed.get("avg_price") or 0)
                usd = float(placed.get("executed_qty") or 0) * price
                if usd > 0:
                    vwap = (round(((vwap or 0) * filled + price * usd) / (filled + usd), 8)
                            if filled + usd > 0 else price)
                    filled += usd
                    aborted = False
                notify(f"TWAP {symbol}: slices under the venue minimum — "
                       f"remainder ${usd:,.2f} filled at market")
            # placed False = even one market order is under the minimum:
            # nothing to do, stay quiet (matches the market path's semantics)
        _finish(symbol, signed_diff, asset_spec, reduce_only, venue_seen["id"],
                contributors, style, filled, vwap, aborted)
    except Exception as e:
        logging.error(f"[execute] {key} {style} crashed: {e}")
        from lib.portfolio import _record_order_error
        _record_order_error(symbol, venue_seen["id"], e)
        notify(f"⚠️ TWAP execution error {symbol}: {e}")
    finally:
        _reap_own(key)
        # kick only when a re-reconcile can achieve something: fills moved the
        # position, or an EXTERNAL stop (flip) asked for a re-diff. Internal
        # stops (below_min/halt) with zero fill would re-dispatch into the same
        # failure every few seconds — let the 5-min heartbeat retry instead
        # (halt trips its own mtime wake-up anyway).
        if filled > 0 or (stop.is_set() and "stop" not in why):
            _touch_kick()


_CHASE_POLL_S = 1.0        # status/BBO re-check cadence while a limit rests
_CHASE_TIMEOUT_S = 45.0    # total chase window; the remainder then goes market
_CHASE_MAX_REPLACES = 20   # cancel-replace budget (thin books re-quote fast)


def _chase_thread(symbol, signed_diff, asset_spec, reduce_only, exchange,
                  contributors, tools, stop):
    """Chase-limit execution (下單方式「限價追價」): post-only at the top of our
    own side of the book, cancel-replace as the BBO moves, and after the chase
    window market the remainder — the style shapes the price, never whether the
    target is reached. Mirrors Hummingbot's MAKER resubmission loop and Ember's
    passive→active escalation, collapsed to zero user parameters.
    """
    from lib import guard
    from lib.venue_wiring import auto_place_order

    key = str(symbol)
    buy = signed_diff > 0
    is_entry = not reduce_only
    total = abs(round(signed_diff, 2))
    remaining = total
    fills = []          # {'fill_price', 'fill_qty'(USD)}
    notify = _get_notify()
    aborted = False
    reason = None

    active_oid = None       # resting order, if the thread dies mid-flight
    cancel_uncertain = False

    def _account(oid):
        """Post-cancel/fill accounting for one resting order. Best-effort: an
        unreadable status must not skip registry cleanup — it costs one order's
        fill in the log, and the reconcile re-diff trues the position up."""
        try:
            st = tools["status"](oid)
        except Exception as e:
            logging.warning(f"[execute] {key} chase status read failed ({e}) — "
                            f"order {oid} unaccounted")
            return 0.0
        filled_usd = st["executed_qty"] * (st["avg_price"] or 0)
        if filled_usd > 0:
            fills.append({"fill_price": st["avg_price"], "fill_qty": filled_usd})
        return filled_usd

    _CANCEL_GONE = ("already_gone", "gone", "canceled", "cancelled",
                    "order_not_found", "order_finished", "order_closed",
                    "order_cancelled", "finished")  # finished = Gate.io's
    # terminal on a successful cancel (may mean filled-first — _account
    # reads the fills either way)

    def _retire(oid):
        """Cancel + CONFIRM terminal. True only when the venue says the order
        is dead/filled — re-posting while the old order may still rest is the
        double-fill path, so an unconfirmed cancel ends the whole chase.
        The cancel RESPONSE itself often already carries the terminal state —
        trust that first; the follow-up status read is for venues that don't.
        Inter-attempt wait is a real sleep: stop is usually already set here,
        and an eventually-consistent venue needs the second to catch up."""
        for _ in range(2):
            try:
                res = tools["cancel"](oid)
                if (isinstance(res, dict)
                        and str(res.get("status", "")).lower() in _CANCEL_GONE):
                    return True
            except Exception as e:
                logging.warning(f"[execute] {key} chase cancel failed ({e})")
            try:
                if tools["status"](oid)["status"] in ("filled", "canceled"):
                    return True
            except Exception as e:
                logging.warning(f"[execute] {key} chase status read failed ({e})")
            time.sleep(1.0)
        return False

    try:
        deadline = time.time() + _CHASE_TIMEOUT_S
        replaces = 0
        start_notified = False
        while (remaining > _RESIDUAL_USD and not stop.is_set()
               and time.time() < deadline and replaces < _CHASE_MAX_REPLACES):
            if is_entry and guard.halted():
                stop.set()
                reason = "state/HALT set"
                break
            bid, ask = tools["bbo"]()
            px = bid if buy else ask
            placed = tools["place"](remaining, px, None, buy)
            if placed is False:
                reason = "below venue minimum"
                break
            if not start_notified:
                # only after the venue accepts working this size — a below-min
                # order must not announce a chase every heartbeat retry
                start_notified = True
                notify(f"Chase start: {'BUY' if buy else 'SELL'} ${total:,.2f} "
                       f"{symbol} (post-only, {int(_CHASE_TIMEOUT_S)}s window)")
            if placed.get("status") == "post_only_rejected":
                replaces += 1
                stop.wait(0.2)  # book moved through us — re-read and re-post
                continue
            oid = placed.get("order_id")
            if not oid:
                raise RuntimeError(f"place_limit_order returned no order_id: {placed}")
            active_oid = oid

            while not stop.is_set() and time.time() < deadline:
                stop.wait(_CHASE_POLL_S)
                st = tools["status"](oid)
                if st["status"] in ("filled", "canceled"):
                    # canceled = venue-side (expiry/ADL) — account either way,
                    # from the row we already hold (a re-read that fails would
                    # silently drop a KNOWN fill)
                    filled_usd = st["executed_qty"] * (st["avg_price"] or 0)
                    if filled_usd > 0:
                        fills.append({"fill_price": st["avg_price"],
                                      "fill_qty": filled_usd})
                    active_oid = oid = None
                    break
                nbid, nask = tools["bbo"]()
                if (nbid if buy else nask) != px:
                    break  # price moved — cancel and re-post at the new top
            if oid is not None:
                if not _retire(oid):
                    cancel_uncertain = True
                    aborted = True
                    reason = "cancel unconfirmed — a resting order may remain"
                    stop.set()
                    notify(f"⚠️ {symbol}: chase could not confirm its resting order "
                           f"was cancelled — not re-posting; restart the reconciler "
                           f"to sweep it if it lingers")
                    break
                _account(oid)
                active_oid = None
                replaces += 1
            remaining = max(0.0, total - sum(f["fill_qty"] for f in fills))

        if stop.is_set() and reason is None:
            reason = "stopped"
            aborted = True

        # Escalation: whatever the chase window left unfilled goes market —
        # unless the platform asked us to stop (flip/HALT re-diffs instead),
        # or the old resting order's fate is unknown (double-fill risk).
        remaining = max(0.0, total - sum(f["fill_qty"] for f in fills))
        if remaining > _RESIDUAL_USD and not stop.is_set() and not cancel_uncertain:
            placed = auto_place_order(symbol, remaining if buy else -remaining,
                                      asset_spec, reduce_only, exchange)
            if isinstance(placed, dict):
                price = float(placed.get("avg_price") or 0)
                qty = float(placed.get("executed_qty") or 0)
                if price and qty:
                    fills.append({"fill_price": price, "fill_qty": qty * price})
                reason = reason or "window expired — remainder filled at market"

        filled = sum(f["fill_qty"] for f in fills)
        vwap = (round(sum(f["fill_price"] * f["fill_qty"] for f in fills) / filled, 8)
                if filled > 0 else None)
        if filled > 0 or stop.is_set():
            tail = f" ({reason})" if reason else ""
            notify(f"Chase done: {'BUY' if buy else 'SELL'} "
                   f"${filled:,.2f}/${total:,.2f} {symbol} | VWAP={vwap}{tail}")
        else:
            logging.info(f"[execute] {key} chase ended with no fills ({reason})")
        _finish(symbol, signed_diff, asset_spec, reduce_only, tools.get("venue"),
                contributors, "chase", filled, vwap, aborted)
    except Exception as e:
        logging.error(f"[execute] {key} chase crashed: {e}")
        if active_oid is not None:
            # dying with a resting order: one best-effort retire now beats
            # waiting for the next reconciler restart's sweep. An UNCONFIRMED
            # retire must set cancel_uncertain — otherwise the finally kick
            # re-dispatches a new chase beside an order of unknown fate (the
            # exact double-fill the main-loop guard exists to prevent).
            try:
                if not _retire(active_oid):
                    cancel_uncertain = True
                    notify(f"⚠️ {symbol}: chase crashed and could not confirm its "
                           f"resting order was cancelled — restart the reconciler "
                           f"to sweep it if it lingers")
            except Exception as retire_err:
                logging.warning(f"[execute] {key} crash-path retire raised: {retire_err}")
                cancel_uncertain = True
                notify(f"⚠️ {symbol}: chase crashed and could not confirm its "
                       f"resting order was cancelled — restart the reconciler "
                       f"to sweep it if it lingers")
        from lib.portfolio import _record_order_error
        _record_order_error(symbol, tools.get("venue"), e)
        notify(f"⚠️ Chase execution error {symbol}: {e}")
    finally:
        _reap_own(key)
        # cancel_uncertain: do NOT kick — an immediate re-dispatch would post a
        # new order while the ambiguous one may still rest (the exact double-
        # fill this abort exists to prevent). The 5-min heartbeat retries after
        # the venue has had time to settle.
        if not cancel_uncertain and (sum(f["fill_qty"] for f in fills) > 0
                                     or stop.is_set()):
            _touch_kick()


class ExecutionContext:
    """What a custom executor gets to work with — see references/lib.md.

    All quantities are USD (account currency). place_slice() places one market
    order leg through the same venue wiring, HALT and audit included; there is
    no way around the platform guardrails from here.
    """

    def __init__(self, place_fn, stop_event, notify_fn, mark_price_fn):
        self._place, self._stop = place_fn, stop_event
        self._notify, self._mark = notify_fn, mark_price_fn

    def place_slice(self, usd):
        """Market-execute `usd` (positive USD) of the order's own direction.
        Returns {'fill_price': float, 'fill_qty': float}  (fill_qty in USD).
        Raises on failure — including once stop_requested() is set."""
        usd = float(usd)
        if usd <= 0:
            raise ValueError("place_slice needs a positive USD amount")
        return self._place("_", "_", usd)

    def stop_requested(self):
        """True once the platform wants this execution to end (signal flip,
        overdue, HALT). Check it in every loop and return promptly."""
        return self._stop.is_set()

    def sleep(self, seconds):
        """Interruptible wait. Returns False if stop was requested meanwhile —
        use `if not ctx.sleep(60): return` as the loop's pulse."""
        self._stop.wait(float(seconds))
        return not self._stop.is_set()

    def mark_price(self):
        """Live mark/spot price of the order's symbol, or None."""
        return self._mark()

    def notify(self, msg):
        """Telegram (or log, when unpaired) — use sparingly."""
        try:
            self._notify(str(msg))
        except Exception as e:
            logging.warning(f"[execute] ctx.notify failed: {e}")


def _custom_thread(symbol, signed_diff, asset_spec, reduce_only, exchange,
                   contributors, module, fn, stop):
    key = str(symbol)
    style = f"custom:{module}"
    side = "buy" if signed_diff > 0 else "sell"
    venue_seen = {"id": exchange}
    notify = _get_notify()
    fills = []
    why = {}
    total = abs(round(signed_diff, 2))
    raw_slice = _make_slice_fn(symbol, asset_spec, reduce_only, exchange, side, stop,
                               venue_seen, why)

    def _tracked(_s, _d, usd):
        # a buggy executor looping "to be safe" must not build N× the order —
        # 10% headroom covers price drift between sizing and fills
        if sum(f["fill_qty"] for f in fills) + float(usd) > total * 1.10:
            raise ValueError("executor exceeding the order's total — slice refused")
        fill = raw_slice(_s, _d, usd)
        fills.append(fill)
        return fill

    ctx = ExecutionContext(_tracked, stop, notify,
                           lambda: _venue_mark_price(symbol))
    aborted = False
    try:
        notify(f"Executor {module} start: {side.upper()} ${total:,.2f} {symbol}")
        fn(symbol, side, total, ctx)
    except Exception as e:
        aborted = True
        logging.error(f"[execute] {key} {style} raised: {e}")
        notify(f"⚠️ Executor {module} error {symbol}: {e}")
    finally:
        filled = 0.0
        try:
            filled = sum(f["fill_qty"] for f in fills)
            vwap = (round(sum(f["fill_price"] * f["fill_qty"] for f in fills) / filled, 8)
                    if filled > 0 else None)
            # the accounting record comes FIRST — a notify hiccup must never
            # leave real fills out of orders.jsonl
            _finish(symbol, signed_diff, asset_spec, reduce_only, venue_seen["id"],
                    contributors, style, filled, vwap, aborted or stop.is_set())
            notify(f"Executor {module} done: {side.upper()} "
                   f"${filled:,.2f}/${total:,.2f} {symbol} | VWAP={vwap}")
        except Exception as e:
            logging.error(f"[execute] {key} {style} completion record failed: {e}")
        _reap_own(key)
        # same internal-stop gating as TWAP: a below_min/halt self-stop with
        # zero fill must not re-dispatch itself every few seconds
        if filled > 0 or (stop.is_set() and "stop" not in why):
            _touch_kick()
