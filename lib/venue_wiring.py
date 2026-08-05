"""
Auto-wiring: routes the reconciler through the machine's OFFICIAL venue libs.

Why this exists (measured 2026-08-04/05 on the onboarding test machine): the
reconciler template shipped with NotImplementedError stubs and relied on the
agent hand-wiring them per venue. Three failure modes followed — a venue whose
official libs were all present still crash-looped on the stubs ("page says
ready, reconciler is a shell"); rebinding to another exchange left the old
hand-wiring pointed at a venue whose keys were gone (auto-halt storms); and
every hand-wiring re-implemented the same USD-diff mapping with fresh bugs.
This module IS that mapping, written once, with every harvested fix baked in.

Scope: venues whose BOTH lib/account_{id}.py AND lib/order_{id}.py ship
officially (see references/exchange-connect.md rule 2). Venues without
official libs still need a hand-wired reconciler — the template's stubs say
how. Taiwan brokers (sinopac signed-diff contract) are NOT routed here.

Key contract (lib/portfolio): plain "BTCUSDT" keys = perp/swap,
"BTCUSDT@spot" = spot inventory (market_key/split_key); spot actuals come
from spot_scope() so removing a spot strategy sells its inventory down and
personal coins never enter.
"""
import importlib
import logging
import math
import os
import re
from datetime import datetime

from lib.portfolio import load_portfolio_config, market_key, split_key, spot_scope

_ENV_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_]+)_API_KEY\s*=", re.IGNORECASE)
_RESERVED_PREFIXES = {"BLAVE"}
_NON_AUTO = {"sinopac", "president", "capital"}  # TW brokers: signed-diff contract


def read_env(path=".env"):
    """Minimal .env parse — the dict shape every lib takes."""
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


def official_venues(env):
    """Venue ids with keys in .env AND both official libs on disk."""
    out = []
    for k in env:
        m = _ENV_KEY_RE.match(k + "=")
        if not m:
            continue
        vid = m.group(1).lower()
        if m.group(1).upper() in _RESERVED_PREFIXES or vid in _NON_AUTO:
            continue
        if os.path.isfile(f"lib/account_{vid}.py") and os.path.isfile(f"lib/order_{vid}.py"):
            out.append(vid)
    return sorted(set(out))


def detect_venue(env):
    """The venue this machine trades on, or None. One bound venue is the
    designed case (one portfolio, one account); with several, prefer the one
    portfolio_config routes strategies to, loudly."""
    vids = official_venues(env)
    if not vids:
        return None
    if len(vids) == 1:
        return vids[0]
    routed = [v for v in (load_portfolio_config().get("exchanges") or {}).values()
              if v in vids]
    pick = max(set(routed), key=routed.count) if routed else vids[0]
    logging.warning(f"[venue_wiring] multiple official venues bound {vids} — using {pick}")
    return pick


_SPOT_QUOTES = ("USDT", "USDC")


def _spot_base(sym):
    """Base asset of a spot pair. Unknown quote RAISES (audit H1): silently
    valuing it 0 makes actual read 0 forever while the buy path still trades —
    reconcile then re-buys the full target every round until the wallet is
    empty. A loud failure lands in the get_positions error contract and the
    auto-halt wrapper instead."""
    for q in _SPOT_QUOTES:
        if sym.endswith(q) and len(sym) > len(q):
            return sym[: -len(q)]
    raise RuntimeError(
        f"unsupported spot quote on {sym} (supported: {'/'.join(_SPOT_QUOTES)}) — "
        f"refusing to trade what cannot be inventory-read"
    )


def _no_venue():
    raise RuntimeError(
        "no officially-supported venue bound (keys + lib/account_*.py + "
        "lib/order_*.py) — for other venues, hand-wire manager/reconciler.py "
        "per references/exchange-connect.md"
    )


def _cid():
    return "rc" + datetime.utcnow().strftime("%y%m%d%H%M%S%f")


def auto_get_positions():
    """Reconciler get_positions: swap positions (plain keys, USD value) +
    spot inventory for spot-scoped symbols ("SYMBOL@spot" keys) when the
    venue's order lib ships a spot layer. Errors PROPAGATE — the template's
    error contract (an empty dict would read as flat and re-buy the whole
    target on link recovery)."""
    env = read_env()
    vid = detect_venue(env)
    if vid is None:
        _no_venue()
    acct = importlib.import_module(f"lib.account_{vid}")
    order = importlib.import_module(f"lib.order_{vid}")

    # net same-symbol rows (audit M3): hedge-mode accounts can report a long
    # AND a short row for one symbol — clobbering keeps only the last and the
    # reconciler then trades against a wrong actual. The reconciler's own
    # orders never create dual-side positions, but user-opened ones exist.
    net = {}
    positions = acct.get_positions(env)
    rows = (positions.items() if isinstance(positions, dict)
            else ((p["symbol"], p) for p in positions))
    for sym, p in rows:
        p = p or {}
        usd = p.get("size", 0) if isinstance(positions, dict) \
            else p["size"] * p["mark_price"]
        signed = usd if p.get("side") == "long" else -usd if p.get("side") == "short" else 0
        net[str(sym)] = net.get(str(sym), 0) + signed
    out = {}
    for sym, v in net.items():
        out[sym] = {"side": "long" if v > 0 else ("short" if v < 0 else None),
                    "size": abs(v)}

    if hasattr(order, "get_spot_balances"):
        balances = None

        def _inv_value(sym):
            nonlocal balances
            base = _spot_base(sym)  # unknown quote raises — see _spot_base
            if balances is None:
                balances = order.get_spot_balances(env)
            amt = balances.get(base, 0.0)
            return amt * order.get_spot_price(env, sym) if amt else 0.0

        for sym, size in spot_scope(_inv_value).items():
            out[market_key(sym, "spot")] = {"side": "long" if size else None, "size": size}
    return out


def _lot_base(order, env, sym):
    """One qty step in BASE units, across both rules dialects: new libs return
    'step' (+contract_value); the older bingx lib returns qty_precision
    (decimal places, contract_value 1)."""
    r = order.get_contract_rules(env, sym)
    step = r.get("step")
    if step is None and "qty_precision" in r:
        step = 10 ** -int(r["qty_precision"])
    return float(step or 0) * float(r.get("contract_value") or 1)


def _reduce_qty(env, vid, order, sym, direction, qty):
    """Reduce legs CEIL to a whole lot and cap at the actual position — the
    USD diff was computed at the snapshot's mark, so a full close converts to
    fractionally under the position's lot count and flooring strands one lot
    below the reconcile threshold forever (measured: closing 0.04 ct became
    0.03 ct, $6.4 residual). Ceiling is only safe WITH the cap, so when the
    position can't be read the qty is returned un-ceiled (floor path — dust
    possible, oversell impossible)."""
    try:
        acct = importlib.import_module(f"lib.account_{vid}")
        positions = acct.get_positions(env)
        held = 0.0
        if isinstance(positions, list):  # list contract: size is BASE units.
            # dict contract carries USD — no base cap possible, held stays 0.
            # SUM matching rows (audit M3): hedge accounts can split a side.
            for p in positions:
                if p["symbol"] == sym and p.get("side") == direction:
                    held += p["size"]
        lot = _lot_base(order, env, sym)
        if held and lot > 0:
            return min(math.ceil(qty / lot - 1e-9) * lot, held)
    except Exception as e:
        logging.warning(f"[venue_wiring] reduce ceil/cap skipped ({e}) — floor path")
    return qty


def auto_place_order(symbol, signed_diff, asset_spec=None, reduce_only=False,
                     exchange=None):
    """Reconciler place_order: routes on the key's market. Spot buys are sized
    in QUOTE currency directly; spot sells in base qty capped at the wallet's
    inventory; swap converts USD at the live mark (get_mark_price contract).
    Below exchange minimum -> False (intentional skip).

    `exchange` is the target's routing from portfolio_config (passed through
    by reconcile when the wiring accepts it). Audit H3: if it names a
    DIFFERENT venue that is itself bound+official, this machine has two live
    venues and silently trading on the detected one is real money on the
    wrong exchange — loud-skip instead. A stale label (previous venue, keys
    gone — the normal state right after a rebind, fixed by the next amounts
    save) only warns and routes to the detected venue."""
    env = read_env()
    vid = detect_venue(env)
    if vid is None:
        _no_venue()
    if exchange and exchange != vid:
        if exchange in official_venues(env):
            msg = (f"target routed to {exchange} but this wiring is on {vid} — "
                   f"refusing to trade it on the wrong venue (re-save 下單設定 "
                   f"to re-route)")
            logging.error(f"[venue_wiring] {symbol}: {msg}")
            try:
                from lib.portfolio import _record_order_error
                _record_order_error(symbol, exchange, msg)
            except Exception:
                pass
            return False
        logging.warning(f"[venue_wiring] {symbol}: routing label {exchange!r} is "
                        f"not a bound official venue — using {vid} (label goes "
                        f"stale after a rebind; the next amounts save fixes it)")
    order = importlib.import_module(f"lib.order_{vid}")
    sym, market = split_key(symbol)
    cid = _cid()

    if market == "spot":
        if not hasattr(order, "place_spot_market_order"):
            raise RuntimeError(f"{vid} has no official spot layer — "
                               f"spot strategy cannot trade on it")
        base = _spot_base(sym)  # unknown quote raises — never trade blind (H1)
        if signed_diff > 0:
            result = order.place_spot_market_order(
                env, sym, "buy", quote_qty=abs(signed_diff), client_order_id=cid)
        else:
            price = order.get_spot_price(env, sym)
            held = order.get_spot_balances(env).get(base, 0.0)
            qty = min(abs(signed_diff) / price, held)
            result = order.place_spot_market_order(
                env, sym, "sell", base_qty=qty, client_order_id=cid)
    else:
        mark = order.get_mark_price(env, sym)
        qty = abs(signed_diff) / mark
        if reduce_only:
            direction = "long" if signed_diff < 0 else "short"
            qty = _reduce_qty(env, vid, order, sym, direction, qty)
        else:
            direction = "long" if signed_diff > 0 else "short"
        try:
            result = order.place_market_order(env, sym, direction, qty,
                                              client_order_id=cid,
                                              reduce_only=reduce_only)
        except ValueError as e:
            # the older bingx lib RAISES below-min (predates the False-skip
            # contract). WHITELIST the min-size wording (audit M2): the same
            # type also carries real faults (missing keys, underivable
            # symbol) that must surface, not become a silent "skip".
            if "below" in str(e) or "floors to" in str(e):
                return False
            raise
    if result is False:
        return False
    placed = dict(result)
    placed["exchange"] = vid
    return placed
