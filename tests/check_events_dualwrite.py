"""Minimal check for the config-side dual-write into state/events.jsonl.

What it protects: the ten P1／P2 sender sites must ALSO land an event, and must
never let that landing break the caller — a notification path that raises would
take the order or the strategy down with it, which is strictly worse than the
missing notification it was trying to report.

Also pins the two types config must NEVER write: `halt` and `order_error` are
produced by the platform diffing the report payload; writing them here too
means one fact lands twice and P1 pages twice.

Asserts: lib.events.emit is a no-op (returns None, no raise) when the runtime
module is absent; it drops None-valued fields; a raising appender is swallowed;
and every wired call site passes a type from the agreed registry.

Run: cd blaveclaw-config && python3 tests/check_events_dualwrite.py
"""
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import lib.events as ev

# 平台登記表裡機器側可以寫的型別（openclaw/agent_events.py）——這裡是白名單，
# 打錯字的型別平台會直接丟掉，而且丟得很安靜。
ALLOWED = {
    "execution_interrupted", "execution_fallback_market", "execution_stuck",
    "strategy_failed", "bar_stale", "scheduler_error", "venue_unbound",
    "ui_override",
}
# 平台自己 diff payload 產生的，config 寫了就是同一件事落兩筆
FORBIDDEN = {"halt", "order_error"}

# ── 1. runtime 模組不在（舊機）→ no-op，不 raise ──────────────────────────
ev._appender = None
ev._unavailable_logged = False
os.environ["BLAVE_AGENT_BASE"] = os.path.join(ROOT, "tests", "_no_such_base")
assert ev.emit("strategy_failed", strategy="x") is None, "舊機必須 no-op"
assert ev.emit("ui_override") is None

# ── 2. 有 appender：None 欄位要被丟掉，型別與 payload 原樣傳下去 ──────────
seen = []
ev._appender = lambda t, p: (seen.append((t, p)), 123)[1]
assert ev.emit("bar_stale", strategy="s", symbol=None, minutes=7) == 123
assert seen == [("bar_stale", {"strategy": "s", "minutes": 7})], seen

# ── 3. appender 爆炸不能傳染給呼叫端 ─────────────────────────────────────
def _boom(t, p):
    raise RuntimeError("disk full")
ev._appender = _boom
assert ev.emit("ui_override") is None, "append 失敗必須吞掉,不能炸掉下單/策略"

# ── 4. 每個接線點用的型別都在白名單裡，而且沒人寫 halt / order_error ─────
WIRED = ["lib/execute.py", "lib/portfolio.py", "lib/venue_wiring.py",
         "manager/alert_failure.py", "manager/wait_for_bar.py"]
found = set()
for path in WIRED:
    src = open(path, encoding="utf-8").read()
    for m in re.finditer(r'emit\(\s*"([a-z_]+)"', src):
        t = m.group(1)
        assert t not in FORBIDDEN, f"{path}: 不可以寫 {t}（平台自己會產生）"
        assert t in ALLOWED, f"{path}: {t} 不在平台登記表裡"
        found.add(t)
missing = ALLOWED - found
assert not missing, f"這些型別沒有任何接線點: {sorted(missing)}"

print(f"check_events_dualwrite: OK（{len(found)} 個型別、{len(WIRED)} 個檔）")
