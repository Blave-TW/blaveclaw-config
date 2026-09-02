"""
Report templates — the deterministic half of a report, built from `lib.data`.

A template returns a `Pack`: the data blocks (KPI row, charts, tables, footnote)
already in contract shape, plus the numbers behind them (`pack.context`) and the
narrative slots left for you to fill (`pack.slots`). You write the judgement —
lead / read / action / risk — and `publish()` assembles and drops the report.
You never build a chart block by hand for these report types, and you never
recompute a number the pack already carries.

    from lib.report_templates import tw_market_brief, publish

    pack = tw_market_brief()              # today's TW market data pack
    print(pack.describe())                # the numbers, one line each — cite these
    publish(pack, narrative={
        "lead":   "...one falsifiable claim...",
        "read":   "...what the numbers say and why...",
        "action": "...what to do about it...",
        "risk":   "...the level that would prove the lead wrong...",
    })

    publish(pack)                          # no narrative = data pack only
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
    "action": ("## 操作建議", 1500),
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


def _today_tpe():
    return datetime.now(TPE).strftime("%Y-%m-%d")


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

    idx = _data.fetch_twmarket_index(start, date, headers)   # end=date 已由 fetch 端裁切
    if len(idx) < 2:
        raise ValueError("加權指數資料不足兩個交易日,無法產晨報")
    close, prev = float(idx["Close"].iloc[-1]), float(idx["Close"].iloc[-2])
    asof = idx.index[-1].strftime("%Y-%m-%d")
    chg = close / prev - 1
    high20 = float(idx["Close"].tail(20).max())
    ctx["資料日"] = asof
    ctx["加權指數"] = f"{_num(close, 2)}({_pct(chg * 100)}),20 日高 {_num(high20, 2)}"
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
        ctx["台指期夜盤"] = f"{_num(night['close'])}({_pct(night['chg'] * 100)} vs 日盤收 {_num(night['day_close'])})"
        kpis.append(kpi("台指期夜盤", _num(night["close"]), _tone(night["chg"]), delta=_pct(night["chg"] * 100)))
        foot.append(("night", "台指期夜盤 = 15:00 至次日 05:00 的交易時段,漲跌以同日日盤收盤價為基準。"))

    blocks.append(kpi_row(kpis))
    lc = line_chart("加權指數", [("加權指數", "primary", idx["Close"])], y_unit="點",
                    reflines=[(high20, "20 日高", False)])
    if lc:
        blocks.append(lc)
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
    foot += [("src", "指數、成交值、三大法人、融資餘額:TWSE 日資料,經 Blave API。三大法人為淨買賣超金額,融資餘額為張數。")]
    blocks.append(footnote(foot))

    title = f"台股大盤晨報 {date}"
    return Pack(f"tw-market-{date.replace('-', '')}", title, "morning", "台股大盤晨報", blocks, ctx, notes,
                meta={"period": {"from": asof[5:].replace("-", "/"), "to": date[5:].replace("-", "/")}})


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
    return {"close": close, "day_close": day_close, "chg": close / day_close - 1}


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
    title = f"加密市場晨報 {date}"
    return Pack(f"crypto-market-{date.replace('-', '')}", title, "morning", "加密市場晨報", blocks, ctx, notes)


# ─── template 3: 單標的晨報 ───────────────────────────────────────────────────

def symbol_brief(symbol, date=None, headers=None, lookback_days=90):
    """單標的晨報 data pack. A 4–6 digit id is a Taiwan stock (日 K + 外資買賣超);
    anything else is a crypto USDT perp (日 K + 資金費率 + 爆倉 / 巨鯨 / 多空力道)."""
    headers = headers or headers_from_env()
    sym = str(symbol).strip().upper()
    if sym.isdigit():
        return _tw_symbol_brief(sym, date, headers, lookback_days)
    return _crypto_symbol_brief(sym, date, headers, lookback_days)


def _levels(close):
    c = close.dropna()
    lv = {"20 日高": float(c.tail(20).max()), "20 日低": float(c.tail(20).min())}
    for n in (5, 20, 60):
        if len(c) >= n:
            lv[f"{n} 日均"] = float(c.tail(n).mean())
    return lv


def _levels_table(lv, last):
    rows = [{"level": k, "price": _num(v, 2), "dist": _pct((last / v - 1) * 100)} for k, v in lv.items()]
    # 距現價是方向不是損益,不走 percent 的上色閘門。
    return table("關鍵價位", [("level", "價位", "left"), ("price", "價格", "right"), ("dist", "距現價", "right")],
                 rows, caption="距現價 = 現價相對該價位的百分比,正值表示現價在其上")


def _tw_symbol_brief(stock_id, date, headers, lookback_days):
    date = date or _today_tpe()
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    notes, ctx, blocks, kpis, foot = [], {}, [], [], []
    df = _data.fetch_twstock_ohlcv(stock_id, "1d", headers, start=start, end=date)
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
    lv = _levels(c)
    ctx["關鍵價位"] = ", ".join(f"{k} {_num(v, 2)}" for k, v in lv.items())

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
    lc = line_chart(f"{stock_id} 收盤", [(stock_id, "primary", c)], y_unit="元",
                    reflines=[(lv["20 日高"], "20 日高", False), (lv["20 日低"], "20 日低", True)])
    if lc:
        blocks.append(lc)
    if inst is not None and len(inst) and "foreign_net" in inst:
        tail = (inst["foreign_net"].dropna() / 1000.0).tail(10)
        bc = bar_chart("外資近 10 日買賣超(張)", [(t.strftime("%m/%d"), v) for t, v in tail.items()])
        if bc:
            blocks.append(bc)
    blocks.append(_levels_table(lv, last))
    foot.append(("src", "日 K 為 TWSE 未還原價,成交量為張。"))
    blocks.append(footnote(foot))
    return Pack(f"symbol-{stock_id}-{date.replace('-', '')}", f"{stock_id} 晨報 {date}", "morning", "單標的晨報",
                blocks, ctx, notes)


def _crypto_symbol_brief(sym, date, headers, lookback_days):
    date = date or _today_tpe()
    start = _window_start(lookback_days + 2)
    s = _data.normalize_symbol(sym if sym.endswith("USDT") else sym + "USDT")
    label = s.replace("USDT", "")
    notes, ctx, blocks, kpis, foot = [], {}, [], [], []
    df = _data.fetch_kline(s, "1d", start, None, headers)
    if df is None or len(df) < 2:
        raise ValueError(f"{s} 日 K 不足")
    c = df["Close"].dropna()
    last, chg = float(c.iloc[-1]), float(c.iloc[-1] / c.iloc[-2] - 1)
    ctx["資料日"] = str(c.index[-1].date())
    ctx["價格"] = f"{_num(last, 2)} USDT({_pct(chg * 100)})"
    kpis.append(kpi(label, _num(last, 2), _tone(chg), unit="USDT", delta=_pct(chg * 100)))
    lv = _levels(c)
    ctx["關鍵價位"] = ", ".join(f"{k} {_num(v, 2)}" for k, v in lv.items())

    args = (s, "1d", start, None, headers)
    fund = _indicator(_data.fetch_funding_rate, args, "資金費率", ctx, kpis, notes, fmt=lambda v: f"{v:+.4f}%")
    liq = _indicator(_data.fetch_liquidation, args, "爆倉指標", ctx, kpis, notes)
    whale = _indicator(_data.fetch_whale_hunter, args, "巨鯨警報", ctx, kpis, notes)
    taker = _indicator(_data.fetch_taker_intensity, args, "多空力道", ctx, kpis, notes)
    blocks.append(kpi_row(kpis[:6]))
    lc = line_chart(f"{label} 收盤", [(label, "primary", c)], y_unit="USDT",
                    reflines=[(lv["20 日高"], "20 日高", False), (lv["20 日低"], "20 日低", True)])
    if lc:
        blocks.append(lc)
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
    blocks.append(_levels_table(lv, last))
    foot.append(("src", "價格:Binance USDT 永續日 K,最後一根為今日未收盤 bar。資金費率單位 %。爆倉 / 巨鯨 / 多空力道為 Blave 指標 z-score。"))
    blocks.append(footnote(foot))
    return Pack(f"symbol-{label.lower()}-{date.replace('-', '')}", f"{label} 晨報 {date}", "morning", "單標的晨報",
                blocks, ctx, notes)


# ─── publish ──────────────────────────────────────────────────────────────────

def publish(pack, narrative=None, report_id=None, title=None, origin=None):
    """Assemble the pack and the narrative into a report and drop it. Returns the path.

    narrative: {"lead", "read", "action", "risk"} — any subset, markdown, each capped
    by `pack.slots`. `lead` becomes the opening conclusion card (right after meta),
    `read`/`action` become sections after the data blocks, `risk` a warning callout
    just before the footnote. No narrative = a data-only report — the honest form
    for a scheduled run, never a place for a made-up view.
    origin: "chat" (default) or "scheduled" — shown in the report header."""
    narrative = dict(narrative or {})
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
    for key in ("read", "action"):
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
    meta["origin"] = origin or ("chat" if any(v.strip() for v in narrative.values()) else "scheduled")
    return write_report(report_id or pack.report_id, title or pack.title, out,
                        type=pack.type, report_type=pack.report_type, meta=meta)
