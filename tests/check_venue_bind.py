"""Minimal check for lib/venue.py — no network. Builds a fake <base>/{workspace,current}
with the monorepo runtime's command_listener, binds okx over a bingx-bound .env (no
credentials.ui.json yet, like a pre-manifest machine) and asserts: exact OKX lines
written 0600, BLAVE keys kept, bingx evicted + halted + reported, manifest = ["okx"],
summary carries names only; bad inputs are refused before the runtime is touched.
Run: cd blaveclaw-config && .venv/bin/python tests/check_venue_bind.py
"""
import os, shutil, stat, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(ROOT, "..", "api", "blave_agent", "runtime", "command_listener.py")
if not os.path.isfile(RUNTIME):
    sys.exit("needs the monorepo layout (../api/blave_agent/runtime)")
BASE = tempfile.mkdtemp(prefix="venue-")
WS = os.path.join(BASE, "workspace")
os.makedirs(os.path.join(WS, "manager"))
open(os.path.join(WS, "manager", "portfolio_config.json"), "w").write("{}")
os.makedirs(os.path.join(BASE, "current"))
shutil.copy(RUNTIME, os.path.join(BASE, "current"))
os.environ["BLAVE_AGENT_WORKSPACE"] = WS
sys.path.insert(0, ROOT)
os.chdir(WS)
from lib import venue

ENV = os.path.join(WS, ".env")
with open(ENV, "w") as f:
    f.write("blave_api_key=bk\nblave_secret_key=bs\n# mine\nBINGX_API_KEY=old1\nBINGX_SECRET_KEY=old2\n")

fails = 0
def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += 0 if cond else 1

def refused(vid, env, why):
    try:
        venue.bind(vid, env)
    except ValueError as e:
        check(not any(v in str(e) for v in env.values() if isinstance(v, str)),
              f"refused {why}: no value in error")
        return
    check(False, f"refused {why}")

K1, S1, P1 = "KV-api-7c1e9f3a2b4d", "KV-sec-0d8e6a5f1c2b", "KV-pass-3f9a1e7c5d2b"  # gitleaks:allow (fake test values)
K2, S2, P2 = "KV-api-2b4d7c1e9f3a", "KV-sec-1c2b0d8e6a5f", "KV-pass-5d2b3f9a1e7c"  # gitleaks:allow (fake test values)
KEYS = {"OKX_API_KEY": K1, "OKX_SECRET_KEY": S1, "OKX_PASSPHRASE": P1}
refused("capital", KEYS, "capital (own onboarding doc)")
refused("paper", KEYS, "paper (no keys)")
refused("kraken", KEYS, "unknown venue")
refused("okx", {k: v for k, v in KEYS.items() if k != "OKX_PASSPHRASE"}, "okx without passphrase")
refused("okx", dict(KEYS, BINANCE_API_KEY="KV-foreign-8e2c4a6f"), "foreign key name in payload")
refused("okx", dict(KEYS, OKX_SECRET_KEY=S1[:5] + " " + S1[5:]), "whitespace in value")
refused("okx", dict(KEYS, OKX_SECRET_KEY=S1 + "é"), "non-ASCII value")
with open(ENV) as f:
    check("BINGX_API_KEY=old1" in f.read(), ".env untouched by refusals")

out = venue.bind("OKX", {"okx_api_key": K1, "OKX_SECRET_KEY": S1, "OKX_PASSPHRASE": P1})
check(out == {"venue": "okx", "written": ["OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"],
              "bound": ["okx"], "evicted": ["bingx"]}, f"summary names only, eviction reported without manifest: {out}")
with open(ENV) as f:
    lines = f.read().splitlines()
check(f"OKX_API_KEY={K1}" in lines and f"OKX_SECRET_KEY={S1}" in lines and f"OKX_PASSPHRASE={P1}" in lines,
      "okx lines written")
check("blave_api_key=bk" in lines and "blave_secret_key=bs" in lines and "# mine" in lines,
      "blave keys + user comment kept")
check(not any(l.startswith("BINGX_") for l in lines), "bingx pair evicted")
check(stat.S_IMODE(os.stat(ENV).st_mode) == 0o600, ".env is 0600")
with open(os.path.join(WS, "manager", "credentials.ui.json")) as f:
    manifest = f.read()
check('"okx"' in manifest and "bingx" not in manifest, "manifest = okx only")
check(os.path.isfile(os.path.join(WS, "state", "HALT")), "rebind halted the evicted venue")

out2 = venue.bind("okx", {"OKX_API_KEY": K2, "OKX_SECRET_KEY": S2, "OKX_PASSPHRASE": P2})
with open(ENV) as f:
    body = f.read()
check(out2["evicted"] == [] and f"OKX_API_KEY={K2}" in body and K1 not in body,
      "same-venue rebind replaces, evicts nothing")

shutil.rmtree(BASE)
print("FAILED" if fails else "ALL OK")
sys.exit(1 if fails else 0)
