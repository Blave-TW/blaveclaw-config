"""One-time: write the self-ledger's baseline (manager/ledger_seed.json).
Run once when turning portfolio_config.json["self_ledger"] on — with the flag
on and NO baseline, the reconciler refuses to trade (fail-loud guard in
lib/portfolio.reconcile). Full onboarding recipe: references/manager.md §
self_ledger.

Two modes:

  python3 manager/seed_ledger.py
      Fresh start (DEFAULT — the right mode for a user who holds manual
      positions): everything currently on the account is the USER's; the bot
      starts from a zero book and only manages what it buys from now on.
      REQUIREMENT: the bot's own positions must be flat first — any live bot
      position becomes the user's, and the bot would re-buy its full target
      on top of it (doubled exposure).

  python3 manager/seed_ledger.py --absorb
      The account's current position is 100% BOT-owned as of now. Only for an
      account with NO manual positions mixed in (migrating a bot-only machine
      without closing anything). On a mixed account this adopts the user's
      manual positions into the bot's book and the bot will later trade them
      away — when unsure, do not use this mode.

Re-running (either mode) REPLACES the baseline and forgets every fill
recorded before now — never run this routinely or on a schedule.
"""
import argparse
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.portfolio import seed_ledger

# Loaded by file path (not `import manager.reconciler`) so this script works
# whether or not manager/ is an importable package — same technique lib/execute.py
# uses to load a custom executor module.
_spec = importlib.util.spec_from_file_location(
    "reconciler", str(Path(__file__).parent / "reconciler.py"))
_reconciler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_reconciler)

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--absorb', action='store_true',
                    help="adopt the account's CURRENT positions as bot-owned "
                         "(bot-only accounts ONLY — see module docstring)")
    args = ap.parse_args()

    if args.absorb:
        print("Reading current account position (absorb mode)...")
        seeded = seed_ledger(_reconciler.get_positions, absorb=True)
        if not seeded:
            print("Account is flat — baseline seeded empty (same as fresh start).")
        else:
            print(f"⚠️  Adopted {len(seeded)} position(s) as BOT-OWNED:")
            for symbol, size in seeded.items():
                print(f"  {symbol}: {size:+.4f}")
            print("If ANY of these are the user's own manual positions, this "
                  "baseline is WRONG — re-run without --absorb after flattening "
                  "the bot's own positions.")
    else:
        # fresh start does not need the account read at all — but read it
        # anyway to warn about live positions the user must be aware of
        try:
            live = _reconciler.get_positions() or {}
        except Exception as e:
            print(f"note: could not read account positions ({e}) — seeding anyway")
            live = {}
        seeded = seed_ledger(_reconciler.get_positions, absorb=False)
        print("Fresh-start baseline seeded: the bot's book is ZERO; everything "
              "currently on the account is treated as the user's.")
        if live:
            print(f"⚠️  The account currently holds {len(live)} open position(s):")
            for symbol, pos in live.items():
                print(f"  {symbol}: {pos.get('side')} {pos.get('size')}")
            print("These are now ALL treated as the user's. If any belong to the "
                  "bot's strategies, the bot will re-buy its full target ON TOP "
                  "of them (doubled exposure) — flatten the bot's positions "
                  "first, or use --absorb only if NOTHING here is manual.")

    print("\nNow set portfolio_config.json[\"self_ledger\"] = true (if not "
          "already) — the reconciler picks both up on its next poll.")
