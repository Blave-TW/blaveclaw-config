# SinoPac (永豐金) Broker — Agent Reference

Use this document when a user asks to connect their SinoPac account to BlaveClaw.

---

## Supported Products

| Product | Symbol Example | Notes |
|---------|---------------|-------|
| 台灣期貨 TXF | `TXFR1` | 台指期近月，1 口 = 200 × 指數點位 (TWD) |
| 台灣股票 Stocks | `2330` (TSE) | 最小單位 1 張 = 1000 股 |

---

## Step 0 — Determine Account Type

**Ask the user first:**
> 你要交易台灣期貨（台指期 TXF）還是台灣股票？或兩者都要？

- 期貨帳號 → 查 `futopt_account`，保證金用 `margin()` → `equity`
- 股票帳號 → 查 `stock_account`，帳戶餘額用 `account_balance()`
- 大多數用戶同一個帳號就有兩種

---

## Step 1 — Apply for API Key

**URL:** https://eservice.sinotrade.com.tw/  
(永豐金證券 e-MANAGER 開發人員中心)

**Steps:**
1. 登入永豐金 e-MANAGER（需要有效的永豐金帳號）
2. 進入「API 金鑰管理」
3. 點「申請 API 金鑰」
4. 記錄下 `API Key` 和 `Secret Key`（頁面離開後 Secret Key 不再顯示）
5. **數位憑證（正式環境必須）：**
   - 下載並安裝永豐金 CA 憑證
   - 記住憑證密碼（`sinopac_ca_passwd`）
   - 憑證檔案路徑通常在 `~/.shioaji/` 或用戶指定位置

**Simulation Mode:** 不需要 CA 憑證，可先用 `simulation=True` 測試連線。

---

## Step 2 — Collect Credentials & Write `.env`

**Ask the user (in one message):**
> 請提供你的永豐 API Key 和 Secret Key（從 e-MANAGER 取得）。
> 如果你有數位憑證，也請告訴我憑證檔案的路徑和密碼。

收到後，agent 直接寫入 `.env`，不需要用戶自行編輯：

```python
# Agent 執行：append keys to .env
with open('.env', 'a') as f:
    f.write(f"\nsinopac_api_key={api_key}\n")
    f.write(f"sinopac_secret_key={secret_key}\n")
    if ca_path:
        f.write(f"sinopac_ca_path={ca_path}\n")
        f.write(f"sinopac_ca_passwd={ca_passwd}\n")
```

確認寫入後告知用戶金鑰已存好，不會再顯示。

---

## Step 3 — Test Connection

用 `simulation=True` 驗證帳號：

```python
import shioaji as sj
from dotenv import dotenv_values

env = dotenv_values('.env')
api = sj.Shioaji(simulation=True)
accounts = api.login(
    api_key=env['sinopac_api_key'],
    secret_key=env['sinopac_secret_key'],
    fetch_contract=True,
)
print("Accounts:", accounts)
print("Stock account:", api.stock_account)
print("Futures account:", api.futopt_account)
api.logout()
```

**成功**：印出 accounts list，包含 stock/futopt 帳號。  
**失敗常見原因：** API Key 未開通、密碼錯誤、系統時間偏移（誤差 > 30 秒會 timeout）。

測試正式環境（加 CA cert）：
```python
api = sj.Shioaji(simulation=False)
accounts = api.login(
    api_key=env['sinopac_api_key'],
    secret_key=env['sinopac_secret_key'],
    ca_path=env.get('sinopac_ca_path'),
    ca_passwd=env.get('sinopac_ca_passwd'),
    fetch_contract=True,
)
```

---

## Step 4 — Check Account Equity & Positions

```python
# 期貨帳戶淨值
margin = api.margin(account=api.futopt_account)
print("Futures equity:", margin.equity, "TWD")

# 股票帳戶餘額
balance = api.account_balance(account=api.stock_account)
print("Stock balance:", balance[0].acc_balance if balance else 0, "TWD")

# 查部位
fut_positions = api.list_positions(api.futopt_account)
stk_positions = api.list_positions(api.stock_account)
print("Futures positions:", fut_positions)
print("Stock positions:", stk_positions)
```

---

## Step 5 — Wire into Portfolio

在 `portfolio_config.json` 的 `exchanges` 加入 sinopac 路由：

```json
{
  "account_value": 500000,
  "exchanges": {
    "txf_strategy": "sinopac",
    "tsmc_strategy": "sinopac"
  },
  "asset_specs": {
    "txf_strategy": {
      "type": "futures_contracts",
      "contract_value": 200,
      "currency": "TWD",
      "lot_size": 1,
      "sinopac_symbol": "TXFR1"
    },
    "tsmc_strategy": {
      "type": "tw_stock",
      "lot_size": 1000,
      "currency": "TWD",
      "sinopac_symbol": "2330"
    }
  }
}
```

---

## Trading Hours

| Market | Hours (台灣時間) |
|--------|-----------------|
| 台灣股票 | 09:00 – 13:30 (Mon–Fri) |
| 台指期日盤 | 08:45 – 13:45 (Mon–Fri) |
| 台指期夜盤 | 15:00 – 05:00 (Mon–Fri) |

---

## Order Types

### TXF 期貨委託
```python
contract = api.Contracts.Futures.TXF.TXFR1  # 近月合約
order = api.FuturesOrder(
    action="Buy",       # "Buy" or "Sell"
    price=0,            # 0 for market order
    quantity=1,         # 口數
    price_type="MKT",   # MKT = 市價
    order_type="IOC",   # IOC for market orders
    octype="Auto",      # Auto = 系統自動判斷開/平倉; "New" = 強制開倉; "Cover" = 強制平倉
)
trade = api.place_order(contract, order)
```

### 股票委託
```python
contract = api.Contracts.Stocks.TSE["2330"]
order = api.Order(
    action="Buy",       # "Buy" or "Sell"
    price=0,            # 0 for market order (需確認是否開放)
    quantity=1,         # 張數（1 張 = 1000 股）
    price_type="MKT",
    order_type="IOC",
)
trade = api.place_order(contract, order)
```

---

## Limits & Gotchas

| Limit | Value |
|-------|-------|
| 最大並發連線 | 5 個 |
| 報價查詢速率 | 50 次 / 5 秒 |
| 委託速率 | 250 次 / 10 秒 |
| 超速處置 | 暫停 1 分鐘 |
| 時間誤差上限 | 30,000 ms（超過登入 timeout） |

**注意事項：**
- 模擬模式 (`simulation=True`) 無法收到委託回報 callbacks；正式帳號才有
- `fetch_contract=True` 登入時下載所有合約資料（約 5-10 秒），必須等完成才能查合約
- 同一帳號同時只能有一個 Shioaji 連線；多程序需各自 login/logout
- 系統時間必須準確；NTP 同步或手動校正可解決 timeout 問題
- 夜盤台指期結算後隔天會換近月合約代碼，`TXFR1` 永遠指向最近月（自動 roll）

---

## Verification Checklist for Agent

在確認用戶完成設定後，依序執行：
1. 連線測試（simulation mode）→ 看到 accounts list
2. 查詢帳戶淨值 → 確認 equity > 0
3. 查詢部位 → 確認無例外
4. 執行 `python3 manager/snapshot.py` → Telegram 收到含 sinopac equity 的日報
5. 用戶確認後，方可設定 reconciler 上線（參考 `references/deployment.md`）
