"""
Paper price resolver — the fill/mark price for the simulated `paper` venue.

The rule (Wei 2026-08-21): a paper order fills at the price the STRATEGY itself
sees, from the STRATEGY's own data source — never an independent public feed.
Two strategies on the same symbol can pull it from different endpoints and even
different adjustments (crypto `fetch_kline`, futures `fetch_db_kline`, TW stock
`fetch_twstock_price_adj` — the last one is DIVIDEND-ADJUSTED, so a raw quote
would silently disagree with what the strategy traded on). So we resolve the
symbol back to the strategy trading it and call that strategy's `fetch_data`,
taking the latest bar's Close as "the current price".

Why this needs no FX / point-value / lot math even across asset classes: the
reconciler sizes orders as qty = USD ÷ price, so paper's PnL is
qty × (mark − entry) = USD_notional × (mark/entry − 1) — a unitless return.
The currency and contract unit cancel out, which is exactly the returns model
the backtest itself uses (log(Close/Close.shift(1))). One USD account, any
asset (product decision B, 2026-08-21).

Fills are immediate at this price (no wait for the next bar — the live-paper
convention every serious system uses, e.g. Freqtrade dry-run; paper deliberately
diverges a little from the next-bar-open backtest, which is normal and even
useful as a look-ahead detector). No bid/ask, no slippage — beginner-tier
fidelity (Webull/TradingView do the same); fees are modelled, that is the one
realism worth keeping.
"""
import glob
import importlib.util
import json
import logging
import os
import re
import time

log = logging.getLogger("paper_data")

_SYMBOL_RE = re.compile(r'^\s*SYMBOL\s*=\s*["\']([^"\']+)["\']', re.M)
# module cache: path -> (mtime, loaded module). Strategy files rarely change;
# reloading on every fill/mark would re-exec module-level code needlessly.
_mod_cache = {}

# Price cache: fetch_data is a BACKTEST loader — one call can pull full history
# since 2018 plus several auxiliary datasets, just to read the last Close. The
# account reader marks every position and calls get_equity/get_positions/
# get_holdings each minute (3× snapshot→settle→equity), and a fill's leverage
# check re-prices every held symbol — without a cache that is dozens of
# full-history pulls a minute per venue, which blows account_reader's 60s/venue
# SIGALRM budget and hammers data-api. A short process-level TTL collapses all
# of that to one pull per symbol per window. Stale-by-≤TTL is fine: the price
# IS a bar Close (1m+), not a live tick, and paper never claims tick precision.
_PRICE_TTL_S = 30
_price_cache = {}  # symbol -> (fetched_at, price, strategy_name)


def _norm(sym):
    return str(sym or "").replace("-", "").replace("_", "").upper()


def _hdrs(env):
    return {"api-key": (env or {}).get("blave_api_key", ""),
            "secret-key": (env or {}).get("blave_secret_key", "")}


def _strategy_symbol(name):
    """The symbol a strategy trades — state.json first (what it actually set at
    runtime), else the SYMBOL constant parsed from strategy.py."""
    try:
        with open(f"strategies/{name}/state.json") as f:
            s = json.load(f).get("symbol")
            if s:
                return _norm(s)
    except (OSError, ValueError):
        pass
    try:
        with open(f"strategies/{name}/strategy.py") as f:
            m = _SYMBOL_RE.search(f.read())
            if m:
                return _norm(m.group(1))
    except OSError:
        pass
    return None


def _load_strategy(name):
    """Import strategies/{name}/strategy.py by path WITHOUT running it — run()
    sits under `if __name__ == '__main__'`, so module load only defines
    fetch_data/compute_signals and is side-effect-free. Cached by mtime."""
    path = os.path.abspath(f"strategies/{name}/strategy.py")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _mod_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    spec = importlib.util.spec_from_file_location(f"_paper_strat_{name}", path)
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        log.warning(f"[paper_data] load {name} failed: {type(e).__name__}: {e}")
        return None
    _mod_cache[path] = (mtime, mod)
    return mod


def _resolve_strategy(symbol):
    """Name of a strategy trading `symbol` (normalized match), or None. Prefers
    a strategy that actually has fetch_data (Type A/C); Type B has none.

    When two strategies trade the same symbol from different sources (e.g. one
    adjusted, one raw), the price comes from whichever sorts first — deterministic
    but arbitrary. This is a known limitation of pricing off the strategy rather
    than the order: the order only carries a symbol, not which strategy sized it.
    Same-symbol strategies almost always share a data source, so it rarely bites;
    documented so a future need can thread the strategy through the fill."""
    want = _norm(symbol)
    candidates = []
    for path in sorted(glob.glob("strategies/*/strategy.py")):
        name = os.path.basename(os.path.dirname(path))
        if _strategy_symbol(name) == want:
            candidates.append(name)
    for name in candidates:
        mod = _load_strategy(name)
        if mod and hasattr(mod, "fetch_data"):
            return name
    return None


def _last_close(df):
    """Latest bar Close as a float, tolerant of column casing (backtest uses
    'Close'; be lenient in case a source returns 'close')."""
    if df is None or len(df) == 0:
        raise PaperNoPrice("strategy fetch_data returned no rows")
    row = df.iloc[-1]
    for col in ("Close", "close", "Open", "open"):
        if col in df.columns:
            return float(row[col])
    raise PaperNoPrice(f"no Close/Open column in fetch_data output: {list(df.columns)}")


class PaperNoPrice(Exception):
    """No price could be sourced for a symbol — the paper fill/mark cannot
    proceed and must fail loud (a silent 0 would book fake PnL)."""


def current_price(symbol, env, max_age=_PRICE_TTL_S):
    """Current price for `symbol` from the trading strategy's own data source,
    served from a short process-level cache (see _price_cache). Returns
    (price, strategy_name).

    Type A/C: the strategy's fetch_data, latest Close. Fallback when no bar
    strategy claims the symbol (Type B / manual / not-yet-run): a generic
    1m crypto kline via the same Blave API — best-effort, and it RAISES
    (PaperNoPrice) rather than guessing when even that has no data, so a paper
    fill never books against a made-up price. `max_age=0` forces a fresh pull.
    """
    hit = _price_cache.get(symbol)
    if hit and max_age and (time.time() - hit[0]) < max_age:
        return hit[1], hit[2]
    price, name = _resolve_price(symbol, env)
    _price_cache[symbol] = (time.time(), price, name)
    return price, name


def _resolve_price(symbol, env):
    name = _resolve_strategy(symbol)
    hdrs = _hdrs(env)
    if name:
        mod = _load_strategy(name)
        try:
            df = mod.fetch_data(hdrs)
            return _last_close(df), name
        except PaperNoPrice:
            raise
        except Exception as e:
            raise PaperNoPrice(f"{name}.fetch_data failed for {symbol}: "
                               f"{type(e).__name__}: {e}")
    # No bar strategy owns this symbol (Type B crypto / manual): best-effort
    # generic 1m crypto kline over a short recent window. Crypto-biased by
    # nature — a TW symbol has no such feed, so this RAISES for it rather than
    # guessing, and the caller must fail the fill loud.
    try:
        from datetime import datetime, timedelta, timezone
        from lib.data import fetch_kline
        start = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        df = fetch_kline(_norm(symbol), "1m", start, None, hdrs)
        return _last_close(df), None
    except Exception as e:
        raise PaperNoPrice(f"no strategy trades {symbol} and generic price "
                           f"lookup failed: {type(e).__name__}: {e}")
