"""Minimal check for lib/watch.py's strategy_chart (contract §3.3) — no network.

Builds a throwaway workspace with three strategies (one traded, one back-tested with an
empty trades list, one with no stats.json at all, one whose
backtest kept no candles) and asserts: the op file carries
`source.kind = "strategy"` with the default 8×6 grid; door 2 refuses a strategy with no
trades and one with no backtest; everything the strategy already knows (symbol, interval,
venue, block_type) and everything a machine widget takes (cron, script) is refused; and
`strategy=` on any other type is refused.
Run: cd blaveclaw-config && python3 tests/check_watch_strategy_chart.py
"""
import json, os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = tempfile.mkdtemp(prefix="watch-strat-")
os.makedirs(os.path.join(WS, "strategies", "traded"))
os.makedirs(os.path.join(WS, "strategies", "notrades"))
os.environ["BLAVE_AGENT_WORKSPACE"] = WS
sys.path.insert(0, ROOT)

BARS = [[1, 1.0, 2.0, 0.5, 1.5, 10], [2, 1.5, 2.5, 1.0, 2.0, 10]]
with open(os.path.join(WS, "strategies", "traded", "stats.json"), "w") as f:
    json.dump({"symbol": "TXF", "interval": "60m", "candles": BARS,
               "trades": [{"t": 1, "side": "buy"}]}, f)
with open(os.path.join(WS, "strategies", "notrades", "stats.json"), "w") as f:
    json.dump({"symbol": "TXF", "interval": "60m", "candles": BARS, "trades": []}, f)
os.makedirs(os.path.join(WS, "strategies", "nocandles"))
with open(os.path.join(WS, "strategies", "nocandles", "stats.json"), "w") as f:
    json.dump({"symbol": "TXF", "interval": "60m", "candles": [],
               "trades": [{"t": 1, "side": "buy"}]}, f)

from lib import watch

fails = 0


def check(cond, msg):
    global fails
    if not cond:
        fails += 1
        print("FAIL:", msg)


def refused(msg, **kw):
    """add_widget must raise ValueError before anything is written."""
    try:
        watch.add_widget(**kw)
    except ValueError:
        return
    check(False, msg)


path = watch.add_widget("txf-exec", "strategy_chart", "traded · 進出場", strategy="traded")
op = json.load(open(path))
w = op["widget"]
check(w["source"] == {"kind": "strategy", "name": "traded"}, f"source shape: {w['source']}")
check(w["props"] == {}, f"props must stay empty: {w['props']}")
check(w["grid"]["w"] == 8 and w["grid"]["h"] == 6, f"default 8×6: {w['grid']}")

# door 2 (contract §3.3): no trades, and no backtest at all
refused("a strategy with an empty trades list must be refused",
        id="a", type="strategy_chart", title="t", strategy="notrades")
refused("a strategy with no stats.json must be refused",
        id="b", type="strategy_chart", title="t", strategy="missing")
refused("a backtest that kept no candles must be refused (the card would never draw)",
        id="nc", type="strategy_chart", title="t", strategy="nocandles")
refused("an unknown strategy path must be refused",
        id="c", type="strategy_chart", title="t", strategy="../etc")

# what the strategy already knows, and what only a machine widget takes
for kw in ({"symbol": "TXF"}, {"interval": "60m"}, {"venue": "binance"},
           {"block_type": "table"}, {"script": "x"},
           {"refresh_cron": "*/5 * * * *", "refresh_human": "每 5 分鐘"}):
    refused(f"strategy_chart must refuse {sorted(kw)}",
            id="d", type="strategy_chart", title="t", strategy="traded", **kw)

# strategy= belongs to this type only
refused("strategy= on a kline must be refused",
        id="e", type="kline", title="t", symbol="TXF", interval="5m", strategy="traded")
refused("strategy= on a block must be refused",
        id="f", type="block", title="t", block_type="table", script="x",
        refresh_cron="*/5 * * * *", refresh_human="每 5 分鐘", strategy="traded")

# a refused add leaves nothing behind: only the one good op above
ops = [n for n in os.listdir(os.path.join(WS, "watch", "ops")) if n.endswith(".json")]
check(len(ops) == 1, f"refused adds must write nothing: {ops}")

print("FAILED" if fails else "ok")
sys.exit(1 if fails else 0)
