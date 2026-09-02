"""Minimal check for lib/report_templates.py — no network, no api.
Builds every template from synthetic frames and asserts the structural rules the
api enforces (references/reports.md §6): meta first, lead right after meta, one
footnote last, known block types, finite numbers, narrative caps.
Run: cd blaveclaw-config && .venv/bin/python tests/check_report_templates.py
"""
import json, math, os, sys, tempfile
os.environ["BLAVE_AGENT_WORKSPACE"] = tempfile.mkdtemp(prefix="rpt-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
from lib import data as d
import lib.report_templates as T

KNOWN = {"meta", "kpi_row", "line_chart", "drawdown", "heatmap", "bar_chart", "histogram", "box", "scatter",
         "metric_table", "table", "text", "quote", "footnote", "code", "divider", "callout", "image"}
days = pd.bdate_range("2026-06-01", "2026-09-01"); n = len(days); rng = np.random.default_rng(1)
walk = lambda base, vol: pd.Series(base * np.cumprod(1 + rng.normal(0, vol, n)), index=days)
d.fetch_twmarket_index = lambda s, e, h: pd.DataFrame({c: walk(45000, .01) for c in ("Open", "High", "Low", "Close")})
d.fetch_twmarket_turnover = lambda s, e, h: pd.DataFrame({"volume": walk(8e9, .1), "value": walk(9e11, .15), "trades": walk(2e6, .1)})
d.fetch_twmarket_institutional = lambda s, e, h: pd.DataFrame({"foreign": rng.normal(0, 2e10, n), "investment_trust": rng.normal(0, 5e9, n), "dealer": rng.normal(0, 5e9, n)}, index=days).assign(total=lambda x: x.sum(axis=1))
d.fetch_twmarket_margin = lambda s, e, h: pd.DataFrame({"margin_balance": walk(8.8e6, .005), "margin_balance_prev": walk(8.8e6, .005), "margin_balance_value": walk(3e11, .005), "short_balance": walk(3e5, .01), "short_balance_prev": walk(3e5, .01)})
d.fetch_twfutures_institutional = lambda fid, s, e, h: pd.DataFrame({"foreign_net_oi": rng.normal(-70000, 3000, n).round()}, index=days)
hours = pd.date_range("2026-08-27 00:00", "2026-09-02 05:00", freq="60min", tz="Asia/Taipei")
hours = hours[((hours.hour >= 8) & (hours.hour < 14)) | (hours.hour >= 15) | (hours.hour <= 5)]
d.fetch_twfutures_ohlcv = lambda sym, sch, s, e, h: pd.DataFrame({"Open": 46800., "High": 46900., "Low": 46700., "Close": 46800 + rng.normal(0, 50, len(hours)), "Volume": 100}, index=hours.tz_convert("UTC"))
d.fetch_economic_calendar = lambda h, **k: pd.DataFrame([{"date": "2026-09-02", "time": "20:30", "country": "US", "country_name": "美國", "subject": "非農就業", "subject_title": "<8月>", "predict": 150, "last": 142, "real": None, "unit": "千人", "priority": 1}])
udays = pd.date_range("2026-08-01", "2026-09-02", freq="D", tz="UTC")
kl = lambda base: pd.DataFrame({"Open": base, "High": base, "Low": base, "Close": base * np.cumprod(1 + rng.normal(0, .03, len(udays))), "Volume": 1.0}, index=udays)
d.fetch_kline_batch = lambda syms, i, s, e, h: {x: kl(70000) for x in syms}
d.fetch_kline = lambda sym, i, s, e, h: kl(70000)
alpha = lambda scale: (lambda *a, **k: pd.DataFrame({"alpha": rng.normal(0, scale, len(udays) - 1)}, index=udays[:-1]))
for fn in ("fetch_funding_rate", "fetch_market_direction", "fetch_capital_shortage", "fetch_top_trader_exposure", "fetch_liquidation", "fetch_whale_hunter", "fetch_taker_intensity"):
    setattr(d, fn, alpha(0.01 if fn == "fetch_funding_rate" else 1.0))
tw = pd.DataFrame({c: walk(900, .02) for c in ("Open", "High", "Low", "Close")}).assign(Volume=walk(30000, .3)); tw.index = tw.index.tz_localize("Asia/Taipei")
d.fetch_twstock_ohlcv = lambda sid, sch, h, start=None, end=None, adjust=False: tw
d.fetch_twstock_institutional = lambda sid, s, e, h: pd.DataFrame({"foreign_net": rng.normal(0, 5e6, n)}, index=days)

H = {"api-key": "x", "secret-key": "y"}
NAR = {"lead": "一句可證偽的主張。", "read": "判讀。", "action": "操作。", "risk": "推翻條件。"}
fails = 0
def check(cond, msg):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + msg); fails += (not cond)

def walk_numbers(o):
    if isinstance(o, float): yield o
    elif isinstance(o, dict):
        for v in o.values(): yield from walk_numbers(v)
    elif isinstance(o, list):
        for v in o: yield from walk_numbers(v)

for name, pack in (("tw", T.tw_market_brief("2026-09-02", H)), ("crypto", T.crypto_market_brief("2026-09-02", H)),
                   ("2330", T.symbol_brief("2330", "2026-09-02", H)), ("btc", T.symbol_brief("BTC", "2026-09-02", H))):
    for nar in (NAR, None):
        path = T.publish(pack, nar, report_id=pack.report_id + ("" if nar else "-sched"))
        doc = json.load(open(path)); b = doc["blocks"]; types = [x["type"] for x in b]
        tag = f"{name}{'+narrative' if nar else ' data-only'}"
        check(types[0] == "meta" and types.count("meta") == 1, f"{tag}: meta 唯一且第一")
        check(types[-1] == "footnote" and types.count("footnote") == 1, f"{tag}: footnote 唯一且最後")
        check(set(types) <= KNOWN, f"{tag}: 只用契約有的 block 型別")
        leads = [i for i, x in enumerate(b) if x.get("variant") == "lead"]
        check(leads == ([1] if nar else []), f"{tag}: lead 只在 meta 之後(或無)")
        check(all(math.isfinite(v) for v in walk_numbers(doc)), f"{tag}: 數字全部有限")
        check(b[0].get("origin") == ("chat" if nar else "scheduled"), f"{tag}: origin={'chat' if nar else 'scheduled'}")
        check(all(x.get("type") == "kpi_row" and 1 <= len(x["items"]) <= 6 for x in b if x["type"] == "kpi_row"), f"{tag}: kpi_row 1–6 格")
        check(all(sum(s["role"] == "primary" for s in x["series"]) <= 1 for x in b if x["type"] == "line_chart"), f"{tag}: line_chart 最多一條 primary")
    check(pack.context and "narrative slots" in pack.describe(), f"{name}: describe() 列出數字與槽位")
try:
    T.kpi_row([T.kpi(str(i), "1") for i in range(7)]); check(False, "kpi_row 超過 6 格要 raise,不靜默砍")
except ValueError:
    check(True, "kpi_row 超過 6 格要 raise,不靜默砍")
check(len([b for b in json.load(open(T.publish(T.tw_market_brief("2026-09-02", H), None, report_id="tw-k")))["blocks"] if b["type"] == "kpi_row"][0]["items"]) == 6
      and any(i["label"] == "台指期夜盤" for i in json.load(open(os.path.join(os.environ["BLAVE_AGENT_WORKSPACE"], "reports", "tw-k.json")))["blocks"][1]["items"]),
      "台股晨報六格 KPI 含台指期夜盤")
for bad, why in (({"lead": "x" * 601}, "超過字數上限"), ({"summary": "x"}, "未知槽位"), ({"read": "見 [^nope]"}, "不存在的註腳引用")):
    try:
        T.publish(pack, bad); check(False, f"publish 拒絕{why}")
    except ValueError:
        check(True, f"publish 拒絕{why}")
print("all checks passed" if not fails else f"FAILED: {fails}"); sys.exit(1 if fails else 0)
