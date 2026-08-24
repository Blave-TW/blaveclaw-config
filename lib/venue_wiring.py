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
import json
import logging
import math
import os
import re
import time
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


# Deployment redline L2 (spec §3.2): manager/credentials.ui.json is written by
# the platform runtime on every UI bind — the venue ids (paper included) whose
# credentials the user confirmed through the web. When it exists, only those
# ids can route; keys an agent hand-writes into .env never become a live
# venue. Missing/corrupt manifest = fail-open (pre-manifest machines
# unchanged). Warn-once set is process-lifetime — official_venues runs every
# reconcile round and must not spam the log; the USER notify (spec §3.2, a
# filtered venue means "looks bound, will not trade" and staying silent hides
# it) has its own 24h stamp-file cooldown, same pattern as lib/portfolio's
# _ui_override_alert.
_CRED_MANIFEST_PATH = "manager/credentials.ui.json"
_MANIFEST_ALERT_STAMP_PATH = "state/ui_credentials_alert"
_MANIFEST_ALERT_COOLDOWN_S = 24 * 3600
_MANIFEST_ALERT_MSG = ("機器上的交易所金鑰與投資組合頁的綁定不一致,"
                       "以投資組合頁為準——請在投資組合頁重新連接交易所")
_manifest_filtered_warned = set()


def _ui_bound_ids():
    """Lowercased venue ids from the UI bind manifest, or None when
    absent/invalid (fail-open — see comment above)."""
    try:
        with open(_CRED_MANIFEST_PATH) as f:
            ids = json.load(f).get("ids")
        if not isinstance(ids, list):
            return None
        return {str(i).lower() for i in ids}
    except (OSError, ValueError, AttributeError):
        return None


def _manifest_filtered_alert(vid):
    """A vid with keys+libs got filtered by the manifest: log once per
    process, and tell the user once per 24h (stamp written BEFORE sending so
    a slow send can't spam; failed stamp still sends — same trade-off as
    lib/portfolio._ui_override_alert)."""
    if vid not in _manifest_filtered_warned:
        _manifest_filtered_warned.add(vid)
        logging.warning(
            f"[venue_wiring] {vid} has keys+libs but is not in the web bind "
            f"manifest ({_CRED_MANIFEST_PATH}) — not routed; bind it from the "
            f"投資組合 page")
    try:
        if os.path.exists(_MANIFEST_ALERT_STAMP_PATH) and \
                time.time() - os.path.getmtime(_MANIFEST_ALERT_STAMP_PATH) \
                < _MANIFEST_ALERT_COOLDOWN_S:
            return
        os.makedirs(os.path.dirname(_MANIFEST_ALERT_STAMP_PATH), exist_ok=True)
        with open(_MANIFEST_ALERT_STAMP_PATH, "w") as f:
            f.write(datetime.utcnow().isoformat())
    except OSError as e:
        logging.warning(f"[venue_wiring] manifest alert stamp failed: {e}")
    try:
        from lib.notify import send_text
        send_text(_MANIFEST_ALERT_MSG)
    except Exception:
        logging.error(f"[notify-unavailable] {_MANIFEST_ALERT_MSG}")


def official_venues(env):
    """Venue ids with keys in .env AND both official libs on disk — and, when
    the UI bind manifest exists, listed in it (see _ui_bound_ids)."""
    allowed = _ui_bound_ids()
    out = []
    for k in env:
        m = _ENV_KEY_RE.match(k + "=")
        if not m:
            continue
        vid = m.group(1).lower()
        if m.group(1).upper() in _RESERVED_PREFIXES or vid in _NON_AUTO:
            continue
        if os.path.isfile(f"lib/account_{vid}.py") and os.path.isfile(f"lib/order_{vid}.py"):
            if allowed is not None and vid not in allowed:
                _manifest_filtered_alert(vid)
                continue
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
    from datetime import timezone
    return "rc" + datetime.now(timezone.utc).strftime("%y%m%d%H%M%S%f")


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
    possible, oversell impossible).

    self_ledger EXCEPTION (measured live 2026-08-20 on uid 29026): with
    portfolio_config["self_ledger"] on, the account position is NOT all the
    bot's — the cap includes the user's own manual holding in the same symbol
    and direction, so ceiling eats one lot step out of the MANUAL position
    (closing the bot's 0.017 ETH ceiled to 0.018, selling $2.25 of the user's
    coins — the exact touch self_ledger exists to prevent). Reduce legs FLOOR
    in that mode: the bot may keep a sub-lot dust residual in its own ledger
    (below the reconcile threshold, never re-ordered), but it can never sell
    what it doesn't own."""
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
            from lib.portfolio import load_portfolio_config
            if load_portfolio_config().get("self_ledger"):
                return min(math.floor(qty / lot + 1e-9) * lot, held)
            return min(math.ceil(qty / lot - 1e-9) * lot, held)
    except Exception as e:
        logging.warning(f"[venue_wiring] reduce ceil/cap skipped ({e}) — floor path")
    return qty


_TERMINAL_FILLED = {"filled"}
# every venue's own terminal-dead states must be here — a missing one makes the
# chase loop treat a dead order as open and idle out its whole window
# (expired_in_match = Binance STP, failed = BingX, mmp_canceled = OKX)
_TERMINAL_GONE = {"canceled", "cancelled", "rejected", "expired",
                  "expired_in_match", "failed", "mmp_canceled"}


def _norm_status(raw):
    s = str(raw or "").lower()
    if s in _TERMINAL_FILLED:
        return "filled"
    if s in _TERMINAL_GONE:
        return "canceled"
    if s == "post_only_rejected":
        return "post_only_rejected"
    return "open"


def auto_limit_toolkit(symbol, reduce_only=False):
    """Limit-layer toolkit for the chase executor (lib.execute), or None when
    the bound venue does not ship the limit layer — the caller then falls back
    to a market order, loudly.

    All quantities are the reconciler's USD terms; conversion to base qty
    happens here at the caller-supplied limit price. Returned callables:
      bbo()                    -> (bid, ask) floats
      place(usd, price, cid)   -> {'order_id', ...} | {'status':'post_only_rejected'}
                                  | False (below venue minimum). Always post-only.
      status(order_id)         -> {'status': open|filled|canceled|post_only_rejected,
                                   'orig_qty','executed_qty','avg_price'} (base qty)
      cancel(order_id)         -> best-effort; "already filled/gone" must not raise
    `usd` is UNSIGNED — direction is fixed at toolkit build time from the leg's
    signed_diff sign (chase never flips a leg mid-flight; a flipped target stops
    the execution instead).
    """
    env = read_env()
    vid = detect_venue(env)
    if vid is None:
        _no_venue()
    order = importlib.import_module(f"lib.order_{vid}")
    sym, market = split_key(symbol)

    if market == "spot":
        need = ("place_spot_limit_order", "cancel_spot_order",
                "get_spot_order", "get_spot_bbo", "get_spot_balances")
        if not all(hasattr(order, n) for n in need):
            return None

        def _place(usd, price, cid, _buy):
            base_qty = abs(usd) / price
            if not _buy:
                held = order.get_spot_balances(env).get(_spot_base(sym), 0.0)
                base_qty = min(base_qty, held)
            return order.place_spot_limit_order(
                env, sym, "buy" if _buy else "sell", base_qty, price,
                client_order_id=cid or _cid(), post_only=True)

        def _spot_bbo():
            b = order.get_spot_bbo(env, sym)
            return float(b["bid"]), float(b["ask"])

        return {
            "venue": vid,
            "bbo": _spot_bbo,
            "place": lambda usd, price, cid, _buy: _place(usd, price, cid, _buy),
            "status": lambda oid: _norm_order(order.get_spot_order(env, sym, oid)),
            "cancel": lambda oid: order.cancel_spot_order(env, sym, oid),
        }

    need = ("place_limit_order", "cancel_order", "get_order", "get_bbo")
    if not all(hasattr(order, n) for n in need):
        return None

    def _place(usd, price, cid, _buy):
        qty = abs(usd) / price
        if reduce_only:
            direction = "long" if not _buy else "short"  # closing that side
            qty = _reduce_qty(env, vid, order, sym, direction, qty)
        else:
            direction = "long" if _buy else "short"
        return order.place_limit_order(
            env, sym, direction, qty, price, client_order_id=cid or _cid(),
            reduce_only=reduce_only, post_only=True)

    def _swap_bbo():
        b = order.get_bbo(env, sym)
        return float(b["bid"]), float(b["ask"])

    return {
        "venue": vid,
        "bbo": _swap_bbo,
        "place": lambda usd, price, cid, _buy: _place(usd, price, cid, _buy),
        "status": lambda oid: _norm_order(order.get_order(env, sym, oid)),
        "cancel": lambda oid: order.cancel_order(env, sym, order_id=oid),
    }


def _norm_order(row):
    row = dict(row or {})
    return {
        "status": _norm_status(row.get("status")),
        "orig_qty": float(row.get("orig_qty") or 0),
        "executed_qty": float(row.get("executed_qty") or 0),
        "avg_price": float(row.get("avg_price") or 0),
    }


# Our resting-order fingerprint: _cid() mints "rc" + a UTC microsecond
# timestamp. A user's own manual order will not carry it, so the sweep can
# cancel on match without touching anything the user placed themselves.
_RC_CID_RE = re.compile(r"rc\d{12,}")


def sweep_orphan_orders():
    """Cancel resting limit orders a dead reconciler left behind (chase posts
    them; a crash mid-chase strands one on the venue). Called once at
    reconciler startup — best-effort, never blocks the loop. Returns the count
    cancelled."""
    env = read_env()
    vid = detect_venue(env)
    if vid is None:
        return 0
    order = importlib.import_module(f"lib.order_{vid}")
    lanes = []
    if hasattr(order, "get_open_orders") and hasattr(order, "cancel_order"):
        lanes.append((order.get_open_orders,
                      lambda s, oid: order.cancel_order(env, s, order_id=oid)))
    if hasattr(order, "get_spot_open_orders") and hasattr(order, "cancel_spot_order"):
        lanes.append((order.get_spot_open_orders,
                      lambda s, oid: order.cancel_spot_order(env, s, oid)))
    n = 0
    for fetch, cancel in lanes:
        try:
            rows = fetch(env) or []
        except Exception as e:
            logging.warning(f"[venue_wiring] orphan sweep read failed: {e}")
            continue
        for row in rows:
            if not _RC_CID_RE.search(str(row.get("client_order_id") or "")):
                continue
            sym, oid = row.get("symbol"), row.get("order_id")
            if not sym or not oid:
                continue
            try:
                cancel(sym, oid)
                n += 1
                logging.info(f"[venue_wiring] cancelled orphaned order {oid} on {sym}")
            except Exception as e:
                logging.warning(f"[venue_wiring] orphan cancel {oid} failed: {e}")
    return n


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
