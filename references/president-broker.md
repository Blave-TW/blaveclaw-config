# President Futures (統一期貨) Broker — Agent Reference

Use this document when a user asks to connect their President Futures (統一期貨) account to
BlaveClaw. The integration uses the official **Unitrade API** (`pip install unitrade`), a
cross-platform Python package (Linux/Windows/macOS) published by President Futures.

Package docs: https://pfcec.github.io/unitrade/ · PyPI: https://pypi.org/project/unitrade/

---

## Supported Products

| Product | Symbol Example | Notes |
|---------|---------------|-------|
| 台灣期貨 TXF | dynamic, e.g. `TXFG6` | 台指期近月，1 口 = 200 × 指數點位 (TWD). No static "always-near-month" alias — the near-month contract id must be looked up each session (see Step 6). |
| Other domestic futures/options | via `get_domestic_contracts` | Same account also trades other TAIFEX domestic products; not yet covered by an `asset_specs` template in this doc — extend by analogy to TXF if a user asks. |

This doc only covers the **domestic futures (國內期貨) account** — Unitrade also exposes overseas
futures (`api.ftrade`/`api.faccount`) and TWSE-listed stocks (`api.strade`, not yet explored here).

---

## Step 0 — Determine Scope

**Ask the user first:**
> 你要交易台指期（TXF）還是其他期貨/選擇權商品？

Most users onboarding for the first time want TXF only — the rest of this doc assumes that.

---

## Step 1 — Apply for API Access

1. User contacts their President Futures broker rep (營業員) and requests **統一API 測試環境開通**.
2. The rep emails a **VIP_API_測試 帳號啟用通知** containing:
   - Test environment login URL (e.g. `https://testNNN.pfctrade.com`) — **use exactly the URL in the email, do not guess or reuse an old one**, PFCF rotates test hosts.
   - Confirmation that the trading password doubles as the test-environment login password.
3. **The test environment requires a real digital certificate — there is no certificate-free simulation mode** (unlike SinoPac). Proceed to Step 2 before attempting login.

---

## Step 2 — Obtain the Certificate (.pfx)

**This step requires a Windows machine — the certificate issuance tool is Windows-only.** The
resulting `.pfx` file itself is platform-agnostic and works fine when uploaded to the Linux
BlaveClaw workspace; only *obtaining* it is the friction point.

**Ask the user:**
> 你之前有沒有用過統一期貨的下單軟體、或申請過電腦憑證？

- **If yes:** the `.pfx` is likely already on their Windows PC at
  `C:\Users\<username>\PSCCA\PSC_<ID>_<expiry>.pfx`. They just need to locate and send it.
- **If no:** they must run **憑證e總管** (Windows tool, download via https://pki.pscnet.com.tw/)
  to request a new certificate. There is no confirmed macOS/Linux path for *issuing* a new
  certificate — a web-based portal exists at the same URL but its cross-platform completeness is
  unconfirmed; tell the user to ask their broker rep if they have no Windows access at all.
- **Certificate password is separate from the trading/login password** — it was set at
  certificate-issuance time. If the user never set one, it may be blank.
- **Certificates expire after 1 year** and must be renewed via the same Windows tool. Warn the
  user this is a recurring (not one-time) step.

**Ask the user (in one message) once they have the file:**
> 請把你的憑證檔案（.pfx）透過 Telegram 傳給我，並告訴我憑證密碼（如果沒設定就跟我說沒有）。

Agent saves the uploaded file to a fixed workspace path, e.g. `certs/president.pfx` — never print
its contents or path aloud beyond confirming it was saved.

---

## Step 3 — Install the Unitrade Package

```bash
pip install unitrade
```

**Platform notes (confirmed by testing):**
- PyPI ships wheels for **Linux x86_64 and Windows**, Python **3.7–3.12**. No `aarch64`/ARM Linux
  wheel, no `cp313`/`cp314` wheel as of this writing.
- BlaveClaw's default Linux workspace (Ubuntu, x86_64, system Python 3.10) installs cleanly.
- BlaveClaw Windows workspaces ship **Python 3.14** by default — `unitrade` will fail to install
  there until a 3.12-or-earlier interpreter is set up alongside it. **Recommend Linux workspaces
  for this integration** unless the user's machine is confirmed to have a compatible Python.
- Before running Step 1 code below, confirm with `python3 -c "import platform; print(platform.machine(), platform.python_version())"` — if the machine is ARM or Python is 3.13+, stop and tell the user this broker isn't supported on their current machine.

---

## Step 4 — Collect Credentials & Write `.env`

**Ask the user (in one message):**
> 請提供你的統一期貨交易帳號（含分公司碼，共 11 碼，例如 8000 開頭）和交易密碼。

The test environment URL was already given in the activation email (Step 1) — reuse it here,
do not ask the user to repeat it.

Agent writes directly to `.env` — never asks the user to edit it themselves:

```python
# Agent 執行：append keys to .env
with open('.env', 'a') as f:
    f.write(f"\npresident_account={account}\n")        # 11-digit account incl. company code
    f.write(f"president_password={password}\n")        # trading password
    f.write(f"president_test_url={test_url}\n")         # from the activation email, Step 1
    f.write(f"president_ca_path=certs/president.pfx\n")
    f.write(f"president_ca_password={ca_password}\n")   # may be empty string
```

Confirm the write succeeded and tell the user the credentials are stored — do not echo them back.

---

## Step 5 — Test Connection

Use the **test environment URL from the user's activation email** — never hardcode a specific
test host, PFCF rotates them per activation batch.

```python
from unitrade.unitrade import Unitrade
from dotenv import dotenv_values

env = dotenv_values('.env')
api = Unitrade()
resp = api.login(
    env['president_test_url'],       # from the activation email, e.g. "https://testNNN.pfctrade.com"
    env['president_account'],
    env['president_password'],
    env['president_ca_path'],
    env['president_ca_password'],
)
print("Login:", resp.ok, resp.error)
if resp.ok:
    accounts = api.get_accounts()   # 7-digit account, no company-code prefix
    print("Accounts:", accounts)
api.logout()
```

**成功**：`resp.ok is True`，`get_accounts()` 回傳帳號列表。
**失敗常見原因**：
- 憑證路徑錯誤或憑證密碼錯誤 → error message mentions `憑證有誤` / `無法在 ... 找到指定憑證`
- 用錯 URL（不是活動信裡指定的測試主機）
- 帳號/密碼打錯

---

## Step 6 — Check Account Equity & Positions

```python
import time
time.sleep(2)   # let the session settle before querying

actno = api.get_accounts()[0]

margin = api.daccount.get_margin(actno, "")
print("Margin ok:", margin.ok, margin.error)
if margin.ok and margin.data:
    d = margin.data[0]
    print("Equity (optequity):", d.optequity, "TWD")

positions = api.daccount.get_position(actno)
print("Positions ok:", positions.ok)
for p in (positions.data or []):
    print(p.productid, p.month, "buy_open", p.current_buy_open_position, "sell_open", p.current_sell_open_position)
```

**Note:** on a test account with no funded balance, `get_margin` legitimately returns
`ok=False, error='查無資料'` — this is not a connection failure. Re-verify equity once the
production account is live and funded, since `manager/snapshot.py` depends on this call.

**Near-month contract lookup** (no `TXFR1`-style auto-roll alias exists in Unitrade):

```python
contracts = api.get_domestic_contracts("TXF", "F")   # "F" = futures product category
near_month_id = contracts.data[0].prod_id if contracts.ok else None
```

Call this once per session (or once per day) rather than hardcoding a contract id — TXF rolls to a
new near-month code monthly and a stale hardcoded id will silently trade the wrong contract.

---

## Step 7 — Wire into Portfolio

在 `portfolio_config.json` 的 `exchanges` 加入 president 路由：

```json
{
  "account_value": 500000,
  "exchanges": {
    "txf_strategy": "president"
  },
  "asset_specs": {
    "txf_strategy": {
      "type": "futures_contracts",
      "contract_value": 200,
      "currency": "TWD",
      "lot_size": 1
    }
  }
}
```

The order/account library implementing `place_order()` / `get_positions()` for `"president"` must
resolve the near-month contract id at call time (Step 6), not read a static symbol from
`asset_specs` — see `references/manager.md` for the reconciler wiring pattern.

---

## Trading Hours

| Market | Hours (台灣時間) |
|--------|-----------------|
| 台指期日盤 | 08:45 – 13:45 (Mon–Fri) |
| 台指期夜盤 | 15:00 – 05:00 (Mon–Fri) |

---

## Order Types

```python
from unitrade.unitrade import DOrderObject

order = DOrderObject()
order.actno = actno
order.note = "blave"
order.subactno = ""              # sub-account, blank if none
order.productid = near_month_id  # e.g. "TXFG6", from Step 6 lookup
order.bs = "B"                   # "B" = buy, "S" = sell
order.ordertype = "M"            # "L" = limit, "M" = market, "P" = range market
order.price = 0                  # 0 for market order
order.orderqty = 1               # 口數
order.ordercondition = "R"       # "I" = IOC, "R" = ROD, "F" = FOK
order.opencloseflag = ""         # "0" = open, "1" = close, "" = auto
order.dtrade = "N"               # "Y" = day trade, "N" = not

result = api.dtrade.order(order)
print(result.issend, result.errorcode, result.errormsg, result.seq)
```

**Fill/order reports** arrive two ways:
1. **Push callbacks** — set `api.dtrade.on_reply` / `api.dtrade.on_match` before placing orders;
   they fire immediately with the full report object (`orderstatus`, e.g. `'委託成功'`).
2. **Polling** — `api.dtrade.query_reply(actno, count, network_id_start, network_id_end, begin_order_time, end_order_time)`.
   `count` must be a **positive integer, not an empty string** — passing `""` returns an HTTP 400
   (`"The value '' is invalid"`). Pass a real limit (e.g. `20`) and, once verified in production,
   a real time range for the other fields.

---

## Limits & Gotchas

**注意事項：**
- Certificate password ≠ trading password — collect both separately (Step 2/4).
- Certificates expire annually and must be re-issued on Windows.
- No unauthenticated/simulation login mode — every test-environment login needs a valid cert.
- `get_domestic_contracts` must be called each session to resolve the current near-month TXF
  contract id — there is no static rolling alias like SinoPac's `TXFR1`.
- `query_reply`/`query_match` reject an empty string for the count parameter; always pass an int.
- No confirmed IP allowlisting behavior for the **production** environment yet — ask the broker
  rep whether the BlaveClaw machine's static IP needs to be registered before going live.
- Unitrade has no Linux ARM wheel and no Python 3.13+ wheel as of this writing (see Step 3).

---

## Verification Checklist for Agent

1. 平台/版本檢查 → 確認 workspace 是 Linux x86_64、Python ≤3.12（Step 3）
2. 連線測試（test environment, real certificate）→ `login.ok == True`
3. 查詢帳戶淨值 → `get_margin` 呼叫成功（測試帳戶查無資料屬正常，正式環境需再驗一次 equity > 0）
4. 查詢部位 → 確認無例外
5. 下一筆市價測試單（1 口）→ `orderstatus == '委託成功'`（測試環境不會真的成交）
6. 用戶通知營業員委託測試完成 → 等待正式環境開通信
7. 正式環境開通後，重複步驟 2–5（更換 URL、憑證維持不變）
8. 執行 `python3 manager/snapshot.py` → Telegram 收到含 president equity 的日報
9. 用戶確認後，方可設定 reconciler 上線（參考 `references/deployment.md`）
