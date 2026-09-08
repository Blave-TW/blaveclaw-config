"""Minimal check for the entry lot rounding + below-minimum silence — no network.

Covers the uid 32321 loop (2026-09-08): $289 on BTC perps is 3.7 lots of ~$78, so a
$227 target floored to 0.002 BTC and left a $70 residual that was above reconcile's
$10 THRESHOLD but below one lot — every round re-dispatched an order the venue could
never accept and logged it as "chase: no slices filled".

Asserts: venue_wiring._entry_qty rounds half-up to a whole lot (so the residual is
placeable and the next diff falls inside THRESHOLD), a sub-half-lot diff still rounds
to nothing, a failed rules read leaves qty untouched, BOTH USD->qty conversions
(market + chase limit) actually go through it, execute._finish stays silent on a
below-minimum no-op but still records a real zero-fill, and the orders.jsonl `failed`
flag is judged against the venue's real granularity instead of a flat $10.

Run: cd blaveclaw-config && python3 tests/check_lot_rounding.py
"""
import os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(tempfile.mkdtemp(prefix="lotround-"))

from lib import execute, venue_wiring
import lib.portfolio as portfolio

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += 0 if cond else 1


MARK = 78312.0          # BTCUSDT mark on the day this was measured
LOT_USD = 0.001 * MARK  # one lot ≈ $78.31


# ── a stand-in for lib/order_<venue>.py ────────────────────────────────────
class _FakeOrder:
    """Only what venue_wiring + execute read. `qty` records what the venue was
    actually asked for, in BASE units (every shipped order lib's contract —
    OKX converts base→contracts internally)."""
    def __init__(self, rules=None, raises=False):
        self.qty = None
        self._rules = rules or {"step": 0.001, "min_qty": 0.001,
                                "min_notional": 50.0, "contract_value": 1,
                                "active": True}
        self._raises = raises

    def get_contract_rules(self, env, sym):
        if self._raises:
            raise RuntimeError("exchangeInfo unreachable")
        return self._rules

    def get_mark_price(self, env, sym):
        return MARK

    def get_bbo(self, env, sym):
        return {"bid": MARK, "ask": MARK}

    def place_market_order(self, env, sym, direction, qty, **kw):
        self.qty = qty
        return {"avg_price": MARK, "executed_qty": qty}

    def place_limit_order(self, env, sym, direction, qty, price, **kw):
        self.qty = qty
        return {"order_id": "1", "orderId": "1"}

    def cancel_order(self, env, sym, order_id=None):
        return {"status": "canceled"}

    def get_order(self, env, sym, oid):
        return {"status": "filled", "orig_qty": 0, "executed_qty": 0, "avg_price": 0}


fake = _FakeOrder()
# Bind the whole venue layer to the fake ONCE: both venue_wiring's own
# importlib call and execute._venue_min_slice_usd's separate one resolve
# through sys.modules, so nothing below is stubbed past the seam under test.
sys.modules["lib.order_binance"] = fake
venue_wiring.read_env = lambda path=".env": {"BINANCE_API_KEY": "k"}
venue_wiring.detect_venue = lambda env: "binance"


# ── venue_wiring._entry_qty ────────────────────────────────────────────────
def entry(order, usd):
    return venue_wiring._entry_qty(order, {}, "BTCUSDT", abs(usd) / MARK)


# the residual that used to loop forever: 0.000897 BTC floors to 0, rounds to one lot
check(abs(entry(fake, 70.25) - 0.001) < 1e-12,
      "$70.25 residual (0.9 lot) rounds UP to one lot instead of flooring to zero")
# What happens to the leftover is NOT this file's claim to make: rounding leaves
# up to half a lot ($39 here), which no flat threshold converges — the entry-side
# gate does (manager/reconciler._symbol_threshold, asserted behaviourally in
# tests/check_reconcile_threshold.py).

check(entry(fake, 30.0) == 0.0,
      "$30 (under half a lot) still rounds to nothing — no trade, no order")
check(abs(entry(fake, 227.30) - 0.003) < 1e-12, "$227.30 target rounds to 3 lots")
check(abs(entry(fake, LOT_USD * 2.5) - 0.003) < 1e-12,
      "an exact .5 tie rounds UP (round-half-up, not banker's rounding)")

# OKX-style: sz steps are in CONTRACTS, so the lot is step x contract_value
okx = _FakeOrder({"step": 0.1, "min_qty": 0.001, "min_notional": 0.0,
                  "contract_value": 0.01, "active": True})
check(abs(entry(okx, 70.25) - 0.001) < 1e-12,
      "contract-sized venues round on step x contract_value, not on step alone")

untouched = 70.25 / MARK
check(entry(_FakeOrder(raises=True), 70.25) == untouched,
      "a failed rules read returns qty untouched (order lib floors as before)")


# ── the wiring: both USD->qty conversions must go through _entry_qty ───────
fake.qty = None
venue_wiring.auto_place_order("BTCUSDT", 70.25)
check(fake.qty is not None and abs(fake.qty - 0.001) < 1e-12,
      "auto_place_order (市價) sizes the $70.25 residual at one whole lot")

fake.qty = None
venue_wiring.auto_limit_toolkit("BTCUSDT")["place"](70.25, MARK, None, True)
check(fake.qty is not None and abs(fake.qty - 0.001) < 1e-12,
      "auto_limit_toolkit (限價追價) sizes the $70.25 residual at one whole lot")

fake.qty = None
venue_wiring.auto_place_order("BTCUSDT", 30.0)
check(fake.qty == 0.0, "a sub-half-lot diff reaches the venue as 0 (skipped there)")


# ── the granularity _finish judges against — NOT stubbed ───────────────────
# If this lookup silently degrades to the $20 floor, the 08:20 case below would
# still be marked failed and nobody would notice; assert the real number.
min_slice = execute._venue_min_slice_usd("BTCUSDT")
check(abs(min_slice - LOT_USD * 1.05) < 0.01,
      f"_venue_min_slice_usd resolves to lot scale (${min_slice:.2f}), not the $20 floor")


# ── execute._finish ────────────────────────────────────────────────────────
logged, errors = [], []
portfolio._append_reconciler_log = lambda e: logged.append(e)
portfolio._record_order_error = lambda s, x, e: errors.append((s, str(e)))


def finish(**kw):
    logged.clear(), errors.clear()
    args = {"symbol": "BTCUSDT", "signed_diff": 70.25, "asset_spec": None,
            "reduce_only": False, "exchange": "binance", "contributors": [],
            "style": "chase", "filled_usd": 0.0, "vwap": None, "aborted": False}
    args.update(kw)
    execute._finish(**args)


finish(filled_usd=0.0, below_min=True)
check(errors == [] and logged == [],
      "below-minimum no-op records NO order_error (the 32321 symptom)")

finish(filled_usd=0.0, below_min=False)
check(len(errors) == 1 and "no slices filled" in errors[0][1],
      "a real zero-fill (window expired, nothing rested) still records an error")

# uid 32321's 08:20:38 fill: a $227.30 leg that filled 0.002 BTC ($156.64) —
# everything the venue would take. Residual $70.66 is over the flat $10 (old:
# marked failed) but under one lot (new: complete).
finish(signed_diff=227.30, filled_usd=156.64, vwap=MARK)
check(len(logged) == 1 and "failed" not in logged[0],
      "a leg that filled everything the venue would take is NOT marked failed")

finish(signed_diff=500.0, filled_usd=100.0, vwap=MARK)
check(len(logged) == 1 and logged[0].get("failed") is True,
      "a genuine partial ($400 short, ~5 lots) is still marked failed")

finish(filled_usd=LOT_USD, vwap=MARK, aborted=True)
check(len(logged) == 1 and logged[0].get("failed") is True,
      "an aborted run is still marked failed regardless of the residual")

print(("FAILED " + str(fails)) if fails else "all ok")
sys.exit(1 if fails else 0)
