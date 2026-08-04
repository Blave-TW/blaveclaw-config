"""Flatten: market-close EVERY open position on every connected venue.

The web 全部平倉 button runs this (the command listener trips state/HALT first,
then launches this detached); it is also runnable by hand:

    cd workspace && python3 manager/flatten.py

Semantics — panic button, not portfolio management:
  - HALT is (re)tripped here too, so a manual run gets the same guarantee:
    nothing re-opens after the flatten (closes always pass the guard; only the
    user pressing 啟動下單 clears the halt).
  - Venues are discovered like the account reader: {PREFIX}_API_KEY in .env,
    flattenable iff BOTH lib/account_{id}.py and lib/order_{id}.py exist.
    A venue with positions but no order lib is reported loudly and skipped.
  - Dust below the exchange minimum can't be closed (format_qty gate) — it is
    logged and left; the exchange rejects sub-minimum orders anyway.
  - Every close is appended to manager/orders.jsonl with its confirmed fill,
    so the web 交易歷史 and the order toast show exactly what happened.
"""
import importlib
import logging
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import guard
from lib.portfolio import _append_reconciler_log, _record_order_error

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_ENV_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_]+)_API_KEY\s*=", re.IGNORECASE)
_RESERVED = {"BLAVE"}


def _read_env(path=".env"):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return env


def _venues(env):
    out = []
    for k in env:
        m = _ENV_KEY_RE.match(k + "=")
        if not m or m.group(1).upper() in _RESERVED:
            continue
        out.append(m.group(1).lower())
    return sorted(set(out))


def flatten():
    env = _read_env()
    if not guard.halted():
        guard.trip_halt("close all positions", "flatten")
    closed = errors = 0
    for vid in _venues(env):
        has_account = os.path.isfile(f"lib/account_{vid}.py")
        has_order = os.path.isfile(f"lib/order_{vid}.py")
        if not has_account:
            continue  # no reader — nothing to see here either
        try:
            acct = importlib.import_module(f"lib.account_{vid}")
            positions = acct.get_positions(env)
        except Exception as e:
            logging.error(f"[{vid}] get_positions failed: {e}")
            _record_order_error("*", vid, f"close-all: get_positions failed: {e}")
            errors += 1
            continue
        # Agent-written account libs sometimes return the reconciler dict shape
        # ({symbol: {side, size}}) instead of the contract list — iterating a
        # dict yields key strings and would crash the whole flatten. Adapt.
        if isinstance(positions, dict):
            positions = [{"symbol": k, **(v if isinstance(v, dict) else {})}
                         for k, v in positions.items()]
        if positions and not has_order:
            logging.error(f"[{vid}] HAS POSITIONS but no lib/order_{vid}.py — cannot flatten")
            _record_order_error("*", vid, f"close-all: positions exist but no order_{vid} lib")
            errors += 1
            continue
        if not positions:
            continue
        order = importlib.import_module(f"lib.order_{vid}")
        for p in positions:
            # One bad row must not abort the rest of the flatten — every branch
            # below either closes, records dust, or records a visible error.
            try:
                sym = (p.get("symbol") or "").replace("-", "").upper()
                side, size = p.get("side"), float(p.get("size", 0))
                if not sym or side not in ("long", "short") or size <= 0:
                    logging.error(f"[{vid}] unflattenable row skipped: {p!r:.120}")
                    if sym:
                        _record_order_error(sym, vid, f"close-all: bad position row (side={side})")
                        errors += 1
                    continue
                price = float(p.get("mark_price", 0) or 0)
                try:
                    order.format_qty(env, sym, size, price=price or None)
                except ValueError:
                    logging.info(f"[{vid}] {sym} {side} {size} below minimum — dust left")
                    continue
                cid = f"flat{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
                result = order.close_position_partial(env, sym, side, size, client_order_id=cid)
            except Exception as e:
                logging.error(f"[{vid}] close {p.get('symbol')} failed: {e}")
                _record_order_error(str(p.get("symbol") or "?"), vid, f"close-all: {e}")
                errors += 1
                continue
            notional = round(size * price, 2) if price else None
            leg = {"signed_diff": (-notional if side == "long" else notional) if notional else None,
                   "reduce_only": True, "exchange": vid}
            if isinstance(result, dict):
                if result.get("avg_price") is not None:
                    leg["fill_price"] = result["avg_price"]
                if result.get("executed_qty") is not None:
                    leg["executed_qty"] = result["executed_qty"]
            _append_reconciler_log({
                "action": "SELL" if side == "long" else "BUY",
                "symbol": sym,
                "signed_diff": leg["signed_diff"],
                "exchange": vid,
                "asset_spec": None,
                "contributors": [],
                "legs": [leg],
            })
            closed += 1
            logging.info(f"[{vid}] closed {side} {sym} ({size})")
    logging.info(f"flatten done: {closed} closed, {errors} errors")
    return errors == 0


if __name__ == "__main__":
    sys.exit(0 if flatten() else 1)
