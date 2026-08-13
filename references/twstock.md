# 台股資料 — Taiwan Stock Data

> 台股資料（日K、三大法人、融資融券、股權分級、財報、月營收、分點買賣超）由 [FinMind](https://finmindtrade.com) 提供；
> 股票清單/基本資料（industry_code、listing_date）例外，來自 TWSE/TPEx 官方公司資料，非 FinMind。

**⚠️ 一律優先用下面這些 `lib/data.py` 函式(不只是寫策略時,單純聊天問答也一樣),函式裡沒有的資料才去外面找。** `lib/data.py` 已經做好新鮮度、fallback、cache,手寫腳本沒有這層保護,拿到舊資料或直接崩潰都有可能。若 `lib/data.py` 的呼叫本身失敗,回報失敗,不要改用手寫腳本、更不要拿崩潰前的部分輸出當答案。

## 股票池（Universe）建立

用 `fetch_twstock_list(headers)`（見 `lib/data.py`）—— 回傳 DataFrame，index 為 `stock_id`，
欄位 `name`、`close`、`industry_code`、`listing_date`（`YYYY-MM-DD`）。涵蓋上市 + 上櫃全市場
（含 ETF，`industry_code`/`listing_date` 為 `None`）。基本資料一天更新一次，函式內建 1 天快取。

```python
df = fetch_twstock_list(headers)

# 全部普通股（排除 ETF / 無產業別的證券）
universe = df[df['industry_code'].notna()].index.tolist()

# 依產業別篩選（例如半導體 '24' + 電腦及周邊 '25'）
tech = df[df['industry_code'].isin(['24', '25'])].index.tolist()
```

`industry_code` 是 TWSE/TPEx 原始數字代碼（passthrough，不是解碼過的名稱）。常用代碼：
`15` 航運業、`17` 金融保險業、`22` 生技醫療業、`24` 半導體業、`25` 電腦及週邊設備業、
`26` 光電業、`27` 通信網路業、`28` 電子零組件業、`29` 電子通路業、`30` 資訊服務業、
`31` 其他電子業（完整清單見 TWSE/TPEx 公司基本資料;上面這份是常見科技/金融產業子集,不是全部)。

**⚠️ Universe 抽樣規則 — 必須分散產業**

台股代碼按產業群組排列（1xxx 水泥/食品、2xxx 紡織/化工/電子、9xxx 其他），直接取前 N 支會集中在少數產業。**回測 universe 必須依產業抽樣**，不得直接用 `[:100]` 截斷：

```python
import random, collections

df = fetch_twstock_list(headers)
stocks = df[df['industry_code'].notna()]  # 排除 ETF 等無產業別的證券

# 依產業別分組
by_sector = collections.defaultdict(list)
for stock_id, row in stocks.iterrows():
    by_sector[row['industry_code']].append(stock_id)

# 每個產業等比例抽樣，合計 N 支
def sample_by_sector(by_sector, total=100, seed=42):
    rng = random.Random(seed)
    n_sectors = len(by_sector)
    per_sector = max(1, total // n_sectors)
    result = []
    for sids in by_sector.values():
        result.extend(rng.sample(sids, min(per_sector, len(sids))))
    # 若不足 total，從各產業再補
    all_ids = [s for sids in by_sector.values() for s in sids if s not in result]
    rng.shuffle(all_ids)
    result.extend(all_ids[:total - len(result)])
    return result[:total]

universe = sample_by_sector(by_sector, total=100)
```

固定 `seed` 確保回測可重現。若用戶有指定產業，改用產業篩選後再抽樣。

單支股票基本資料查詢用 `fetch_twstock_info(stock_id, headers)` —— 回傳
`{stock_id, name, close, industry_code, listing_date}` 或 `None`（查無此股）；內部直接查
`fetch_twstock_list` 的快取結果，不另外打 API。

---

## 台股日K — 原始 vs 還原價

| 函式 | endpoint | 何時使用 |
|---|---|---|
| `fetch_twstock_price(sid, start, end, hdrs)` | `/twstock/price/` | **畫圖、走勢查詢** — 原始市價，符合用戶在 app 看到的價格；欄位 Open/High/Low/Close/Volume |
| `fetch_twstock_price_adj(sid, start, end, hdrs)` | `/twstock/price_adj/` | **回測** — 向後除權息還原價，歷史報酬可比較；欄位 Open/Close |

> 除權息後原始價格會向下跳空，還原價則平滑消除跳空，適合計算指標與回報。  
> 用戶問「台積電最近走勢怎樣」→ `fetch_twstock_price`；要跑 SMA 回測 → `fetch_twstock_price_adj`。

---

## Real-Time Quote (即時報價)

| Function | Endpoint | When to use |
|---|---|---|
| `fetch_twstock_quote(sid, headers)` | `/twstock/quote/<sid>` | Single stock — current price check before a trade decision, "what's it trading at right now" |
| `fetch_twstock_quote_batch(stock_ids, headers)` | `/twstock/quote` | Multiple stocks in one call (max 50) — checking several holdings at once |

Both return a plain **dict** (or `{stock_id: dict}` for batch), not a DataFrame — there is
no date range to index on, only the current snapshot. Refreshes approximately every 10
seconds during market and post-market sessions; there is no history endpoint variant.

```python
quote = fetch_twstock_quote('2330', headers)
# {'open': 2415.0, 'high': 2465.0, 'low': 2415.0, 'close': 2445.0,
#  'change_price': -20.0, 'change_rate': -0.81, 'average_price': 2432.58,
#  'volume': 4245, 'total_volume': 26403, 'amount': 10379025000, 'total_amount': 64227410000,
#  'yesterday_volume': 27390, 'buy_price': 2445.0, 'buy_volume': 17,
#  'sell_price': 2450.0, 'sell_volume': 11, 'volume_ratio': 0.96,
#  'quote_time': '2026-07-03 14:30:00', 'stock_id': '2330', 'tick_type': 2}

quotes = fetch_twstock_quote_batch(['2330', '2317'], headers)
# {'2330': {...}, '2317': {...}}
```

**Notes:**
- `buy_price`/`sell_price` are best bid/ask, not last-traded price — use `close` for last price.
- `tick_type`: `0` = indeterminate, `1` = sell-initiated (賣盤成交), `2` = buy-initiated (買盤成交).
- `quote_time` is a full timestamp (`YYYY-MM-DD HH:MM:SS`), unlike every other twstock
  function's `date`, which is a bare calendar day — don't treat it as a date-only field.
- No local caching by design — the server enforces a 10s Redis TTL, so calling this
  repeatedly in a loop is fine and will pick up fresh data every ~10s; do not add your own
  parquet/file cache on top, it would defeat the purpose.
- Do not use for backtesting — no history exists. Use `fetch_twstock_price`/`fetch_twstock_price_adj` instead.

---

## Minute-Line OHLCV（現股分線）— preferred

Use `fetch_twstock_ohlcv` / `fetch_twstock_ohlcv_symbols` in `lib/data.py` — do not
hand-roll requests (the lib layer adds retry, chunking, monthly parquet cache, and
OHLC sanity checks).

- `fetch_twstock_ohlcv(stock_id, schema, headers, start=None, end=None, adjust=False)` →
  DataFrame with Open/High/Low/Close/Volume, UTC index (Asia/Taipei for `1d`).
  `schema`: `'1m'`/`'5m'`/`'15m'`/`'30m'`/`'60m'`/`'1d'`. **Volume is in lots (張),
  not shares.** Bars carry minute-START labels; the 13:30 Taipei bar is the closing
  auction. History from 2019-01. `adjust=True` returns forward-adjusted (後復權)
  OHLC — use for backtests spanning ex-dividend dates; same factor pipeline as
  `fetch_twstock_price_adj`, volume unchanged; the server returns 503 (fail-loud)
  if adjustment factors are unavailable, never silently raw prices.
- `fetch_twstock_ohlcv_symbols(headers)` → list of stock_ids that already have
  minute-line data server-side.

**Coverage.** The whole market is backfilled server-side from 2019-01; requesting an
earlier `start` is silently clamped to 2019-01-01. Ongoing tracking per stock:
intraday real-time bars + a daily official correction after market close. Only very
newly listed stocks are demand-driven: seeded on their first query and queued for
deep backfill, so that query may return only recent data — full history usually
lands by the next day. Empty past months are cached locally with a 24-hour TTL
(not permanently), so the cache self-heals once the server has the data.

```python
df = fetch_twstock_ohlcv('2330', '5m', headers, start='2025-01-01', end='2025-06-30')
```

---

## 台股分K（1分鐘 OHLCV）— legacy endpoint

> Superseded for most uses by the minute-line section above (`fetch_twstock_ohlcv`
> supports 1m–60m/1d resampling, caching, and retry). Kept for reference only.

```python
import requests

def fetch_twstock_kbar(stock_id: str, start: str, end: str, headers: dict) -> pd.DataFrame:
    """最多 31 天，資料從 2019-01-01 起。欄位：date, minute, open, high, low, close, volume"""
    r = requests.get(
        f"https://api.blave.org/studio/market/twstock/kbar/{stock_id}",
        params={"start": start, "end": end},
        headers=headers,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["datetime"] = pd.to_datetime(df["date"] + " " + df["minute"])
    return df.set_index("datetime").sort_index()
```

**注意事項：**
- `minute` 格式為 `HH:MM:SS`（例如 `09:00:00`）
- 一天約 266 筆（09:00–13:25，每分鐘一筆）
- 非交易日（週末、假日）自動略過，不返回資料
- 超過 31 天會回 400 錯誤；長期回測需分段呼叫

---

## 台股新聞

每天多篇，`date` 為 datetime 字串（含時間）。最多 31 天。

```python
def fetch_twstock_news(stock_id: str, start: str, end: str, headers: dict) -> pd.DataFrame:
    """欄位：date（datetime string）, title, source, link"""
    r = requests.get(
        f"https://api.blave.org/studio/market/twstock/news/{stock_id}",
        params={"start": start, "end": end},
        headers=headers,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return pd.DataFrame(data) if data else pd.DataFrame()
```

---

## 市值

```python
def fetch_twstock_market_value(stock_id: str, start: str, end: str, headers: dict) -> pd.DataFrame:
    """欄位：date, market_value（元，NTD）。資料從 2004-01-01 起。"""
    r = requests.get(
        f"https://api.blave.org/studio/market/twstock/market_value/{stock_id}",
        params={"start": start, "end": end},
        headers=headers,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return pd.DataFrame(data).set_index("date") if data else pd.DataFrame()
```

---

## 八大行庫買賣超

每天 8 筆（一家銀行一筆）。FinMind 不支援單股查詢，後台會拉整天全市場資料再 filter，**最多 31 天**。

```python
def fetch_twstock_gov_bank(stock_id: str, start: str, end: str, headers: dict) -> pd.DataFrame:
    """欄位：date, bank_name, buy（張）, buy_amount（元）, sell（張）, sell_amount（元）"""
    r = requests.get(
        f"https://api.blave.org/studio/market/twstock/gov_bank/{stock_id}",
        params={"start": start, "end": end},
        headers=headers,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return pd.DataFrame(data) if data else pd.DataFrame()
```

常用分析：
```python
df = fetch_twstock_gov_bank("2330", "2025-01-01", "2025-01-31", hdrs)

# 每日八大行庫合計淨買超（張）
daily_net = df.groupby("date").apply(lambda x: (x["buy"] - x["sell"]).sum())

# 各銀行累計淨買超
bank_net = (df.groupby("bank_name")["buy"].sum() - df.groupby("bank_name")["sell"].sum())
```

---

## PE / PB / 殖利率

用 `fetch_twstock_per(stock_id, start, end, headers)`（見 `lib/data.py`）—— DatetimeIndex
（與價格／法人等 fetcher 對齊，可直接 reindex），欄位 `dividend_yield`、`PER`、`PBR`，
資料從 2005-10-01 起。

```python
from lib.data import fetch_twstock_per

per = fetch_twstock_per("2408", "2026-01-01", "2026-07-28", hdrs)
latest = per.iloc[-1]        # dividend_yield / PER / PBR
```

當日資料在盤後直接取自 TWSE／TPEx 官方報表；上櫃比上市晚幾小時發布，未發布時退回 FinMind
（會慢一天）。ETF 沒有 PE 資料——官方報表與 FinMind 都不含，回傳空 DataFrame。

多支股票（價值選股）用 `fetch_twstock_per_batch(stock_ids, start, end, headers)`，見下方 Batch 資料函式。

---

## Dividend Events (股利事件)

Per-stock dividend event history — cash/stock amounts plus record/announce/ex/pay dates.
Use `fetch_twstock_dividend(stock_id, start, end, headers)`; for many stocks use
`fetch_twstock_dividend_batch(stock_ids, start, end, headers)` → `{stock_id: DataFrame}`.

```python
from lib.data import fetch_twstock_dividend

div = fetch_twstock_dividend("2330", "2025-01-01", None, hdrs)
# columns: record_date, period, announce_date, cash_ex_date, stock_ex_date,
#          pay_date, cash, stock, stock_ratio   (RangeIndex — event rows, not a series)
upcoming = div[div["cash_ex_date"] > pd.Timestamp.now().strftime("%Y-%m-%d")]
```

Notes:
- `period` is an **opaque label** (`114年第3季`, `113`, `不適用`, …) — group/compare by
  string, never parse it into a Western year.
- Empty date fields are `''` (empty string), never NaN. An announced event whose ex date
  is not decided yet has `cash_ex_date == ''` — range queries still surface it (the
  filter falls back to `stock_ex_date`, then `record_date`).
- Zero-value rows (`cash == 0 and stock == 0`) are announced **no-distribution** decisions
  and are kept — do not treat them as missing data.
- Unknown / delisted ids and stocks with no dividend history return an empty DataFrame;
  batch omits them silently.
- Full history per stock is cached with a 1-day TTL and sliced locally — repeat calls
  with different ranges are free within the day.

---

## 借券成交明細

每天多筆，`transaction_type` 為 `競價` 或 `議借`，每筆費率/張數不同。

```python
def fetch_twstock_lending(stock_id: str, start: str, end: str, headers: dict) -> pd.DataFrame:
    """欄位：date, transaction_type, volume, fee_rate, close, original_return_date, original_lending_period"""
    r = requests.get(
        f"https://api.blave.org/studio/market/twstock/lending/{stock_id}",
        params={"start": start, "end": end},
        headers=headers,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return pd.DataFrame(data) if data else pd.DataFrame()
```

常用分析：
```python
df = fetch_twstock_lending("2330", "2025-01-01", "2025-05-30", hdrs)

# 每日借券總量
daily_volume = df.groupby("date")["volume"].sum()

# 競價 vs 議借 分布
by_type = df.groupby(["date", "transaction_type"])["volume"].sum().unstack(fill_value=0)

# 加權平均費率（借券成本）
df["value"] = df["volume"] * df["fee_rate"]
avg_fee = df.groupby("date").apply(lambda x: x["value"].sum() / x["volume"].sum())
```

---

## Market-wide data (大盤)

Whole-market series — no `stock_id` dimension. Use these for index level, market breadth /
turnover, and market-wide institutional or margin flows; the per-stock `fetch_twstock_*`
functions above answer a different question and must not be summed as a substitute.

```python
from lib.data import (
    fetch_twmarket_index,          # (start, end, headers, index_id='TAIEX') → Open/High/Low/Close
    fetch_twmarket_turnover,       # (start, end, headers) → volume, value, trades
    fetch_twmarket_institutional,  # (start, end, headers) → foreign, investment_trust, dealer, total
    fetch_twmarket_margin,         # (start, end, headers) → margin/short balances
)

taiex = fetch_twmarket_index("2024-01-01", "2026-07-28", hdrs)   # DatetimeIndex, daily
```

| Function | Columns | Units | Since |
|---|---|---|---|
| `fetch_twmarket_index` | `Open` `High` `Low` `Close` | index points | 1999-01-05 |
| `fetch_twmarket_turnover` | `volume` `value` `trades` | shares / TWD / count | 1990-01-04 |
| `fetch_twmarket_institutional` | `foreign` `investment_trust` `dealer` `total` | TWD, net (buy - sell) | 2004-04-07 |
| `fetch_twmarket_margin` | `margin_balance` `margin_balance_prev` `margin_balance_value` `short_balance` `short_balance_prev` | lots (張), except `margin_balance_value` in TWD | 2001-01-03 |

Notes:
- `TAIEX` is the only supported `index_id`; any other value returns 400. The index carries no
  volume column — market turnover comes from `fetch_twmarket_turnover`, keyed on the same dates.
- In `fetch_twmarket_institutional`, 外資自營商 (foreign dealers' own account) is counted in
  `dealer`, not in `foreign` — the same bucketing FinMind uses.
- Margin balances are whole-market; `margin_balance_value` is the only TWD column, the rest are lots.
- TXO put/call ratio is a futures/options dataset — see `fetch_twfutures_pcr` in
  `references/twfutures.md`, not here.
- TAIEX daily index dividend points (`fetch_twmarket_dividend_points` — realized +
  forward estimates, the correction term for TXF basis math) is documented in
  `references/twfutures.md` › Index Dividend Points, since its main consumer is
  futures fair-basis logic.

---

## Batch 資料函式

所有台股資料一律用 batch 函式（即使只有 1 支），回傳 `dict {stock_id: DataFrame}`，超過 50 支自動切塊：

```python
from lib.data import (
    fetch_twstock_price_adj_batch,              # (stock_ids, start, end, headers) → Open/Close
    fetch_twstock_price_batch,                  # (stock_ids, start, end, headers) → 原始日K OHLCV（含 High/Low）
    fetch_twstock_per_batch,                    # (stock_ids, start, end, headers) → dividend_yield/PER/PBR
    fetch_twstock_institutional_batch,          # (stock_ids, start, end, headers) → foreign_net 及原始欄位
    fetch_twstock_shareholding_batch,           # (stock_ids, start, end, headers) → shareholders 欄
    fetch_twstock_foreign_shareholding_batch,   # (stock_ids, start, end, headers) → 外資持股比率/股數
    fetch_twstock_financials_batch,             # (stock_ids, headers) → 損益表 long format
    fetch_twstock_balance_sheet_batch,          # (stock_ids, headers) → 資產負債表 long format
    fetch_twstock_monthly_revenue_batch,        # (stock_ids, headers) → revenue, revenue_month, revenue_year
)
```

`fetch_twstock_foreign_shareholding_batch` 主要欄位：

| 欄位 | 說明 |
|---|---|
| `ForeignInvestmentSharesRatio` | 外資持股比率（%） |
| `ForeignInvestmentShares` | 外資持股股數 |
| `ForeignInvestmentRemainRatio` | 外資剩餘可投資比率（%） |
| `ForeignInvestmentRemainingShares` | 外資剩餘可投資股數 |
| `NumberOfSharesIssued` | 已發行股數 |

分點資料（非 batch，按 trader 維度）：
- `fetch_twstock_trader_flows(trader_id, start, end, headers)` → MultiIndex (date, stock_id)，`net` 欄（買 - 賣股數）；trader_id 例如 `'9217'`（凱基-松山）

---

## 全市場選股（Screening）

**絕不對多支股票 fan-out 單檔 fetcher（含自開 ThreadPool 平行打）** —— 單檔 endpoint 有
rate limit，300 支就會 429 退避到分鐘級；上表的 batch 函式一次 50 支、全市場約 40 個請求。

流程走漏斗，先縮池再拉時間序列（實測 uid=1 機器）：

1. **縮池（秒級）**：`fetch_twstock_list`（產業別；注意 list **沒有市值欄**——市值在逐股的
   `/market_value/` 端點，只能對縮完的池子逐支拉，不能拿來當第一層濾網）、`fetch_twstock_quote_batch`
   （全市場漲幅/量比約 24s）、`fetch_twstock_monthly_revenue_batch`／`fetch_twstock_per_batch`
   （基本面/價值條件）→ 縮到幾百支。
2. **時間序列條件（每百支約 10–30s）**：對縮完的池子用
   `fetch_twstock_price_batch`（KD/突破等需 High/Low 的技術條件）、
   `fetch_twstock_price_adj_batch`（均線/報酬類）、`fetch_twstock_institutional_batch`（法人連買）。
3. 全市場直接拉時間序列（不縮池）一次約 2–3 分鐘——用戶明確要全市場掃描才這樣做，
   並先講清楚要等多久。

---

## 財報因子計算

財報資料為 long format，先 pivot 再計算：

```python
fin_all = fetch_twstock_financials_batch(universe, hdrs)     # {sid: df}
bs_all  = fetch_twstock_balance_sheet_batch(universe, hdrs)
rev_all = fetch_twstock_monthly_revenue_batch(universe, hdrs)

for sid in universe:
    fin = fin_all[sid].pivot_table(index='date', columns='type', values='value', aggfunc='last')
    bs  = bs_all[sid].pivot_table(index='date', columns='type', values='value', aggfunc='last')
    rev = rev_all[sid]

    roe          = fin['IncomeAfterTaxes'] / bs['Equity']    # ROE
    gross_margin = fin['GrossProfit'] / fin['Revenue']       # 毛利率
    eps_yoy      = fin['EPS'].pct_change(4)                  # EPS YoY（同季比）
    rev_yoy      = rev['revenue'].pct_change(12)             # 月營收 YoY
```

損益表 key types：`Revenue`、`GrossProfit`、`OperatingIncome`、`IncomeAfterTaxes`、`EPS`

資產負債表 key types：`TotalAssets`、`Equity`

---

## Lookahead Bias — 財報可用日期

| 季別 | 財報公告截止日 | 策略可用日 |
|------|-------------|-----------|
| Q1（1–3月） | 5/15 | 5/16 起 |
| Q2（4–6月） | 8/14 | 8/15 起 |
| Q3（7–9月） | 11/14 | 11/15 起 |
| Q4（10–12月） | 翌年 3/31 | 翌年 4/1 起 |
