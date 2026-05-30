# 台灣期貨資料 — Taiwan Futures Data

> 期貨日行情由 [FinMind](https://finmindtrade.com) 提供。

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
