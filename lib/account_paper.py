"""
Paper venue account reader — the simulated account behind lib/order_paper.py.

Same contract as lib/account_binance.py (get_equity / get_positions /
get_holdings / get_flows); every number comes from state/paper_ledger.json
marked to Binance public prices. Reading settles resting limit orders and
SL/TP triggers first (order_paper.snapshot), so a machine whose only caller is
the account timer still fills what the book touched since the last read.
No keys involved: PAPER_API_KEY / PAPER_SECRET_KEY are fixed markers the web
writes so the venue pipeline treats paper like any other bound venue.
"""
from lib import order_paper as _paper


def get_equity(env: dict) -> dict:
    """{'equity', 'currency': 'USDT'} — equity = cash + unrealized swap PnL +
    spot inventory at market.

    No `accounts` wallet breakdown: paper is ONE simulated account, so there is
    no spot/funding/futures split to show (that field exists on real venues so
    funds parked outside the trading wallet don't look vanished — moot here). A
    cash/unrealized/spot split would be wrong as a "wallet" list anyway
    (unrealized PnL is not a parked balance), and the web sums every accounts
    value into the displayed equity, so a partial split would misstate it.
    Omitting it makes the web fall back to `equity` — correct, and no
    confusing/untranslated wallet rows."""
    s = _paper.snapshot(env)
    return {"equity": float(s["equity"]), "currency": "USDT"}


def get_positions(env: dict) -> list:
    """[{'symbol', 'side', 'size', 'mark_price'}, ...] — canonical symbols,
    base units, one net row per symbol; [] if flat."""
    return [{"symbol": p["symbol"], "side": p["side"], "size": float(p["size"]),
             "mark_price": float(p["mark_price"])}
            for p in _paper.snapshot(env)["positions"]]


def get_holdings(env: dict) -> list:
    """Display-only: simulated spot coins (wallet 'spot') + the USDT cash pool
    (wallet 'cash'). Unpriceable coins are listed with usdt_value None."""
    s = _paper.snapshot(env)
    rows = []
    for asset, d in s["spot"].items():
        amt = float(d["amount"])
        px = d.get("price")
        rows.append({"asset": asset, "amount": amt,
                     "usdt_value": (amt * px) if px else None, "wallet": "spot"})
    rows.append({"asset": "USDT", "amount": float(s["cash"]),
                 "usdt_value": float(s["cash"]), "wallet": "cash"})
    rows.sort(key=lambda r: -(r["usdt_value"] or 0))
    return rows


def get_flows(env: dict, since: int) -> list:
    """No external flows ever — the seed is not a deposit and there is no
    chain. Empty list, never None (None would read as 'flows unsupported')."""
    return []
