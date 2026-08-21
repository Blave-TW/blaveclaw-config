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
    """{'equity', 'currency': 'USDT', 'accounts'} — equity = cash + unrealized
    swap PnL + spot inventory at market; the breakdown shows where it sits."""
    s = _paper.snapshot(env)
    return {
        "equity": float(s["equity"]),
        "currency": "USDT",
        "accounts": {
            "cash": round(float(s["cash"]), 2),
            "unrealized": round(float(s["unrealized"]), 2),
            "spot": round(float(s["spot_value"]), 2),
        },
    }


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
