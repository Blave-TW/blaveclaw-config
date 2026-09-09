"""Minimal check for the per-symbol ENTRY gate — no network.

What it protects (measured in review 2026-09-08, after 8fbecec shipped entry
rounding): on a coarse instrument an entry rounds half-up to a whole lot and
lands the position up to half a lot OVER target; the reduce leg that follows
ceils to a whole lot and sells it back; the next entry buys it again. Against a
flat gate of 10 that is a real buy/sell round trip every heartbeat — one BTC
perp lot is ~$78, so every leg clears 10 easily. Gating ENTRIES at the venue's
own minimum converges the leftover instead.

Reduce legs get HALF a lot (measured 2026-09-09, uid 32321: the entry gate
alone left the other direction open — a $10 over-target was ceil-sold as a
whole $79 lot and bought straight back, 442 fills). Never a whole lot: they
ceil and cap at the position, so a position of exactly one lot has to stay
closable and flippable — and that holds on a stale mark too.

Asserts: a one-lot position closes and flips out, incl. against a ±2%-stale
gate; sub-lot buy-backs (half a lot, and a drifted 0.6 lot) place nothing; a
shrink under half a lot places nothing while one over it does, and the sell it
ceils to converges the next round; an entry over the gate still trades;
spot / fine-grained instruments / a failed lookup keep the flat 10 on the
reduce side; a failed LOT read alone costs the reduce side only and leaves the
entry gate standing; capital/lot rows pass untouched with zero venue lookups;
the gate is cached per round and expires.

Run: cd blaveclaw-config && python3 tests/check_reconcile_threshold.py
"""
import inspect, os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(tempfile.mkdtemp(prefix="recongate-"))
os.makedirs("manager", exist_ok=True)
open("manager/portfolio_config.json", "w").write("{}")

from lib import portfolio, venue_wiring  # noqa: E402
from manager import reconciler  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += 0 if cond else 1


SYM = "BTCUSDT"         # coarse on purpose: one lot ($78) is what breaks a flat gate
MARK = 78312.0
LOT_USD = 0.001 * MARK  # 78.31
GATE = LOT_USD * 1.05   # 82.23 — the venue minimum plus the stale-mark buffer
RGATE = LOT_USD * 0.5   # 39.16 — the reduce side: half a lot, no buffer


class _FakeOrder:
    """Stands in for lib/order_<venue>.py: only the two reads are exercised."""
    def __init__(self, raises=False):
        self.raises, self.rule_calls, self.mark = raises, 0, MARK
        self.rules = {"step": 0.001, "min_qty": 0.001, "min_notional": 5.0,
                      "contract_value": 1}

    def get_contract_rules(self, env, sym):
        self.rule_calls += 1
        if self.raises:
            raise RuntimeError("exchangeInfo unreachable")
        return self.rules

    def get_mark_price(self, env, sym):
        return self.mark


fake = _FakeOrder()
# Bind the venue layer once: _venue_min_slice_usd resolves the order lib through
# sys.modules, so nothing below is stubbed past the seam under test.
sys.modules["lib.order_binance"] = fake
venue_wiring.read_env = lambda path=".env": {"BINANCE_API_KEY": "k"}
venue_wiring.detect_venue = lambda env: "binance"


def gate(symbol=SYM, reduce_only=False):
    reconciler._min_order_gate.clear()
    return reconciler._symbol_threshold(symbol, reduce_only)


# ── the gate itself ────────────────────────────────────────────────────────
check(abs(gate() - GATE) < 0.01,
      f"entry gate follows the venue minimum (${gate():.2f}), not the flat $10")
check(abs(gate(reduce_only=True) - RGATE) < 0.01,
      f"reduce gate is half a lot (${gate(reduce_only=True):.2f}) — under one "
      "lot, over the flat 10")
check(gate(SYM + "@spot") == gate(SYM + "@spot", reduce_only=True) == 10
      == inspect.signature(portfolio.spot_scope).parameters["threshold"].default,
      "a spot symbol's gate stays at 10 on both sides, matching spot_scope's default")
fake.mark = 4000.0   # an ETH-like lot: $4
check(gate(reduce_only=True) == reconciler.THRESHOLD,
      "a fine-grained instrument (lot $4) keeps the flat 10 on the reduce side")
fake.mark = MARK

reconciler._min_order_gate.clear()
fake.rule_calls = 0
reconciler._symbol_threshold(SYM), reconciler._symbol_threshold(SYM)
check(fake.rule_calls == 1,
      "the venue is asked once per symbol per round, not once per gate check")

reconciler._min_order_gate.clear()
fake.rule_calls, _ttl = 0, reconciler.MIN_ORDER_TTL_S
reconciler.MIN_ORDER_TTL_S = 0
reconciler._symbol_threshold(SYM), reconciler._symbol_threshold(SYM)
reconciler.MIN_ORDER_TTL_S = _ttl
check(fake.rule_calls == 2,
      "an expired entry is re-read — the lot is static, its USD value is not")

fake.raises = True
check(gate() == gate(reduce_only=True) == reconciler.THRESHOLD,
      "an unreadable venue degrades to the flat 10 on both sides (the behaviour "
      "before this)")
fake.raises = False

# the LOT is read out of a second rules dialect (step, else qty_precision) —
# rules that parse for the minimum can still fail there. That failure must cost
# the reduce side only: sharing one try/except with the entry gate would drop a
# gate we already computed correctly back to the flat 10 and reopen the churn.
_rules = fake.rules
fake.rules = {"qty_precision": "n/a", "min_qty": 0.001, "min_notional": 5.0,
              "contract_value": 1}   # int("n/a") raises inside _lot_base only
check(abs(gate() - GATE) < 0.01 and gate(reduce_only=True) == reconciler.THRESHOLD,
      f"an unreadable LOT keeps the entry gate (${GATE:.2f}) and drops only the "
      "reduce side to the flat 10")
fake.rules = _rules


# ── whole rounds: what actually reaches place_order_fn ─────────────────────
placed, errors = [], []
portfolio._record_order_error = lambda s, x, e: errors.append((s, str(e)))


def run(target_usd, held_usd, asset_spec=None, exchange=None, threshold=None,
        keep_gate=False):
    """One reconcile round against a stubbed target/position — returns the legs
    that reached place_order_fn, signed. keep_gate leaves an already-cached gate
    in place, which is what a real round does inside MIN_ORDER_TTL_S."""
    if not keep_gate:
        reconciler._min_order_gate.clear()
    placed.clear(), errors.clear()
    target = {} if target_usd is None else {
        SYM: {"side": "long" if target_usd >= 0 else "short",
              "size": abs(target_usd), "exchange": exchange,
              "asset_spec": asset_spec, "contributors": []}}
    portfolio.aggregate_portfolio = lambda: target

    def place(symbol, signed_diff, spec, **kw):
        placed.append(round(signed_diff, 2))

    portfolio.reconcile(
        get_positions_fn=lambda: {SYM: {"side": "long" if held_usd >= 0 else "short",
                                        "size": abs(held_usd)}},
        place_order_fn=place,
        threshold=reconciler._symbol_threshold if threshold is None else threshold,
    )
    return list(placed)


# ① a position of exactly one lot must still be closable — the whole reason the
#    gate is entry-only. With it on both sides, -78.31 < 82.23 and the position
#    sat there forever while the reconciler printed "Converged" every round.
check(run(None, LOT_USD) == [-78.31],
      "a one-lot position still closes (strategy removed → target gone)")
check(run(0, LOT_USD) == [-78.31], "a one-lot position still closes (target 0)")

# ② and must still flip. The CLOSE leg always fires; the new leg is an entry
#    like any other, so a sub-lot target is not opened — deliberate: _entry_qty
#    would round a 1-lot request up to a whole lot, i.e. take ~2x the requested
#    size on a half-lot target. Flat is the safe end state, stuck-long is not.
check(run(-LOT_USD, LOT_USD) == [-78.31],
      "a one-lot position still flips out (close leg fires, sub-lot short not opened)")
check(run(-LOT_USD * 2, LOT_USD) == [-78.31, -156.62],
      "a flip into a target over the gate places BOTH legs")
check(run(LOT_USD * 0.6, 0) == [],
      "a sub-lot target is never opened — rounding it up would take ~2x the request")

# ③ the churn this exists to stop: after a ceil-sell leaves the position short
#    of target, the sub-lot buy-back must not be re-dispatched.
check(run(LOT_USD * 3, LOT_USD * 2.5) == [],
      "a half-lot buy-back ($39) places nothing — no buy/sell round trip")
check(run(LOT_USD * 3, LOT_USD * 2.4) == [],
      "a drifted 0.6-lot buy-back ($47) is also converged (why the gate is at "
      "one lot, not at the half-lot rounding boundary)")
check(run(LOT_USD * 3, LOT_USD * 1.9) == [86.14],
      "an entry over the gate ($86 > $82.23) still trades")
check(run(LOT_USD * 3, LOT_USD * 2.5, threshold=10) == [39.16],
      "with the old flat 10 that same buy-back is dispatched (the churn)")

# the 1.05 buffer, pinned: the gate carries a mark up to MIN_ORDER_TTL_S old
# while the gap is priced now. A ceil-sell leaves 0.99 lots short; a 2% rise
# inside the cache window puts that gap over a 1.0x gate, _entry_qty rounds it
# to a whole lot, the next reduce ceils it off — churn. At 1.05 it stays shut.
reconciler._min_order_gate.clear()
fake.mark = MARK * 0.98                 # the gate resolves on a 2%-stale mark
stale_gate = reconciler._symbol_threshold(SYM)
fake.mark = MARK                        # ...the gap below is priced now
shortfall = LOT_USD * 0.99
check(stale_gate < shortfall * 1.05 and run(LOT_USD * 3, LOT_USD * 3 - shortfall,
                                            keep_gate=True) == [],
      f"a 0.99-lot shortfall (${shortfall:.2f}) against a 2%-stale gate "
      f"(${stale_gate:.2f}) is still blocked — at 1.0x it would pass")

# ⑤ the other direction of the churn (2026-09-09): an over-target under half a
#    lot is left alone — the ceil-sell would take a whole lot, and that sell
#    was the loss whether or not anything bought it back.
check(run(LOT_USD * 3, LOT_USD * 3.13) == [],
      "3 lots drifted $10 over a 3-lot target places nothing (the 442-fill event)")
check(run(LOT_USD * 2, LOT_USD * 2.4) == [],
      "a 0.4-lot SHRINK places nothing")
check(run(LOT_USD * 2, LOT_USD * 2.6) == [round(-LOT_USD * 0.6, 2)],
      "a 0.6-lot SHRINK still places (reduce gate is half a lot, not one)")
# two steps and done: 1.5 lots over → placed → _reduce_qty ceils it to two
# whole lots → 0.5 lot short, which is under the entry gate → converged.
check(run(LOT_USD * 3, LOT_USD * 4.5) == [round(-LOT_USD * 1.5, 2)]
      and run(LOT_USD * 3, LOT_USD * 2.5) == [],
      "a 1.5-lot over-target sells, and the half-lot gap it leaves is not bought back")

# ⑥ the reduce gate carries a stale mark too. Half a lot leaves 50% headroom
#    under a one-lot close — a whole-lot gate would shut it on any uptick.
for drift in (1.02, 0.98):
    reconciler._min_order_gate.clear()
    fake.mark = MARK * drift
    stale_rgate = reconciler._symbol_threshold(SYM, reduce_only=True)
    fake.mark = MARK
    check(run(0, LOT_USD, keep_gate=True) == [-78.31]
          and run(-LOT_USD, LOT_USD, keep_gate=True) == [-78.31],
          f"a one-lot position still closes and flips against a {drift:.2f}x-stale "
          f"reduce gate (${stale_rgate:.2f})")
check(errors == [], "none of the above recorded an order_error")

# ⑦ and the same, end to end: with no lot to be read the entry gate still holds
#    the sub-lot buy-back shut (on a shared try it would be the flat 10 and the
#    $39 buy would go out).
_rules = fake.rules
fake.rules = {"qty_precision": "n/a", "min_qty": 0.001, "min_notional": 5.0,
              "contract_value": 1}
check(run(LOT_USD * 3, LOT_USD * 2.5) == [],
      "a half-lot buy-back is still blocked when only the LOT read fails")
fake.rules = _rules


# ④ capital / lot rows: untouched, and never trigger a venue lookup
fake.rule_calls = 0
check(run(1, 0, asset_spec={"type": "futures_contracts"}, exchange="capital") == [1.0],
      "a 1-lot capital diff still places (account-currency gate does not apply)")
check(fake.rule_calls == 0,
      "a lot-based row never asks the venue for an account-currency minimum")

print("\n" + ("PASS" if not fails else f"{fails} FAILED"))
sys.exit(1 if fails else 0)
