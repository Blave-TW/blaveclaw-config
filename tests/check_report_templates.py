"""Minimal check for lib/report_templates.py — no network, no api.
Builds every template from synthetic frames and asserts the structural rules the
api enforces (references/reports.md §6): meta first, lead right after meta, one
footnote last, known block types, finite numbers, narrative caps, price charts as
candlesticks (schema 1.2) and everything else as line charts.
Run: cd blaveclaw-config && .venv/bin/python tests/check_report_templates.py
"""
import json, math, os, re, sys, tempfile
os.environ["BLAVE_AGENT_WORKSPACE"] = tempfile.mkdtemp(prefix="rpt-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
from lib import data as d
import lib.report_templates as T
_REAL_NOW_TPE = T._now_tpe
# 夜盤是否已收看「現在」;釘住時鐘,不讓結果跟著跑測試的時刻變。預設在合成資料的夜盤收完之後。
T._now_tpe = lambda: pd.Timestamp("2026-09-02 09:00", tz="Asia/Taipei").to_pydatetime()

KNOWN = {"meta", "kpi_row", "line_chart", "candlestick", "drawdown", "heatmap", "bar_chart", "histogram", "box",
         "scatter", "metric_table", "table", "text", "quote", "footnote", "code", "divider", "callout", "image"}
days = pd.bdate_range("2026-06-01", "2026-09-01"); n = len(days); rng = np.random.default_rng(1)
walk = lambda base, vol: pd.Series(base * np.cumprod(1 + rng.normal(0, vol, n)), index=days)
def ohlc(close):
    """Coherent bars around a close series: open = previous close, wicks outside both."""
    o = close.shift(1).fillna(close.iloc[0]); wick = 1 + np.abs(rng.normal(0, .003, len(close)))
    return pd.DataFrame({"Open": o, "High": np.maximum(o, close) * wick, "Low": np.minimum(o, close) / wick, "Close": close})
d.fetch_twmarket_index = lambda s, e, h: ohlc(walk(45000, .01))
d.fetch_twmarket_turnover = lambda s, e, h: pd.DataFrame({"volume": walk(8e9, .1), "value": walk(9e11, .15), "trades": walk(2e6, .1)})
d.fetch_twmarket_institutional = lambda s, e, h: pd.DataFrame({"foreign": rng.normal(0, 2e10, n), "investment_trust": rng.normal(0, 5e9, n), "dealer": rng.normal(0, 5e9, n)}, index=days).assign(total=lambda x: x.sum(axis=1))
d.fetch_twmarket_margin = lambda s, e, h: pd.DataFrame({"margin_balance": walk(8.8e6, .005), "margin_balance_prev": walk(8.8e6, .005), "margin_balance_value": walk(3e11, .005), "short_balance": walk(3e5, .01), "short_balance_prev": walk(3e5, .01)})
d.fetch_twfutures_institutional = lambda fid, s, e, h: pd.DataFrame({"foreign_net_oi": rng.normal(-70000, 3000, n).round()}, index=days)
hours = pd.date_range("2026-08-27 00:00", "2026-09-02 05:00", freq="60min", tz="Asia/Taipei")
hours = hours[((hours.hour >= 8) & (hours.hour < 14)) | (hours.hour >= 15) | (hours.hour <= 5)]
d.fetch_twfutures_ohlcv = lambda sym, sch, s, e, h: pd.DataFrame({"Open": 46800., "High": 46900., "Low": 46700., "Close": 46800 + rng.normal(0, 50, len(hours)), "Volume": 100}, index=hours.tz_convert("UTC"))
d.fetch_economic_calendar = lambda h, **k: pd.DataFrame([{"date": "2026-09-02", "time": "20:30", "country": "US", "country_name": "美國", "subject": "非農就業", "subject_title": "<8月>", "predict": 150, "last": 142, "real": None, "unit": "千人", "priority": 1}])
udays = pd.date_range("2026-06-01", "2026-09-02", freq="D", tz="UTC")   # ≈ the 90-day crypto window
kl = lambda base: ohlc(pd.Series(base * np.cumprod(1 + rng.normal(0, .03, len(udays))), index=udays)).assign(Volume=1.0)
d.fetch_kline_batch = lambda syms, i, s, e, h: {x: kl(70000) for x in syms}
d.fetch_kline = lambda sym, i, s, e, h: kl(70000)
alpha = lambda scale: (lambda *a, **k: pd.DataFrame({"alpha": rng.normal(0, scale, len(udays) - 1)}, index=udays[:-1]))
for fn in ("fetch_funding_rate", "fetch_market_direction", "fetch_capital_shortage", "fetch_top_trader_exposure", "fetch_liquidation", "fetch_whale_hunter", "fetch_taker_intensity"):
    setattr(d, fn, alpha(0.01 if fn == "fetch_funding_rate" else 1.0))
tw = ohlc(walk(900, .02)).assign(Volume=walk(30000, .3)); tw.index = tw.index.tz_localize("Asia/Taipei")
d.fetch_twstock_ohlcv = lambda sid, sch, h, start=None, end=None, adjust=False: tw
d.fetch_twstock_institutional = lambda sid, s, e, h: pd.DataFrame({"foreign_net": rng.normal(0, 5e6, n)}, index=days)
HOL = pd.DataFrame({"date": pd.to_datetime(["2026-09-02"]), "name": ["測試休市"], "type": ["holiday"], "note": [None]})
HOL_SRC = "臺灣證券交易所 2026 年有價證券集中交易市場開（休）市日期（測試出處全文）；https://data.gov.tw/license"
HOL.attrs = {"source_zh": HOL_SRC, "source": "Taiwan Stock Exchange, 2026 (test)"}
d.fetch_twstock_holidays = lambda h, year=None: HOL

H = {"api-key": "x", "secret-key": "y"}
NAR = {"lead": "一句可證偽的主張。", "read": "判讀。", "watch": "觀察條件。", "risk": "推翻條件。"}
# name → (title of the one price chart, or None; line_chart titles that must stay line charts)
PRICE = {"tw": ("加權指數", {"融資餘額", "外資期貨淨多單"}), "close": ("加權指數", {"融資餘額", "外資期貨淨多單"}),
         "crypto": (None, {"BTC 資金費率", "Blave 市場指標(z-score)"}),
         "2330": ("2330 日 K", set()), "btc": ("BTC 日 K", {"資金費率", "Blave 指標(z-score)"})}
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

def sound(k):
    return all(c[3] <= min(c[1], c[4]) and max(c[1], c[4]) <= c[2] for c in k["candles"]) and \
        all(a[0] < z[0] and isinstance(a[0], int) for a, z in zip(k["candles"], k["candles"][1:]))

for name, pack in (("tw", T.tw_market_brief("2026-09-02", H)), ("close", T.tw_close_brief("2026-09-01", H)),
                   ("crypto", T.crypto_market_brief("2026-09-02", H)),
                   ("2330", T.symbol_brief("2330", "2026-09-02", H)), ("btc", T.symbol_brief("BTC", "2026-09-02", H))):
    for nar in (NAR, None):
        path = T.publish(pack, nar)
        check(os.path.basename(path) == pack.report_id + ("" if nar else "-auto") + ".json", f"{name}: {'有判讀' if nar else '純數據包'} id = {os.path.basename(path)[:-5]}")
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
        price, lines = PRICE[name]
        ks = [x for x in b if x["type"] == "candlestick"]
        if price:
            n_k = len(ks[0]["candles"]) if ks else 0
            check(len(ks) == 1 and ks[0]["title"] == price and n_k == 60, f"{tag}: 價格圖「{price}」是 K 線,{n_k} 根(範本統一 60 根,日 K 建議 40–65)")
            check(all(sound(k) and {"y_unit", "reflines"} <= set(k) for k in ks), f"{tag}: K 線高低包住開收、t 嚴格遞增,帶單位與 20 日參考線")
            check(all(not r["emphasis"] for k in ks for r in k.get("reflines", [])), f"{tag}: 參考線都不強調(強調低點讀起來像標支撐)")
            check(doc["schema_version"] == "1.2", f"{tag}: 含 K 線 → schema_version 1.2")
            ref = {r["label"]: r["y"] for r in (ks[0].get("reflines", []) if ks else [])}
            prior = ks[0]["candles"][-21:-1] if ks else []   # 倒數第 2–21 根,不含當日
            want = {"前 20 日高": max(k[2] for k in prior), "前 20 日低": min(k[3] for k in prior)} if prior else {}
            if name in ("tw", "close"):
                want.pop("前 20 日低", None)   # 大盤晨報、收盤報告只畫前 20 日高
            check(bool(want) and ref == want,
                  f"{tag}: 參考線恰為 {sorted(want)},值 = 倒數第 2–21 根的最高價/最低價")
            levels = {r["level"]: r["price"] for x in b if x["type"] == "table" and x.get("title") == "近期高低與均線" for r in x["rows"]}
            check(bool(want) and all(f"{k} {v:,.2f}" in pack.describe() for k, v in want.items())
                  and (name in ("tw", "close") or all(levels.get(k) == f"{v:,.2f}" for k, v in want.items())),
                  f"{tag}: 參考線、近期高低與均線表、describe() 的前 20 日高/低同名同值")
        else:
            check(not ks and doc["schema_version"] == "1.1", f"{tag}: 沒有 K 線 → 維持 1.1")
        check(not re.search(r"(?<!前 )20 日[高低]", json.dumps(doc, ensure_ascii=False) + pack.describe()),
              f"{tag}: 沒有不帶「前」的 20 日高/低標籤(同名不同口徑)")
        body = json.dumps(doc, ensure_ascii=False) + pack.describe()
        check(not re.search("操作建議|支撐|壓力|關鍵價位", body.replace("非支撐壓力", "")),
              f"{tag}: 範本不自帶操作建議/支撐壓力/關鍵價位字眼")
        check(("## 觀察重點" in body) == bool(nar), f"{tag}: watch 槽標題為「觀察重點」")
        titles = {x.get("title") for x in b if x["type"] == "line_chart"}
        check(lines <= titles and not any("收盤" in (t or "") for t in titles), f"{tag}: 非價格圖仍是 line_chart,沒有收盤折線")
    check(pack.context and "narrative slots" in pack.describe(), f"{name}: describe() 列出數字與槽位")

d.fetch_twstock_ohlcv = lambda sid, sch, h, start=None, end=None, adjust=False: tw.tail(15)
p = T.symbol_brief("2330", "2026-09-02", H)
kb = [x for x in p.blocks if x["type"] == "candlestick"]
check(len(kb) == 1 and "reflines" not in kb[0] and any("不足 21 根" in x for x in p.notes)
      and "前 20 日" not in "".join(p.context.values())
      and all(not r["level"].startswith("前 20 日") for x in p.blocks if x["type"] == "table" for r in x["rows"]),
      "日 K 不足 21 根:不畫前 20 日高/低、記 notes,不拿較短的窗口頂替")
d.fetch_twstock_ohlcv = lambda sid, sch, h, start=None, end=None, adjust=False: tw

idx = pd.date_range("2026-01-01", periods=4, freq="D", tz="UTC")
bad = pd.DataFrame({"Open": [10., 10., 10., 10.], "High": [11., 9.5, 11., 11.], "Low": [9., 9., 9., 9.], "Close": [10.5, 9.2, np.nan, 10.2]}, index=idx)
ck = T.candlestick("x", bad)
check(ck is not None and [k[0] for k in ck["candles"]] == [T._ts(idx[0]), T._ts(idx[3])], "candlestick 丟掉 NaN 與開盤高於最高的那根(lib.data 只擋 high<low)")
check(T.candlestick("x", bad.iloc[:2]) is None, "candlestick 剩不到 2 根 → None(不產一張畫不出來的圖)")
long = ohlc(pd.Series(100 * np.cumprod(1 + rng.normal(0, .01, 150)), index=pd.date_range("2026-01-01", periods=150, freq="D", tz="UTC")))
ck = T.candlestick("x", long)
check(len(ck["candles"]) == 120 and ck["candles"][-1][0] == T._ts(long.index[-1]), "candlestick 超過 120 根只留最後 120 根")
try:
    T.kpi_row([T.kpi(str(i), "1") for i in range(7)]); check(False, "kpi_row 超過 6 格要 raise,不靜默砍")
except ValueError:
    check(True, "kpi_row 超過 6 格要 raise,不靜默砍")
check(len([b for b in json.load(open(T.publish(T.tw_market_brief("2026-09-02", H), None, report_id="tw-k")))["blocks"] if b["type"] == "kpi_row"][0]["items"]) == 6
      and any(i["label"] == "台指期夜盤" for i in json.load(open(os.path.join(os.environ["BLAVE_AGENT_WORKSPACE"], "reports", "tw-k.json")))["blocks"][1]["items"]),
      "台股晨報六格 KPI 含台指期夜盤")
p = T.tw_market_brief("2026-09-02", H)
check(p.context["台指期夜盤"].split("(")[1].startswith("收盤;") and "最新價" not in json.dumps(p.blocks, ensure_ascii=False),
      "夜盤已收(05:00 後、最後一根在):describe 寫收盤,footnote 不帶盤中說明")
full_night = d.fetch_twfutures_ohlcv

def night_case(last_bar, now, label, why):
    """夜盤資料只到 last_bar(bar 起始時間)、現在是 now:KPI label 恰為 label,describe 同一句,不寫收盤,footnote 講最新價。"""
    bars = hours[hours <= pd.Timestamp(last_bar, tz="Asia/Taipei")]
    d.fetch_twfutures_ohlcv = lambda sym, sch, s, e, h: pd.DataFrame({"Open": 46800., "High": 46900., "Low": 46700., "Close": 46800., "Volume": 100}, index=bars.tz_convert("UTC"))
    T._now_tpe = lambda: pd.Timestamp(now, tz="Asia/Taipei").to_pydatetime()
    p = T.tw_market_brief("2026-09-02", H)
    items = [x for x in p.blocks if x["type"] == "kpi_row"][0]["items"]
    state = label[len("台指期夜盤("):-1]
    foot = json.dumps([x for x in p.blocks if x["type"] == "footnote"], ensure_ascii=False)
    check(any(i["label"] == label for i in items) and p.context["台指期夜盤"].split("(")[1].startswith(state + ";")
          and "尚未收盤" not in foot and "不是收盤價" in foot, why)

night_case("2026-09-01 21:00", "2026-09-01 22:30", "台指期夜盤(盤中,截至 22:00)", "夜盤盤中、api 已丟未收那根:標「盤中,截至 22:00」")
night_case("2026-09-01 22:00", "2026-09-01 22:30", "台指期夜盤(盤中,截至 22:30)", "夜盤盤中、export 留著未收那根:時點取現在「截至 22:30」")
night_case("2026-09-02 02:00", "2026-09-02 09:00", "台指期夜盤(截至 03:00,資料未含收盤)", "05:00 後資料缺尾:不標收盤,label 與 footnote 不自相矛盾")
d.fetch_twfutures_ohlcv = full_night
T._now_tpe = lambda: pd.Timestamp("2026-09-02 09:00", tz="Asia/Taipei").to_pydatetime()

# ── 台股收盤報告:id / 夜盤 / 不發佈 / 法人未出 ──
REPORTS = os.path.join(os.environ["BLAVE_AGENT_WORKSPACE"], "reports")
def unwritten(rid):
    return not any(os.path.exists(os.path.join(REPORTS, rid + s + ".json")) for s in ("", "-auto"))
def skipped(p, why, *must):
    ok = bool(p.skip) and not p.blocks and all(m in p.skip for m in must) and p.skip in p.notes and "不發佈" in p.describe()
    check(ok and T.publish(p, NAR) is None and T.publish(p) is None and unwritten(p.report_id), why)

p = T.tw_close_brief("2026-09-01", H)
check(p.report_id == "tw-close-20260901" and p.title == "台股收盤報告" and p.type == "morning" and p.skip is None,
      "收盤報告:id tw-close-YYYYMMDD、標題台股收盤報告、type morning")
check("夜盤" not in json.dumps(p.blocks, ensure_ascii=False) + p.describe(), "收盤報告不含夜盤")
p = T.tw_close_brief("2026-09-02", H)
skipped(p, "休市表列為休市:不發佈,notes 講休市名稱、上一交易日,並附休市表出處全文", "測試休市", "上一交易日 2026-09-01", HOL_SRC)
check(p.context.get("休市表出處") == HOL_SRC and HOL_SRC in p.describe(), "休市表出處全文進 context 與 describe()(授權條件)")
d.fetch_twstock_holidays = lambda h, year=None: (_ for _ in ()).throw(AssertionError("週末不該查休市表"))
skipped(T.tw_close_brief("2026-09-05", H), "週六:不查休市表就不發佈,指名上一交易日", "週末", "2026-09-01")
d.fetch_twstock_holidays = lambda h, year=None: HOL
skipped(T.tw_close_brief("2026-09-03", H), "交易日但指數還沒有當日收盤(未入庫或臨時停市):不拿舊收盤充當今日", "尚未入庫", "2026-09-01")
d.fetch_twstock_holidays = lambda h, year=None: None
skipped(T.tw_close_brief("2026-09-02", H), "休市表拿不到、當日無收盤:不發佈", "尚未入庫")
p = T.tw_close_brief("2026-09-01", H)
check(p.skip is None and any("休市表無法取得" in x for x in p.notes) and os.path.exists(T.publish(p, NAR)),
      "休市表拿不到但當日有收盤:照發,notes 說明改用收盤資料判斷")
d.fetch_twstock_holidays = lambda h, year=None: HOL

full = {k: getattr(d, k) for k in ("fetch_twmarket_institutional", "fetch_twmarket_margin", "fetch_twfutures_institutional", "fetch_twmarket_turnover")}
d.fetch_twmarket_institutional = lambda s, e, h: full["fetch_twmarket_institutional"](s, e, h).iloc[:-1]
d.fetch_twmarket_margin = lambda s, e, h: full["fetch_twmarket_margin"](s, e, h).iloc[:-1]
d.fetch_twfutures_institutional = lambda fid, s, e, h: full["fetch_twfutures_institutional"](fid, s, e, h).iloc[:-1]
p = T.tw_close_brief("2026-09-01", H)
labels = [i["label"] for x in p.blocks if x["type"] == "kpi_row" for i in x["items"]]
body = json.dumps([x for x in p.blocks if x["type"] != "footnote"], ensure_ascii=False)   # 註腳的來源說明本來就列這些名稱
check(p.skip is None and labels == ["加權指數", "成交值"] and "三大法人" not in body and "融資餘額" not in body
      and "外資期貨淨多單" not in body and "08/31" not in body and "08-31" not in body
      and all(any(x.startswith(f"{k} 2026-09-01 尚未公布(資料源最新為 2026-08-31)") for x in p.notes) for k in ("三大法人", "融資餘額", "外資期貨淨多單")),
      "法人、融資、期貨法人當日未出:notes 寫尚未公布,區塊與 KPI 都不放前一日的數字")
for k, fn in full.items():
    setattr(d, k, fn)

p = T.tw_close_brief("2026-9-1", H)
check(p.report_id == "tw-close-20260901" and p.skip is None, "「2026-9-1」正規化成 2026-09-01:照發,不被逐字比對誤判成未入庫")
p = T.tw_close_brief(pd.Timestamp("2026-08-31 17:00", tz="UTC"), H)
check(p.report_id == "tw-close-20260901" and p.skip is None, "帶時區的 UTC 時間先換成台北日期(UTC 8/31 17:00 = 台北 9/1)")
for bad in ("9/1", "2026/09/01", "20260901"):
    try:
        T.tw_close_brief(bad, H); check(False, f"日期「{bad}」要明確拒絕,不能靜默 skip")
    except ValueError:
        check(True, f"日期「{bad}」明確拒絕(ValueError),不靜默 skip")

# 台北日期:機器時鐘是 UTC、時間落在 UTC 午夜前,台北已是隔天。改成 UTC 或拿掉 +8 都會拿到 9/11。
from datetime import datetime as _RealDT, timezone as _tz
_UTC_NOW = _RealDT(2026, 9, 11, 17, 30, tzinfo=_tz.utc)   # = 台北 2026-09-12 01:30
class _ClockDT(_RealDT):
    @classmethod
    def now(cls, tz=None):
        return _UTC_NOW.astimezone(tz) if tz else _UTC_NOW.replace(tzinfo=None)
    @classmethod
    def utcnow(cls):
        return _UTC_NOW.replace(tzinfo=None)
T.datetime, T._now_tpe = _ClockDT, _REAL_NOW_TPE
p = T.tw_close_brief(headers=H)
check(T._today_tpe() == "2026-09-12" and p.report_id == "tw-close-20260912",
      "預設日期取台北(UTC 9/11 17:30 → tw-close-20260912),不是機器的 UTC 日期")
T.datetime = _RealDT
T._now_tpe = lambda: pd.Timestamp("2026-09-02 09:00", tz="Asia/Taipei").to_pydatetime()
for bad, why in (({"lead": "x" * 601}, "超過字數上限"), ({"summary": "x"}, "未知槽位"), ({"action": "x"}, "舊的 action 槽位"),
                 ({"read": "見 [^nope]"}, "不存在的註腳引用")):
    try:
        T.publish(pack, bad); check(False, f"publish 拒絕{why}")
    except ValueError:
        check(True, f"publish 拒絕{why}")
print("all checks passed" if not fails else f"FAILED: {fails}"); sys.exit(1 if fails else 0)
