"""Minimal check for the ENTRY gate recorded into the reconcile snapshot — no network.

What it protects: the silent case. On a coarse instrument a gap under one lot
places nothing and raises nothing — the workspace showed "目標 100 / 實際 78 /
差 22" with no order and no error, and no way to say why. compute_diff now
writes the gate it actually used into manager/last_reconcile.json["gates"], so
the page can name the number instead of guessing it (it is not recomputable
off-machine: venue minimum × mark, read with the user's keys).

Asserts: a diff stuck between the flat 10 and the venue gate places nothing yet
IS recorded with its gate and its signed diff; a diff over the gate trades and
is recorded all the same (the form prompt needs the number either way); a
flat-gate symbol (spot) trades and stays OUT; a reduce leg is recorded with its
own (half-lot) gate and `side: "reduce"` — entry rows carry no `side`, their
shape is unchanged; EVERY recorded row carries both sides (`entry_usd` /
`reduce_usd`, same values whichever side the round took) because the page
colours a live diff that may have flipped sign since; a row whose reduce side
is the flat 10 is recorded anyway when its entry side is not; lot-based rows
stay out; the key is present-but-empty when nothing qualifies.

Run: cd blaveclaw-config && python3 tests/check_reconcile_gates.py
"""
import json, os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(tempfile.mkdtemp(prefix="recongates-"))
os.makedirs("manager", exist_ok=True)
open("manager/portfolio_config.json", "w").write("{}")

from lib import portfolio, venue_wiring  # noqa: E402
from manager import reconciler  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += 0 if cond else 1


SYM = "BTCUSDT"          # coarse: one lot is worth far more than the flat 10
SPOT = "BTCUSDT@spot"    # spot minimums sit under the floor → the flat gate
MARK = 78312.0
LOT_USD = 0.001 * MARK   # 78.31
GATE = LOT_USD * 1.05    # 82.23 — venue minimum plus the stale-mark buffer
RGATE = LOT_USD * 0.5    # 39.16 — the reduce side: half a lot


class _FakeOrder:
    """Stands in for lib/order_<venue>.py: only the two reads are exercised."""
    def __init__(self):
        self.mark = MARK
        self.rules = {"step": 0.001, "min_qty": 0.001, "min_notional": 5.0,
                      "contract_value": 1}

    def get_contract_rules(self, env, sym):
        return self.rules

    def get_mark_price(self, env, sym):
        return self.mark


fake = _FakeOrder()
sys.modules["lib.order_binance"] = fake
venue_wiring.read_env = lambda path=".env": {"BINANCE_API_KEY": "k"}
venue_wiring.detect_venue = lambda env: "binance"
portfolio._record_order_error = lambda s, x, e: None

placed = {}


def run(rows):
    """One reconcile round over {symbol: (target_usd, held_usd, asset_spec,
    exchange)} — returns (legs that reached place_order_fn, snapshot gates)."""
    reconciler._min_order_gate.clear()
    placed.clear()
    target, actual = {}, {}
    for symbol, row in rows.items():
        t_usd, a_usd = row[0], row[1]
        spec = row[2] if len(row) > 2 else None
        exchange = row[3] if len(row) > 3 else None
        if t_usd is not None:
            target[symbol] = {"side": "long" if t_usd >= 0 else "short",
                              "size": abs(t_usd), "exchange": exchange,
                              "asset_spec": spec, "contributors": []}
        actual[symbol] = {"side": "long" if a_usd >= 0 else "short",
                          "size": abs(a_usd)}
    portfolio.aggregate_portfolio = lambda: target

    def place(symbol, signed_diff, spec, **kw):
        placed.setdefault(symbol, []).append(round(signed_diff, 2))

    portfolio.reconcile(get_positions_fn=lambda: actual, place_order_fn=place,
                        threshold=reconciler._symbol_threshold)
    with open("manager/last_reconcile.json") as f:
        return dict(placed), json.load(f).get("gates")


def near(a, b):
    return a is not None and abs(a - b) < 0.01


# ① the silent case: 10 < |diff| < gate. Nothing placed, and the number that
#    stopped it is on record.
legs, gates = run({SYM: (LOT_USD * 3, LOT_USD * 2.5)})
check(legs == {}, "a half-lot buy-back places nothing (unchanged)")
check(near((gates.get(SYM) or {}).get("usd"), GATE)
      and near((gates.get(SYM) or {}).get("diff"), LOT_USD * 0.5),
      f"...and is recorded: gate ${GATE:.2f}, diff ${LOT_USD * 0.5:.2f}")
check("side" not in gates[SYM], "...with no 'side' — the entry row's shape is unchanged")
# Both sides, on an entry row: the page colours the LIVE diff, whose sign can
# have flipped since this round — with only the side that round used it would
# colour a buy-back green against the reduce gate (39) it stored last time.
check(near((gates.get(SYM) or {}).get("entry_usd"), GATE)
      and near((gates.get(SYM) or {}).get("reduce_usd"), RGATE),
      f"...and carries BOTH gates: entry ${GATE:.2f} / reduce ${RGATE:.2f}")

# ② recorded whether or not it trades — the form prompt needs the same number
#    on a symbol that is currently converging fine.
legs, gates = run({SYM: (LOT_USD * 3, LOT_USD * 1.9)})
check(legs == {SYM: [86.14]}, "an entry over the gate still trades")
check(near((gates.get(SYM) or {}).get("usd"), GATE)
      and near((gates.get(SYM) or {}).get("diff"), LOT_USD * 1.1),
      "...and is recorded too — the gate is not a 'was blocked' flag")

# ③ a flat-gate symbol has nothing to explain: it must not appear at all, or
#    the page would print a gate line on every position.
legs, gates = run({SPOT: (60, 0)})
check(legs == {SPOT: [60.0]}, "a spot entry over the flat 10 trades")
check(gates == {}, "a flat-gate (spot) symbol is not recorded, and the key is "
                   "present-but-empty rather than missing")

# ④ reduce legs are gated at half a lot, so an over-target under that is the
#    same silent case in the other direction — recorded, and told apart by side
legs, gates = run({SYM: (LOT_USD * 2, LOT_USD * 2.4)})
check(legs == {}, "a 0.4-lot shrink places nothing")
check(near((gates.get(SYM) or {}).get("usd"), RGATE)
      and near((gates.get(SYM) or {}).get("diff"), -LOT_USD * 0.4)
      and gates[SYM].get("side") == "reduce",
      f"...and is recorded as a reduce gate: ${RGATE:.2f}, diff ${-LOT_USD * 0.4:.2f}")
check(near((gates.get(SYM) or {}).get("entry_usd"), GATE)
      and near((gates.get(SYM) or {}).get("reduce_usd"), RGATE),
      "...carrying the same two gates as the entry row — a symbol's two "
      "thresholds do not depend on which side this round took")
legs, gates = run({SYM: (LOT_USD * 2, LOT_USD * 2.6)})
check(legs == {SYM: [round(-LOT_USD * 0.6, 2)]} and gates[SYM].get("side") == "reduce",
      "a 0.6-lot shrink places, and is recorded as a reduce gate all the same")

# ④b a row whose REDUCE side is the flat 10 while its entry side is not (a $15
#     lot: entry 15.75, reduce 10) is still recorded — the round the diff flips
#     sign is exactly when the page needs that 15.75, and it has no other
#     source for it.
fake.mark = 15000.0
legs, gates = run({SYM: (15.0 * 2, 15.0 * 2.5)})
check(legs == {} and near((gates.get(SYM) or {}).get("usd"), 10)
      and near((gates.get(SYM) or {}).get("entry_usd"), 15.75)
      and near((gates.get(SYM) or {}).get("reduce_usd"), 10)
      and (gates.get(SYM) or {}).get("side") == "reduce",
      "a reduce row sitting on the flat 10 is recorded for its entry side's sake")
fake.mark = MARK

# ⑤ lot-based rows never resolve an account-currency gate (they skip the
#    threshold entirely), so they must not leak into gates either
legs, gates = run({"TXFR1": (1, 0, {"type": "futures_contracts"}, "capital")})
check(legs == {"TXFR1": [1.0]}, "a 1-lot capital diff still places")
check(gates == {}, "a lot-based row is not recorded")

# ⑥ two symbols, one round: the entry-side one is explained, the other is not
legs, gates = run({SYM: (LOT_USD * 3, LOT_USD * 2.5), SPOT: (60, 0)})
check(legs == {SPOT: [60.0]} and list(gates) == [SYM],
      "in a mixed round only the venue-gated entry is recorded")

print("\n" + ("PASS" if not fails else f"{fails} FAILED"))
sys.exit(1 if fails else 0)
