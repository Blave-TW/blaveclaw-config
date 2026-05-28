# 台股資料 — Taiwan Stock Data

## 股票池（Universe）建立

使用 TWSE 公開 API（無需認證）：

```python
import requests

r = requests.get('https://openapi.twse.com.tw/v1/opendata/t187ap03_L', timeout=15)

# 全部上市普通股（排除 ETF / 權證）
universe = [item['公司代號'].strip() for item in r.json()]

# 依產業別篩選（例如半導體 '20' + 電腦及周邊 '21'）
tech = [item['公司代號'].strip() for item in r.json() if item['產業別'] in ('20', '21')]
```

欄位：`公司代號`（需 `.strip()`）、`公司簡稱`、`產業別`、`上市日期`（YYYYMMDD）

產業別代碼速查：`20` 半導體、`21` 電腦及周邊、`22` 光電、`23` 通信網路、`24` 電子零組件、`25` 電子通路、`26` 資訊服務、`27` 其他電子、`33` 金融保險、`31` 航運

---

## Batch 資料函式

所有台股資料一律用 batch 函式（即使只有 1 支），回傳 `dict {stock_id: DataFrame}`，超過 50 支自動切塊：

```python
from lib.data import (
    fetch_twstock_price_adj_batch,        # (stock_ids, start, end, headers) → Open/Close
    fetch_twstock_institutional_batch,    # (stock_ids, start, end, headers) → foreign_net 及原始欄位
    fetch_twstock_shareholding_batch,     # (stock_ids, start, end, headers) → shareholders 欄
    fetch_twstock_financials_batch,       # (stock_ids, headers) → 損益表 long format
    fetch_twstock_balance_sheet_batch,    # (stock_ids, headers) → 資產負債表 long format
    fetch_twstock_monthly_revenue_batch,  # (stock_ids, headers) → revenue, revenue_month, revenue_year
)
```

分點資料（非 batch，按 trader 維度）：
- `fetch_twstock_trader_flows(trader_id, start, end, headers)` → MultiIndex (date, stock_id)，`net` 欄（買 - 賣股數）；trader_id 例如 `'9217'`（凱基-松山）

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
