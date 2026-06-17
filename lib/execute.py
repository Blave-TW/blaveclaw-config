import json
import logging
import math
import os
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
          'duration_min', 'n_slices',
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

    msg = f"TWAP start: {side.upper()} {total_qty} {symbol} | {n_slices} slices over {duration_min}m"
    logging.info(msg)
    if send_telegram_fn:
        send_telegram_fn(msg)

    for i in range(n_slices):
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

            slip_str  = f" | slip={slippage_bps:+.1f}bps" if slippage_bps is not None else ""
            slice_msg = f"TWAP {i+1}/{n_slices}: {fill_qty} @ {fill_price}{slip_str}"
            logging.info(slice_msg)
            if send_telegram_fn:
                send_telegram_fn(slice_msg)

        except Exception as e:
            record["error"] = str(e)
            logging.error(f"TWAP slice {i+1}/{n_slices} error: {e}")
            if send_telegram_fn:
                send_telegram_fn(f"TWAP {i+1}/{n_slices} ERROR: {e}")

        with open(log_path, "a") as f:
            f.write(json.dumps(record) + "\n")

        if i < n_slices - 1:
            time.sleep(max(0.0, interval_s - (time.time() - slice_start)))

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
        "start_ts": start_ts, "end_ts": end_ts,
    }

    with open(log_path, "a") as f:
        f.write(json.dumps(summary) + "\n")

    slip_str = f" | slip={summary_slip:+.1f}bps" if summary_slip is not None else ""
    done_msg = f"TWAP done: {side.upper()} {total_filled}/{total_qty} {symbol} | VWAP={vwap}{slip_str}"
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
