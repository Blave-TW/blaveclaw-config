# 台灣期貨資料 — Taiwan Futures Data

> 期貨日行情由 [FinMind](https://finmindtrade.com) 提供。

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
