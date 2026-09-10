"""Minimal check for the reconciler's no-venue idle gate — no network, real files.

What it protects (measured 2026-09-09, uid 29026): the web unbound the venue,
the platform's _stop_reconciler() failed to confirm the daemon was stopped, and
this daemon then raised "no officially-supported venue bound" once per 300s
heartbeat for 16 hours — 190 Telegram messages the user could do nothing about
(they were the one who unbound it). The daemon now checks its own precondition
each round instead of relying on being killed from outside.

The gate reads manager/credentials.ui.json, NOT venue_wiring.detect_venue: that
one goes through official_venues(), which skips _NON_AUTO ({sinopac, president,
capital}) — gating on it would idle every 群益期貨 machine forever. Hence the
capital case below; it is the whole reason this file writes real files instead
of mocking the lookup.

Asserts: no manifest → reconcile (fail-open); {"ids": []} → idle; a bound
crypto venue / paper / capital → reconcile; corrupt JSON and a non-list "ids"
→ reconcile (fail-open).

Run: cd blaveclaw-config && python3 tests/check_reconciler_idle_gate.py
"""
import json, os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import manager.reconciler as rec

CASES = [
    (None,                          True,  "no manifest at all (pre-manifest machine)"),
    ({"ids": []},                   False, "unbound: manifest says nothing is bound"),
    ({"ids": ["bybit"]},            True,  "a crypto venue is bound"),
    ({"ids": ["paper"]},            True,  "paper-only machine still reconciles"),
    ({"ids": ["capital"]},          True,  "capital-only machine MUST NOT idle"),
    ({"ids": ["capital", "bybit"]}, True,  "multi-venue"),
    ("{not json",                   True,  "corrupt manifest fails open"),
    ({"ids": "bybit"},              True,  "non-list ids fails open"),
]

cwd = os.getcwd()
with tempfile.TemporaryDirectory() as tmp:
    os.makedirs(os.path.join(tmp, "manager"))
    os.chdir(tmp)  # _ui_bound_ids resolves the manifest relative to cwd
    try:
        for doc, expected, why in CASES:
            path = "manager/credentials.ui.json"
            if doc is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                with open(path, "w") as f:
                    f.write(doc if isinstance(doc, str) else json.dumps(doc))
            got = rec._venue_bound()
            assert got is expected, f"{why}: expected {expected}, got {got}"
            print(f"ok   {'reconcile' if expected else 'idle     '}  {why}")
    finally:
        os.chdir(cwd)

print("\ncheck_reconciler_idle_gate: PASS")
