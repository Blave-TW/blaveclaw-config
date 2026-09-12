"""
Report templates — the deterministic half of a report, built from `lib.data`.

A template returns a `Pack`: the data blocks (KPI row, charts, tables, footnote)
already in contract shape, plus the numbers behind them (`pack.context`) and the
narrative slots left for you to fill (`pack.slots`). You write the judgement —
lead / read / watch / risk — and `publish()` assembles and drops the report.
You never build a chart block by hand for these report types, and you never
recompute a number the pack already carries.

    from lib.report_templates import tw_market_brief, publish

    pack = tw_market_brief()              # today's TW market data pack
    print(pack.describe())                # the numbers, one line each — cite these
    publish(pack, narrative={
        "lead":   "...one falsifiable claim...",
        "read":   "...what the numbers say and why...",
        "watch":  "...which conditions / indicators to watch, at what thresholds...",
        "risk":   "...the indicator threshold that would prove the lead wrong...",
    })

    publish(pack)                          # no narrative = data pack only, id gets "-auto"
                                           # (a scheduled run: no LLM, no invented view)

Templates: `tw_market_brief()`, `crypto_market_brief()`, `symbol_brief(symbol)`.
Block shapes follow `references/reports.md` §3; the narrative rules are §7 (one
claim in the lead, every number a cause or a comparison, write the other side).
The pack never invents a value: a series the source does not have is a block
that is not there, and `describe()` says so.
"""

import math
import os
import re
from datetime import datetime, timedelta, timezone

import pandas as pd

from lib import data as _data
from lib.report import write_report

TPE = timezone(timedelta(hours=8))
_FNREF_RE = re.compile(r"\[\^([A-Za-z0-9_-]{1,32})\]")

# Narrative slots: key → (markdown heading, char cap). Caps are generous for a
# judgement and tight for filler — a lead is one claim, not a summary.
SLOTS = {
    "lead": ("", 600),
    "read": ("## 判讀", 2400),
    # 不叫「操作建議」:對不特定人給支撐壓力、買賣價位是投顧法規點名的態樣,這格只寫條件與門檻。
    "watch": ("## 觀察重點", 1500),
    "risk": ("推翻這份解讀的訊號", 900),
}


class Pack:
    """What a template hands back. `blocks` are contract-shaped and complete;
    `slots` lists the narrative you may add; `context` holds the figures
    (label → display string) that `describe()` prints for you to cite."""

    def __init__(self, report_id, title, type_, report_type, blocks, context, notes=None,
                 meta=None):
        self.report_id = report_id
        self.title = title
        self.type = type_
        self.report_type = report_type
        self.blocks = blocks
        self.context = context
        self.notes = notes or []          # what is missing and why
        self.meta = meta or {}
        self.slots = dict(SLOTS)

    def describe(self):
        lines = [f"[{self.report_id}] {self.title}"]
        lines += [f"  {k}: {v}" for k, v in self.context.items()]
        if self.notes:
            lines += ["  缺少:"] + [f"    - {n}" for n in self.notes]
        lines.append("  narrative slots: " + ", ".join(f"{k}≤{cap}" for k, (_, cap) in self.slots.items()))
        return "\n".join(lines)


# ─── headers ──────────────────────────────────────────────────────────────────

def headers_from_env():
    """Auth headers from the workspace `.env` (or the environment). Same shape as
    references/lib.md: lowercase keys, `api-key` / `secret-key` header names."""
    env = dict(os.environ)
    try:
        from dotenv import dotenv_values
        env.update({k: v for k, v in dotenv_values().items() if v is not None})
    except ImportError:
        pass
    return {"api-key": env.get("blave_api_key", ""), "secret-key": env.get("blave_secret_key", "")}


# ─── small block constructors (shape by construction) ─────────────────────────

def _finite(v):
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _ts(idx):
    """DatetimeIndex entry → unix seconds int (UTC)."""
    t = pd.Timestamp(idx)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return int(t.timestamp())


def _points(series, scale=1.0):
    return [[_ts(t), float(v) * scale] for t, v in series.items() if _finite(v)]


def kpi(label, value, tone="neutral", unit=None, delta=None):
    item = {"label": label[:40], "value": value, "tone": tone}
    if unit:
        item["unit"] = unit[:16]
    if delta is not None:
        item["delta"] = delta
    return item


def kpi_row(items, title=None):
    # 契約 1–6 格。超過就 raise,不靜默砍:砍掉的那格 describe() 還在列,agent 會引用一個
    # 讀者看不到的數字。
    if not 1 <= len(items) <= 6:
        raise ValueError(f"kpi_row takes 1–6 items, got {len(items)}")
    b = {"type": "kpi_row", "items": list(items)}
    if title:
        b["title"] = title[:80]
    return b


def line_chart(title, series, y_unit=None, caption=None, reflines=None):
    """series: list of (name, role, pandas Series). Drops NaN points; a series with
    no finite point is dropped; returns None when nothing survives."""
    out = []
    for name, role, s in series:
        pts = _points(s)
        if pts:
            out.append({"name": name[:40], "role": role, "points": pts[-5000:]})
    if not out:
        return None
    b = {"type": "line_chart", "title": title[:80], "series": out[:4]}
    if y_unit:
        b["y_unit"] = y_unit[:8]
    if caption:
        b["caption"] = caption[:300]
    if reflines:
        b["reflines"] = [{"y": float(y), "label": lab[:32], "emphasis": bool(em)}
                         for y, lab, em in reflines if _finite(y)][:4]
    return b


# 範本的價格 K 線一律畫最後 60 根:手機寬度約放得下 68 根完整 K 棒(日 K 建議 40–65)。
_PRICE_BARS = 60


def _clean_ohlc(df):
    """The bars a candlestick can draw: sorted, de-duplicated, no NaN, open/close inside
    high/low. Other columns (Volume) ride along on the kept rows."""
    # lib.data 只丟 high<low 的壞 K;開收落在高低之外的那根也會讓 api 整份 400,丟掉留一個缺口。
    # 範本畫圖與算價位共用這一份,參考線才不會算到圖上沒畫的那根。
    df = df[~df.index.duplicated(keep="last")].sort_index()
    o, h, l, c = (df[k].astype(float) for k in ("Open", "High", "Low", "Close"))
    ok = pd.concat([o, h, l, c], axis=1).notna().all(axis=1) & (l <= o.combine(c, min)) & (o.combine(c, max) <= h)
    return df[ok]


def candlestick(title, df, y_unit=None, caption=None, reflines=None):
    """df: Open/High/Low/Close on a DatetimeIndex. Keeps the last ≤120 drawable bars;
    returns None when fewer than 2 survive."""
    ohlc = _clean_ohlc(df)[["Open", "High", "Low", "Close"]].astype(float)
    candles = [[_ts(t), *map(float, row)] for t, row in zip(ohlc.index, ohlc.values)][-120:]
    if len(candles) < 2:
        return None
    b = {"type": "candlestick", "title": title[:80], "candles": candles}
    if y_unit:
        b["y_unit"] = y_unit[:8]
    if caption:
        b["caption"] = caption[:300]
    if reflines:
        b["reflines"] = [{"y": float(y), "label": lab[:32], "emphasis": bool(em)}
                         for y, lab, em in reflines if _finite(y)][:4]
    return b


def bar_chart(title, items, caption=None):
    its = [{"label": lab[:40], "value": float(v)} for lab, v in items if _finite(v)]
    if not its:
        return None
    b = {"type": "bar_chart", "title": title[:80], "variant": "bars", "items": its[:60]}
    if caption:
        b["caption"] = caption[:300]
    return b


def table(title, columns, rows, caption=None):
    """columns: list of (key, label, align[, format]); rows: list of dicts keyed by key."""
    cols = []
    for c in columns:
        key, label, align = c[0], c[1], c[2]
        d = {"key": key, "label": label[:40], "align": align}
        if len(c) > 3 and c[3]:
            d["format"] = c[3]
        cols.append(d)
    keys = {c["key"] for c in cols}
    clean = [{k: (None if (isinstance(v, float) and not math.isfinite(v)) else v)
              for k, v in r.items() if k in keys} for r in rows]
    if not clean:
        return None
    b = {"type": "table", "title": title[:80], "columns": cols[:20], "rows": clean[:500]}
    if caption:
        b["caption"] = caption[:300]
    return b


def text(markdown, lead=False):
    b = {"type": "text", "markdown": markdown[:20000]}
    if lead:
        b["variant"] = "lead"
    return b


def callout(text_, tone="warning", title=None):
    b = {"type": "callout", "tone": tone, "text": text_[:2000]}
    if title:
        b["title"] = title[:120]
    return b


def footnote(items):
    return {"type": "footnote", "items": [{"id": i, "text": t[:1000]} for i, t in items][:30]}


# ─── formatting helpers ───────────────────────────────────────────────────────

def _pct(v, digits=2):
    return f"{v:+.{digits}f}%"


def _num(v, digits=0):
    return f"{v:,.{digits}f}"


def _signed(v, digits=0):
    return f"{v:+,.{digits}f}"


def _tone(v):
    return "pos" if v > 0 else "neg" if v < 0 else "neutral"


def _tw_yi(v):
    """TWD → 億, one decimal, signed."""
    return f"{v / 1e8:+,.1f} 億"


def _dated(delta, ts, asof):
    """Append the series date to a KPI delta when it differs from the report's as-of day."""
    d = pd.Timestamp(ts).strftime("%Y-%m-%d")
    if d == asof:
        return delta or None
    tag = pd.Timestamp(ts).strftime("%m/%d")
    return f"{delta}({tag})" if delta else tag


def _last_two(series):
    s = series.dropna()
    if len(s) == 0:
        return None, None
    return float(s.iloc[-1]), (float(s.iloc[-2]) if len(s) > 1 else None)


def _window_start(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _now_tpe():
    return datetime.now(TPE)


def _today_tpe():
    return _now_tpe().strftime("%Y-%m-%d")


def _calendar_rows(headers, notes, countries=None):
    """Today's priority-1/2 macro events as table rows; [] when none or unavailable."""
    today = _today_tpe()
    try:
        cal = _data.fetch_economic_calendar(headers, start=today, end=today, countries=countries,
                                            max_priority=2, limit=12)
    except Exception as e:  # the brief must not die on a side table
        notes.append(f"經濟日曆抓取失敗({type(e).__name__}),今日事件表省略")
        return []
    rows = []
    for _, r in cal.iterrows():
        t = r.get("time")
        rows.append({"time": t if isinstance(t, str) and t else "—", "country": r.get("country_name") or r.get("country"),
                     "subject": f"{r.get('subject')} {r.get('subject_title') or ''}".strip(),
                     "predict": _fmt_cal(r.get("predict"), r.get("unit")),
                     "last": _fmt_cal(r.get("last"), r.get("unit"))})
    return rows


def _fmt_cal(v, unit):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    u = unit or ""
    return f"{v}{u}" if u in ("%", "") else f"{v} {u}"


_CAL_COLUMNS = [("time", "時間", "left"), ("country", "國家", "left"), ("subject", "指標", "left"),
                ("predict", "預期", "right"), ("last", "前值", "right")]


def _indicator(fn, args, name, ctx, kpis, notes, fmt=lambda v: f"{v:+.2f}"):
    """One Blave indicator series → context line + KPI; None (and a note) when the
    fetch fails or is empty. Indicator values are not P&L, so the KPI stays neutral."""
    try:
        df = fn(*args)
    except Exception as e:
        notes.append(f"{name} 抓取失敗({type(e).__name__})")
        return None
    ser = df["alpha"].dropna() if df is not None and "alpha" in df else pd.Series(dtype=float)
    if len(ser) == 0:
        notes.append(f"{name} 無資料")
        return None
    v, m7 = float(ser.iloc[-1]), float(ser.tail(7).mean())
    ctx[name] = f"{fmt(v)}(7 日均 {fmt(m7)},{ser.index[-1].date()})"
    kpis.append(kpi(name, fmt(v), "neutral", delta=f"7日均 {fmt(m7)}"))
    return ser


# ─── template 1: 台股大盤晨報 ─────────────────────────────────────────────────

def tw_market_brief(date=None, headers=None, lookback_days=90):
    """台股大盤晨報 data pack for the morning of `date` (Taipei; default today).
    Reads the last trading day's close, turnover, 三大法人, 融資, 外資期貨淨多單 and
    the TXF night session; charts cover `lookback_days`."""
    headers = headers or headers_from_env()
    date = date or _today_tpe()
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    notes, ctx, blocks, kpis, foot = [], {}, [], [], []

    idx = _clean_ohlc(_data.fetch_twmarket_index(start, date, headers))   # end=date 已由 fetch 端裁切
    if len(idx) < 2:
        raise ValueError("加權指數資料不足兩個交易日,無法產晨報")
    close, prev = float(idx["Close"].iloc[-1]), float(idx["Close"].iloc[-2])
    asof = idx.index[-1].strftime("%Y-%m-%d")
    chg = close / prev - 1
    high20, _ = _prior20(idx, notes)
    ctx["資料日"] = asof
    ctx["加權指數"] = f"{_num(close, 2)}({_pct(chg * 100)})" + (f",前 20 日高 {_num(high20, 2)}" if high20 is not None else "")
    kpis.append(kpi("加權指數", _num(close, 2), _tone(chg), delta=_pct(chg * 100)))

    turn = _data.fetch_twmarket_turnover(start, date, headers)
    val, val_prev = _last_two(turn["value"]) if len(turn) else (None, None)
    if val is not None:
        avg5 = float(turn["value"].tail(5).mean())
        ctx["成交值"] = f"{val / 1e12:.2f} 兆(5 日均 {avg5 / 1e12:.2f} 兆)"
        kpis.append(kpi("成交值", f"{val / 1e12:.2f}", "neutral", unit="兆",
                        delta=_pct((val / avg5 - 1) * 100) + " vs 5日均"))
    else:
        notes.append("成交值無資料")

    inst = _data.fetch_twmarket_institutional(start, date, headers)
    blocks_inst = None
    inst_ok = len(inst) and all(_finite(inst[c].iloc[-1]) for c in ("foreign", "investment_trust", "dealer", "total"))
    if inst_ok:
        last = inst.iloc[-1]
        f_prev = float(inst["foreign"].iloc[-2]) if len(inst) > 1 and _finite(inst["foreign"].iloc[-2]) else None
        ctx["三大法人"] = (f"外資 {_tw_yi(last['foreign'])}(昨 {_tw_yi(f_prev) if f_prev is not None else '—'})、"
                        f"投信 {_tw_yi(last['investment_trust'])}、自營 {_tw_yi(last['dealer'])}、"
                        f"合計 {_tw_yi(last['total'])}")
        # 指數可能已是今天、籌碼還是昨天:日期不同的 KPI 在 delta 標日期,讀者才不會把兩天讀成同一天。
        kpis.append(kpi("外資買賣超", _tw_yi(float(last["foreign"])), _tone(float(last["foreign"])),
                        delta=_dated("", inst.index[-1], asof)))
        # 合計不佔 KPI 格(六格要留給夜盤),寫在長條圖說明裡。
        blocks_inst = bar_chart("三大法人買賣超(億元)",
                                [("外資", last["foreign"] / 1e8), ("投信", last["investment_trust"] / 1e8),
                                 ("自營商", last["dealer"] / 1e8)],
                                caption=f"{inst.index[-1].strftime('%Y-%m-%d')} 淨買賣超金額,億元;三大法人合計 {_tw_yi(float(last['total']))}")
    else:
        notes.append("三大法人無資料")

    mg = _data.fetch_twmarket_margin(start, date, headers)
    m_last, m_prev = _last_two(mg["margin_balance"]) if len(mg) else (None, None)
    if m_last is not None:
        d_m = (m_last - m_prev) if m_prev is not None else 0.0
        ctx["融資餘額"] = f"{m_last / 1e4:,.1f} 萬張({_signed(d_m / 1e4, 1)} 萬張)"
        kpis.append(kpi("融資餘額", f"{m_last / 1e4:,.1f}", "neutral", unit="萬張",
                        delta=_dated(f"{_signed(d_m / 1e4, 1)} 萬張", mg.index[-1], asof)))
    else:
        notes.append("融資餘額無資料")

    fut = None
    try:
        fut = _data.fetch_twfutures_institutional("TX", start, date, headers)
    except Exception as e:
        notes.append(f"期貨三大法人抓取失敗({type(e).__name__})")
    if fut is not None and len(fut):
        f_last, f_prev = _last_two(fut["foreign_net_oi"])
        d_f = (f_last - f_prev) if f_prev is not None else 0.0
        ctx["外資期貨淨多單"] = f"{_signed(f_last)} 口({_signed(d_f)} 口,{fut.index[-1].strftime('%m-%d')})"
        # 淨部位是方向不是損益:長期淨空會永遠紅,上色沒有資訊,一律 neutral。
        kpis.append(kpi("外資期貨淨多單", _signed(f_last), "neutral", unit="口",
                        delta=_dated(_signed(d_f) + " 口", fut.index[-1], asof)))
        foot.append(("futinst", "期貨三大法人為 TAIFEX 盤後統計,晨報引用的是前一交易日收盤後的未平倉淨口數(多 − 空)。"))

    night = _txf_night_session(headers, asof, notes)
    if night:
        ctx["台指期夜盤"] = (f"{_num(night['close'])}({night['state']};{_pct(night['chg'] * 100)} "
                          f"vs 日盤收 {_num(night['day_close'])})")
        label = "台指期夜盤" if night["done"] else f"台指期夜盤({night['state']})"
        kpis.append(kpi(label, _num(night["close"]), _tone(night["chg"]), delta=_pct(night["chg"] * 100)))
        foot.append(("night", "台指期夜盤 = 15:00 至次日 05:00 的交易時段,漲跌以同日日盤收盤價為基準。"
                     + ("" if night["done"] else "數值為截至標示時點的最新價,不是收盤價。")))

    blocks.append(kpi_row(kpis))
    ck = candlestick("加權指數", idx.tail(_PRICE_BARS), y_unit="點",
                     reflines=[(high20, "前 20 日高", False)] if high20 is not None else None)
    if ck:
        blocks.append(ck)
    if blocks_inst:
        blocks.append(blocks_inst)
    if m_last is not None:
        lc = line_chart("融資餘額", [("融資餘額", "primary", mg["margin_balance"] / 1e4)], y_unit="萬張")
        if lc:
            blocks.append(lc)
    if fut is not None and len(fut):
        lc = line_chart("外資期貨淨多單", [("外資淨多單", "primary", fut["foreign_net_oi"])], y_unit="口",
                        reflines=[(0.0, "0", False)])
        if lc:
            blocks.append(lc)
    cal = _calendar_rows(headers, notes, countries=["US", "CN", "TW", "JP", "EU"])
    if cal:
        blocks.append(table("今日總經事件", _CAL_COLUMNS, cal, caption="台北時間;priority 1–2 的事件"))
    foot += [("src", "指數、成交值、三大法人、融資餘額:TWSE 日資料,經 Blave API。三大法人為淨買賣超金額,融資餘額為張數。前 20 日高 = 不含當日的前 20 個交易日最高價。")]
    blocks.append(footnote(foot))

    # 標題不帶日期——側欄列本身顯示建立時間(Wei 2026-09-02 拍板);id 仍帶日期,同日重跑才會覆蓋。
    # 資料日與晨報日不同才標 period,同日就省(印「09/02–09/02」沒有資訊)。
    meta = {} if asof == date else {"period": {"from": asof[5:].replace("-", "/"), "to": date[5:].replace("-", "/")}}
    return Pack(f"tw-market-{date.replace('-', '')}", "台股大盤晨報", "morning", "台股大盤晨報", blocks, ctx, notes, meta=meta)


def _txf_night_session(headers, day, notes):
    """Last TXF night session after trading day `day` (YYYY-MM-DD): close and change
    vs that day's day-session close, from 60m bars. None when the source has no
    bars in the 15:00–05:00 window (or no bars at all)."""
    try:
        df = _data.fetch_twfutures_ohlcv("TXF", "60m", (pd.Timestamp(day) - timedelta(days=5)).strftime("%Y-%m-%d"),
                                         None, headers)
    except Exception as e:
        notes.append(f"台指期 60m 抓取失敗({type(e).__name__}),夜盤省略")
        return None
    if df is None or len(df) == 0:
        notes.append("台指期 60m 無資料,夜盤省略")
        return None
    t = df.index
    t = t.tz_localize("UTC") if t.tz is None else t
    tpe = t.tz_convert(TPE)
    d = pd.Timestamp(day).date()
    day_mask = (tpe.date == d) & (tpe.hour >= 8) & (tpe.hour < 14)
    # 夜盤 15:00 至次日 05:00;bar 以起始分鐘標記,收盤那根標 05:00,所以次日取 hour ≤ 5。
    night_mask = ((tpe.date == d) & (tpe.hour >= 15)) | ((tpe.date == d + timedelta(days=1)) & (tpe.hour <= 5))
    if not day_mask.any():
        notes.append(f"台指期 {day} 無日盤 60m bar,夜盤省略")
        return None
    if not night_mask.any():
        notes.append(f"台指期 {day} 無夜盤 bar(資料源可能不含夜盤,或夜盤尚未開始)")
        return None
    day_close = float(df.loc[day_mask, "Close"].iloc[-1])
    close = float(df.loc[night_mask, "Close"].iloc[-1])
    # bar 以起始時間標記(api 丟掉未收的那根,export 路徑可能留著),價格的時點 = min(起始 + 60 分,
    # 現在, 05:00)。夜盤還在交易時這是盤中價;不標出來,agent 會寫成「夜盤收」(uid=1 T13)。
    now = pd.Timestamp(_now_tpe())
    session_end = pd.Timestamp(d + timedelta(days=1)).tz_localize(TPE) + pd.Timedelta(hours=5)
    as_of = min(tpe[night_mask][-1] + pd.Timedelta(minutes=60), now, session_end)
    done = as_of >= session_end
    if done:
        state = "收盤"
    elif now < session_end:
        state = f"盤中,截至 {as_of:%H:%M}"
    else:
        state = f"截至 {as_of:%H:%M},資料未含收盤"
    return {"close": close, "day_close": day_close, "chg": close / day_close - 1, "state": state, "done": done}


# ─── template 2: 加密市場晨報 ─────────────────────────────────────────────────

def crypto_market_brief(date=None, headers=None, symbols=("BTC", "ETH", "SOL"), lookback_days=30):
    """加密市場晨報 data pack: BTC/ETH(/others) price and returns, funding, and the
    market-wide Blave indicators (市場方向 / 資金稀缺 / 頂尖交易員曝險)."""
    headers = headers or headers_from_env()
    date = date or _today_tpe()          # 報告日與 id 一律台北日期,同日重跑才會覆蓋
    start = _window_start(lookback_days + 2)
    notes, ctx, blocks, kpis, foot = [], {}, [], [], []
    syms = [_data.normalize_symbol(s if s.endswith("USDT") else s + "USDT") for s in symbols]
    klines = _data.fetch_kline_batch(syms, "1d", start, None, headers)
    closes = {}
    for s in syms:
        df = klines.get(s)
        if df is None or len(df) < 2:
            notes.append(f"{s} 日 K 不足")
            continue
        closes[s] = df["Close"].dropna()
    if not closes:
        raise ValueError("沒有任何幣種的日 K,無法產晨報")
    rows = []
    for s, c in closes.items():
        last = float(c.iloc[-1])
        r1 = c.iloc[-1] / c.iloc[-2] - 1
        r7 = c.iloc[-1] / c.iloc[-8] - 1 if len(c) > 8 else None
        r30 = c.iloc[-1] / c.iloc[-(lookback_days + 1)] - 1 if len(c) > lookback_days else None
        label = s.replace("USDT", "")
        rows.append({"symbol": label, "price": _num(last, 2), "r1": _pct(r1 * 100),
                     "r7": _pct(r7 * 100) if r7 is not None else None,
                     "r30": _pct(r30 * 100) if r30 is not None else None})
        ctx[label] = f"{_num(last, 2)},1d {_pct(r1 * 100)}" + (f",30d {_pct(r30 * 100)}" if r30 is not None else "")
        if len(kpis) < 2:
            kpis.append(kpi(label, _num(last, 2), _tone(r1), unit="USDT", delta=_pct(r1 * 100)))
    ctx["資料日"] = str(next(iter(closes.values())).index[-1].date())

    fund = _indicator(_data.fetch_funding_rate, ("BTCUSDT", "1d", start, None, headers), "BTC 資金費率",
                      ctx, kpis, notes, fmt=lambda v: f"{v:+.4f}%")
    direction = _indicator(_data.fetch_market_direction, ("1d", start, None, headers), "市場方向", ctx, kpis, notes)
    shortage = _indicator(_data.fetch_capital_shortage, ("1d", start, None, headers), "資金稀缺", ctx, kpis, notes)
    exposure = _indicator(_data.fetch_top_trader_exposure, ("1d", start, None, headers), "頂尖交易員曝險", ctx, kpis, notes)

    blocks.append(kpi_row(kpis[:6]))
    base = next(iter(closes))
    win = {s: c.tail(lookback_days + 1) for s, c in closes.items()}   # 圖與表同一個 N 日窗口
    series = [(base.replace("USDT", ""), "primary", win[base] / win[base].iloc[0] * 100)]
    series += [(s.replace("USDT", ""), "benchmark", c / c.iloc[0] * 100) for s, c in win.items() if s != base][:3]
    lc = line_chart(f"相對表現(重定基 100,{lookback_days} 日)", series, y_unit="",
                    caption="每個幣種以窗口第一天收盤為 100")
    if lc:
        blocks.append(lc)
    blocks.append(table("主要幣種報價與報酬", [("symbol", "幣種", "left"), ("price", "價格", "right"),
                                              ("r1", "1 日", "right", "percent"), ("r7", "7 日", "right", "percent"),
                                              ("r30", f"{lookback_days} 日", "right", "percent")], rows,
                        caption="Binance USDT 永續日 K 收盤;最後一根為今日未收盤 bar"))
    if fund is not None:
        lc = line_chart("BTC 資金費率", [("BTC", "primary", fund)], y_unit="%", reflines=[(0.0, "0", False)])
        if lc:
            blocks.append(lc)
    ind = [(n, "benchmark", s) for n, s in (("市場方向", direction), ("資金稀缺", shortage)) if s is not None]
    if ind:
        ind[0] = (ind[0][0], "primary", ind[0][2])
        lc = line_chart("Blave 市場指標(z-score)", ind, caption="標準化分數,0 = 樣本均值;日頻資料只到前一個完整日")
        if lc:
            blocks.append(lc)
    if exposure is not None:
        # 這支不是 z-score(實測值約 20–30),不能跟上面同軸;單獨一張、不標單位。
        lc = line_chart("頂尖交易員曝險", [("曝險", "primary", exposure)],
                        caption="Blave 頂尖交易員曝險指標原始值(非標準化),日頻資料只到前一個完整日")
        if lc:
            blocks.append(lc)
    cal = _calendar_rows(headers, notes, countries=["US", "CN", "EU", "JP"])
    if cal:
        blocks.append(table("今日總經事件", _CAL_COLUMNS, cal, caption="台北時間;priority 1–2 的事件"))
    foot += [("src", "價格:Binance USDT 永續日 K。資金費率為 Binance 日頻,單位 %。市場方向 / 資金稀缺為 Blave 指標(z-score);頂尖交易員曝險為指標原始值。日頻指標只到前一個完整日。")]
    blocks.append(footnote(foot))
    return Pack(f"crypto-market-{date.replace('-', '')}", "加密市場晨報", "morning", "加密市場晨報", blocks, ctx, notes)


# ─── template 3: 單標的晨報 ───────────────────────────────────────────────────

def symbol_brief(symbol, date=None, headers=None, lookback_days=90):
    """單標的晨報 data pack. A 4–6 digit id is a Taiwan stock (日 K + 外資買賣超);
    anything else is a crypto USDT perp (日 K + 資金費率 + 爆倉 / 巨鯨 / 多空力道)."""
    headers = headers or headers_from_env()
    sym = str(symbol).strip().upper()
    if sym.isdigit():
        return _tw_symbol_brief(sym, date, headers, lookback_days)
    return _crypto_symbol_brief(sym, date, headers, lookback_days)


def _prior20(bars, notes):
    """(前 20 日高, 前 20 日低) = 倒數第 2–21 根的最高價/最低價,或 (None, None) 並記 notes。"""
    # 不含當日:含當日時收盤永遠不可能高於這個值,判讀會寫出與事實相反的「仍在 20 日高之下」;
    # 不含當日,今日收盤才可能高於(或低於)這個值。不足 21 根就不算,不拿短窗口頂替。
    if len(bars) < 21:
        notes.append(f"日 K 只有 {len(bars)} 根,不足 21 根,前 20 日高/低省略")
        return None, None
    w = bars.iloc[-21:-1]
    return float(w["High"].max()), float(w["Low"].min())


def _levels(bars, notes):
    """bars: `_clean_ohlc` 過的日 K(同一份也拿去畫圖)。均線取收盤,含當日。"""
    hi, lo = _prior20(bars, notes)
    lv = {} if hi is None else {"前 20 日高": hi, "前 20 日低": lo}
    for n in (5, 20, 60):
        if len(bars) >= n:
            lv[f"{n} 日均"] = float(bars["Close"].tail(n).mean())
    return lv


_LEVELS_TITLE = "近期高低與均線"


def _level_lines(lv):
    # 兩條都不強調:紅色低點線讀起來就是在標支撐,與「價位是統計、不是支撐」相反。
    return [(lv[k], k, False) for k in ("前 20 日高", "前 20 日低") if k in lv]


def _levels_table(lv, last):
    rows = [{"level": k, "price": _num(v, 2), "dist": _pct((last / v - 1) * 100)} for k, v in lv.items()]
    # 距現價是方向不是損益,不走 percent 的上色閘門。
    return table(_LEVELS_TITLE, [("level", "項目", "left"), ("price", "數值", "right"), ("dist", "距現價", "right")],
                 rows, caption="歷史統計值,非支撐壓力或進出場價。距現價 = 現價相對該數值的百分比,正值表示現價在其上")


def _tw_symbol_brief(stock_id, date, headers, lookback_days):
    date = date or _today_tpe()
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    notes, ctx, blocks, kpis, foot = [], {}, [], [], []
    df = _data.fetch_twstock_ohlcv(stock_id, "1d", headers, start=start, end=date)
    df = _clean_ohlc(df) if df is not None else df
    if df is None or len(df) < 2:
        raise ValueError(f"{stock_id} 日 K 不足兩個交易日")
    c = df["Close"].dropna()
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])
    chg = last / prev - 1
    vol, vol5 = float(df["Volume"].iloc[-1]), float(df["Volume"].tail(5).mean())
    ctx["資料日"] = str(c.index[-1].date())
    ctx["收盤"] = f"{_num(last, 2)}({_pct(chg * 100)}),量 {_num(vol)} 張(5 日均 {_num(vol5)})"
    kpis.append(kpi("收盤", _num(last, 2), _tone(chg), delta=_pct(chg * 100)))
    kpis.append(kpi("成交量", _num(vol), "neutral", unit="張", delta=_pct((vol / vol5 - 1) * 100) + " vs 5日均"))
    lv = _levels(df, notes)
    ctx[_LEVELS_TITLE] = ", ".join(f"{k} {_num(v, 2)}" for k, v in lv.items())

    inst = None
    try:
        inst = _data.fetch_twstock_institutional(stock_id, start, date, headers)
    except Exception as e:
        notes.append(f"外資買賣超抓取失敗({type(e).__name__})")
    if inst is not None and len(inst) and "foreign_net" in inst:
        fn = inst["foreign_net"].dropna() / 1000.0   # 股 → 張
        if len(fn):
            f_last = float(fn.iloc[-1])
            f5 = float(fn.tail(5).sum())
            ctx["外資買賣超"] = f"{_signed(f_last)} 張(近 5 日累計 {_signed(f5)} 張,{fn.index[-1].date()})"
            kpis.append(kpi("外資買賣超", _signed(f_last), _tone(f_last), unit="張", delta=f"5日累計 {_signed(f5)}"))
            foot.append(("inst", "外資買賣超 = 外資買進 − 賣出,資料源以股為單位,此處換算為張(÷1000)。"))
    blocks.append(kpi_row(kpis[:6]))
    ck = candlestick(f"{stock_id} 日 K", df.tail(_PRICE_BARS), y_unit="元", reflines=_level_lines(lv))
    if ck:
        blocks.append(ck)
    if inst is not None and len(inst) and "foreign_net" in inst:
        tail = (inst["foreign_net"].dropna() / 1000.0).tail(10)
        bc = bar_chart("外資近 10 日買賣超(張)", [(t.strftime("%m/%d"), v) for t, v in tail.items()])
        if bc:
            blocks.append(bc)
    lt = _levels_table(lv, last)
    if lt:
        blocks.append(lt)
    foot.append(("src", "日 K 為 TWSE 未還原價,成交量為張。前 20 日高/低 = 不含當日的前 20 個交易日最高價/最低價,均線取收盤價(含當日)。"))
    blocks.append(footnote(foot))
    return Pack(f"symbol-{stock_id}-{date.replace('-', '')}", f"{stock_id} 晨報", "morning", "單標的晨報",
                blocks, ctx, notes)


def _crypto_symbol_brief(sym, date, headers, lookback_days):
    date = date or _today_tpe()
    start = _window_start(lookback_days + 2)
    s = _data.normalize_symbol(sym if sym.endswith("USDT") else sym + "USDT")
    label = s.replace("USDT", "")
    notes, ctx, blocks, kpis, foot = [], {}, [], [], []
    df = _data.fetch_kline(s, "1d", start, None, headers)
    df = _clean_ohlc(df) if df is not None else df
    if df is None or len(df) < 2:
        raise ValueError(f"{s} 日 K 不足")
    c = df["Close"].dropna()
    last, chg = float(c.iloc[-1]), float(c.iloc[-1] / c.iloc[-2] - 1)
    ctx["資料日"] = str(c.index[-1].date())
    ctx["價格"] = f"{_num(last, 2)} USDT({_pct(chg * 100)})"
    kpis.append(kpi(label, _num(last, 2), _tone(chg), unit="USDT", delta=_pct(chg * 100)))
    lv = _levels(df, notes)
    ctx[_LEVELS_TITLE] = ", ".join(f"{k} {_num(v, 2)}" for k, v in lv.items())

    args = (s, "1d", start, None, headers)
    fund = _indicator(_data.fetch_funding_rate, args, "資金費率", ctx, kpis, notes, fmt=lambda v: f"{v:+.4f}%")
    liq = _indicator(_data.fetch_liquidation, args, "爆倉指標", ctx, kpis, notes)
    whale = _indicator(_data.fetch_whale_hunter, args, "巨鯨警報", ctx, kpis, notes)
    taker = _indicator(_data.fetch_taker_intensity, args, "多空力道", ctx, kpis, notes)
    blocks.append(kpi_row(kpis[:6]))
    # 60 日均仍用整段收盤算,K 線只畫最後 _PRICE_BARS 根。
    ck = candlestick(f"{label} 日 K", df.tail(_PRICE_BARS), y_unit="USDT", reflines=_level_lines(lv))
    if ck:
        blocks.append(ck)
    if fund is not None:
        lc = line_chart("資金費率", [(label, "primary", fund)], y_unit="%", reflines=[(0.0, "0", False)])
        if lc:
            blocks.append(lc)
    ind = [(n, "benchmark", x) for n, x in (("爆倉指標", liq), ("巨鯨警報", whale), ("多空力道", taker)) if x is not None]
    if ind:
        ind[0] = (ind[0][0], "primary", ind[0][2])
        lc = line_chart("Blave 指標(z-score)", ind, caption="標準化分數,0 = 樣本均值;日頻資料只到前一個完整日")
        if lc:
            blocks.append(lc)
    lt = _levels_table(lv, last)
    if lt:
        blocks.append(lt)
    foot.append(("src", "價格:Binance USDT 永續日 K,最後一根為今日未收盤 bar。資金費率單位 %。爆倉 / 巨鯨 / 多空力道為 Blave 指標 z-score。前 20 日高/低 = 不含當日(今日未收盤 bar)的前 20 根日 K 最高價/最低價,均線取收盤價(含當日)。"))
    blocks.append(footnote(foot))
    return Pack(f"symbol-{label.lower()}-{date.replace('-', '')}", f"{label} 晨報", "morning", "單標的晨報",
                blocks, ctx, notes)


# ─── publish ──────────────────────────────────────────────────────────────────

def publish(pack, narrative=None, report_id=None, title=None, origin=None):
    """Assemble the pack and the narrative into a report and drop it. Returns the path.

    narrative: {"lead", "read", "watch", "risk"} — any subset, markdown, each capped
    by `pack.slots`. `lead` becomes the opening conclusion card (right after meta),
    `read`/`watch` become sections after the data blocks, `risk` a warning callout
    just before the footnote. No narrative = a data-only report — the honest form
    for a scheduled run, never a place for a made-up view.
    origin: "chat" (default) or "scheduled" — shown in the report header."""
    narrative = dict(narrative or {})
    if "action" in narrative:
        raise ValueError("'action' was renamed to 'watch' (觀察重點): conditions and indicator thresholds "
                         "only, no trade instruction, see references/reports.md §1b")
    unknown = set(narrative) - set(pack.slots)
    if unknown:
        raise ValueError(f"unknown narrative slot(s): {sorted(unknown)}; allowed: {sorted(pack.slots)}")
    for k, v in narrative.items():
        cap = pack.slots[k][1]
        if not isinstance(v, str):
            raise ValueError(f"narrative[{k!r}] must be a markdown string")
        if len(v) > cap:
            raise ValueError(f"narrative[{k!r}] is {len(v)} chars, cap {cap} — cut it, don't summarise the summary")
    blocks = list(pack.blocks)
    foot = blocks.pop() if blocks and blocks[-1].get("type") == "footnote" else None
    out = []
    if narrative.get("lead", "").strip():
        out.append(text(narrative["lead"].strip(), lead=True))
    out += blocks
    for key in ("read", "watch"):
        body = narrative.get(key, "").strip()
        if body:
            heading = pack.slots[key][0]
            # 只有 body 自己已經以這個標題開頭才省略;以 ### 子標或 #1 開頭的段落照常加標題。
            out.append(text(body if not heading or body.startswith(heading) else f"{heading}\n\n{body}"))
    if narrative.get("risk", "").strip():
        out.append(callout(narrative["risk"].strip(), tone="warning", title=pack.slots["risk"][0]))
    if foot:
        out.append(foot)
    # [^id] 是 api 唯一會拒的敘事錯誤,而 id 清單就在手上——本地先擋,免得整份進 failed/。
    known = {i["id"] for i in (foot or {}).get("items", [])}
    for key, body in narrative.items():
        missing = sorted(set(_FNREF_RE.findall(body)) - known)
        if missing:
            raise ValueError(f"narrative[{key!r}] references footnote id(s) {missing} that the pack has not got; known: {sorted(known)}")
    if origin not in (None, "chat", "scheduled"):
        raise ValueError("origin must be 'chat' or 'scheduled'")
    meta = dict(pack.meta)
    narrated = any(v.strip() for v in narrative.values())
    meta["origin"] = origin or ("chat" if narrated else "scheduled")
    # 純數據包用自己的 id(-auto):排程版同一天跑,不能把早上那份有判讀的蓋掉
    # (29026 實測:cron 首跑覆蓋了對話產的 tw-market-20260902)。明給 report_id 就照給。
    if report_id is None:
        report_id = pack.report_id if narrated else pack.report_id + "-auto"
    # write_report prints the "moved to reports/sent/, reply now" line for both paths.
    return write_report(report_id, title or pack.title, out,
                        type=pack.type, report_type=pack.report_type, meta=meta)
