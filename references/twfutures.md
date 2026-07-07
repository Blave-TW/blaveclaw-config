# 台灣期貨資料 — Taiwan Futures Data

> 期貨日行情由 [FinMind](https://finmindtrade.com) 提供。

---

## 選擇權大額交易人（TaiwanOptionOpenInterestLargeTraders）

每天 6 筆：`put_call`（call/put）× `contract_type`（week/近月/all）。`option_id` 通常為 `TXO`。

```python
def fetch_twoption_large_traders(option_id: str, start: str, end: str, headers: dict) -> pd.DataFrame:
    """欄位：date, option_id, put_call, contract_type, buy/sell_top5/top10_trader_open_interest(_per), market_open_interest"""
    r = requests.get(
        f"https://api.blave.org/studio/market/twfutures/option/large_traders/{option_id}",
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
df = fetch_twoption_large_traders("TXO", "2025-01-01", "2025-05-30", hdrs)

# all contracts Put/Call Ratio（大戶買方未平倉）
all_c = df[df["contract_type"] == "all"].copy()
pivot = all_c.groupby(["date", "put_call"])["buy_top10_trader_open_interest"].sum().unstack()
pcr = pivot["put"] / pivot["call"]   # > 1 偏空

# 外資 call net（用法人資料搭配）
call_oi = all_c[all_c["put_call"] == "call"].set_index("date")
net = call_oi["buy_top5_trader_open_interest"] - call_oi["sell_top5_trader_open_interest"]
```

---

## 期貨大額交易人（TaiwanFuturesOpenInterestLargeTraders）

每天 3 筆：`contract_type` = week（當週到期）、近月（如 202505）、all（全部合約）。

```python
def fetch_twfutures_large_traders(futures_id: str, start: str, end: str, headers: dict) -> pd.DataFrame:
    """欄位：date, futures_id, name, contract_type, buy/sell_top5/top10_trader_open_interest(_per), market_open_interest, buy/sell_top5/top10_specific_open_interest(_per)"""
    r = requests.get(
        f"https://api.blave.org/studio/market/twfutures/large_traders/{futures_id}",
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
df = fetch_twfutures_large_traders("TX", "2025-01-01", "2025-05-30", hdrs)

# 近月合約大戶買賣超（top5 多 - 空）
front = df[df["contract_type"].str.len() == 6]  # 202505 格式
front["net_top5"] = front["buy_top5_trader_open_interest"] - front["sell_top5_trader_open_interest"]
net_series = front.set_index("date")["net_top5"]

# 大戶持倉比例（all contracts）
all_c = df[df["contract_type"] == "all"].set_index("date")
buy_per = all_c["buy_top10_trader_open_interest_per"]
sell_per = all_c["sell_top10_trader_open_interest_per"]
```

---

## 選擇權三大法人（TaiwanOptionInstitutionalInvestors）

每天 6 筆：3 法人 × 買權/賣權。`option_id` 通常為 `TXO`。

```python
def fetch_twoption_institutional(option_id: str, start: str, end: str, headers: dict) -> pd.DataFrame:
    """欄位：date, option_id, call_put（買權/賣權）, institutional_investors, long/short_deal_volume/amount, long/short_open_interest_balance_volume/amount"""
    r = requests.get(
        f"https://api.blave.org/studio/market/twfutures/option/institutional/{option_id}",
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
df = fetch_twoption_institutional("TXO", "2025-01-01", "2025-05-30", hdrs)

# 外資買權淨未平倉
call_df = df[(df["call_put"] == "買權") & (df["institutional_investors"] == "外資")]
call_net_oi = (call_df.set_index("date")["long_open_interest_balance_volume"]
               - call_df.set_index("date")["short_open_interest_balance_volume"])

# Put/Call Ratio（外資）
foreign = df[df["institutional_investors"] == "外資"].groupby(["date", "call_put"])["long_open_interest_balance_volume"].sum().unstack()
pcr = foreign["賣權"] / foreign["買權"]
```

---

## 期貨三大法人（TaiwanFuturesInstitutionalInvestors）

每天 3 筆：自營商、投信、外資。

```python
def fetch_twfutures_institutional(futures_id: str, start: str, end: str, headers: dict) -> pd.DataFrame:
    """欄位：date, institutional_investors, long/short_deal_volume/amount, long/short_open_interest_balance_volume/amount"""
    r = requests.get(
        f"https://api.blave.org/studio/market/twfutures/institutional/{futures_id}",
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
df = fetch_twfutures_institutional("TX", "2025-01-01", "2025-05-30", hdrs)

# 外資淨未平倉（多 - 空）
foreign = df[df["institutional_investors"] == "外資"].copy()
foreign["net_oi"] = foreign["long_open_interest_balance_volume"] - foreign["short_open_interest_balance_volume"]
foreign_net = foreign.set_index("date")["net_oi"]
```

---

## 期貨日行情（TaiwanFuturesDaily）

每天多筆：所有合約月份 × trading_session（`position` 盤中 / `after_market` 盤後）。

```python
import requests
import pandas as pd

def fetch_twfutures_daily(futures_id: str, start: str, end: str, headers: dict) -> pd.DataFrame:
    """
    futures_id: TX（台指期）、MTX（小台）、TE（電子期）、TF（金融期）
    欄位：date, futures_id, contract_date, open, max, min, close,
          spread, spread_per, volume, settlement_price, open_interest, trading_session
    """
    r = requests.get(
        f"https://api.blave.org/studio/market/twfutures/daily/{futures_id}",
        params={"start": start, "end": end},
        headers=headers,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return pd.DataFrame(data) if data else pd.DataFrame()
```

常用篩選：

```python
df = fetch_twfutures_daily("TX", "2025-01-01", "2025-05-30", hdrs)

# 只取近月合約（position session，排除價差）
front_month = (
    df[(df["trading_session"] == "position") & (~df["contract_date"].str.contains("/"))]
    .sort_values(["date", "volume"], ascending=[True, False])
    .groupby("date")
    .first()   # 成交量最大 = 近月
    .reset_index()
)

# 近月收盤價序列
close_series = front_month.set_index("date")["close"]

# 未平倉量（open interest）
oi_series = front_month.set_index("date")["open_interest"]
```

**注意事項：**
- `contract_date` 含 `/` 的為價差合約（e.g. `202505/202506`），通常過濾掉
- `trading_session = "position"` 為正規盤，`"after_market"` 為盤後交易
- 近月連續序列需自行用成交量或到期日判斷；TXF 分K 仍使用 `/twfutures/ohlcv/TXF/<schema>`

---

## Stock Futures Batch Daily (個股期貨)

231 Taiwan individual stock futures (股票期貨) — one contract per underlying listed common stock (e.g. `CDF` → 2330 台積電).

```python
from lib.data import fetch_stock_futures_batch_daily

batch = fetch_stock_futures_batch_daily(['CDF', 'DHF'], "2025-01-01", "2025-05-30", hdrs)
# dict {futures_id: DataFrame}, same fields as fetch_twfutures_daily (see above)
```

**Notes:**
- `fetch_twfutures_daily(futures_id, ...)` (the generic function documented above) also works for any individual stock futures id — `fetch_stock_futures_batch_daily` is just the parallel/batch form (max 250 ids per call), matching `fetch_twstock_batch` for stocks.
- **Daily OHLCV covers all 231 symbols.** Intraday/minute bars (`fetch_twfutures_ohlcv(symbol, schema, ...)`) do NOT — that same function also accepts individual stock futures ids as `symbol`, but only `TXF` plus whichever ones currently have backfilled minute-line data (a dynamically-growing subset, nowhere near all 231). Passing an unsupported symbol gets a 400.
- **Check `fetch_stock_futures_ohlcv_symbols(hdrs)` first** to get the current list of symbols supported by `fetch_twfutures_ohlcv` — no need to trial-and-error against the 400:
```python
from lib.data import fetch_stock_futures_ohlcv_symbols, fetch_twfutures_ohlcv

symbols = fetch_stock_futures_ohlcv_symbols(hdrs)  # e.g. ['CDF', 'TXF', ...]
if 'CDF' in symbols:
    df = fetch_twfutures_ohlcv('CDF', '1m', "2025-01-01", "2025-01-31", hdrs)
```
- **Long-history intraday fetches are cheap** — for intraday spans >62 days, `fetch_twfutures_ohlcv` automatically switches to the server's 1m-parquet bulk export endpoint (`/studio/market/twfutures/ohlcv/<symbol>/export/<year>`, one request per calendar year, resampled locally to the requested schema) instead of chunked 28-day JSON fetches. Falls back to chunked fetches automatically if the export endpoint is unavailable. Fetching 100 symbols × multi-year 60m is a few hundred requests, not thousands.
- The direct JSON endpoint's per-request date-range caps are schema-scaled: `1d` 3,650 / `60m` 365 / `30m` 186 / `15m` 93 / `5m` 62 / `1m` 31 days. The lib's auto-chunking already respects these — only relevant when calling the API directly.

---

## Option Put/Call Ratio (TaiwanOptionPutCallRatio)

Official TAIFEX daily put/call open-interest ratio (OI-based PCR), one row per day.

```python
from lib.data import fetch_twfutures_pcr

df = fetch_twfutures_pcr("2024-01-01", "2024-12-31", hdrs)
# index: date (daily, trading days only)
# column: pcr (買賣權未平倉量比率%, float)
```

**Notes:**
- This is the official PCR endpoint (`/studio/market/twfutures/option/pcr`). It is NOT the same as the `put/call` ratio computed by hand from `fetch_twoption_large_traders` / `fetch_twoption_institutional` above. Use this function when you need the official value — do not recompute it.
- `pcr > 100` (ratio > 1.0) tends to be bearish, `< 100` bullish, but in practice read the relative level and trend rather than an absolute threshold.

---

## Bid/Ask Volume (TaiwanFuturesBidAskVolume)

TXF 1-minute bid/ask volume aggregated from tick data, including both day and night sessions (backfilled history; earliest date — see the blave-quant skill). Max 31 days per request — the lib auto-chunks.

```python
from lib.data import fetch_twfutures_bid_ask_vol

df = fetch_twfutures_bid_ask_vol("2024-01-01", "2024-03-31", hdrs)
# index: UTC time (1-minute bars)
# columns: bid_vol (內盤, seller-initiated), ask_vol (外盤, buyer-initiated), total_vol (incl. unclassified)
```

**Notes:**
- bid_vol = 內盤 (seller-initiated / 主動賣), ask_vol = 外盤 (buyer-initiated / 主動買). `ask_vol - bid_vol` is the net aggressive-buy pressure.
- Includes both day (08:45–13:45 TWN) and night (15:00–next day 05:00 TWN) sessions.
- Auto-chunked at 31 days per request — no need to split manually.
