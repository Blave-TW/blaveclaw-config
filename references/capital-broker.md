# Capital (群益) Broker — Agent Reference

Use this document when a user asks to connect their **Capital Securities (群益證券)** and/or
**Capital Futures (群益期貨)** account to Blave Agent — same corporate group, same underlying API,
one certificate covers both markets. The integration uses the official **Capital API**
(`SKCOM.dll`, a Windows COM component) — **Windows-only**, so this broker requires a
**Windows Blave Agent workspace**. There is no cross-platform package (unlike SinoPac/President).

Steps 1–6 (agreement, certificate, component install, login, accounts) are **shared** by both
markets. Order placement diverges — see Step 7a (Futures) / Step 7b (Securities).

Component download (login required): https://www.capital.com.tw/web/#/download/ApiTrading/ApiTradinginfo

---

## Supported Products

**Futures (期貨):**

| Product | `bstrStockNo` | Contract value | Notes |
|---------|--------------|----------------|-------|
| 台指期 TXF | `TX00` | 200 × index (TWD) | `TX00` is an official always-near-month alias — no per-session contract lookup needed |
| 小台 MXF | `MTX00` | 50 × index (TWD) | same alias convention |
| 微台 TMF | `TM0000` | 10 × index (TWD) | same alias convention |

Month-specific codes like `TX03` also work but auto-roll to *next year's* March once expired —
prefer the near-month aliases, or the V2.13.54+ `bstrCIDTandem`(`FITX`) + `bstrSettlementMonth`
(`yyyymm`) fields when a specific month is required.

**Securities (證券):** any TWSE/TPEx-listed stock by its 4-digit ticker (e.g. `2330`) — 整股
(1000-share lots) and 零股 (odd-lot, 1–999 shares) both supported, see Step 7b.

---

## Step 0 — Determine Scope & Platform

**Ask the user:**
> 你要交易股票、期貨（大台/小台/微台），還是兩者都要？

Then confirm the workspace is **Windows x64**. If the user is on a Linux workspace, this broker
cannot run there — escalate to Blave ops for a Windows machine before continuing.

---

## Step 1 — Sign the API Agreement (user side, any device)

1. User signs the declaration(s) for whichever market(s) they want, on the same 同意書簽署 portal:
   **證券API服務下單聲明書** (securities) and/or **期貨API服務下單聲明書** (futures) — two separate
   checkbox items on one page: https://tradeweb.capital.com.tw/TSWEB/agreeList.aspx (also available
   in 群益行動贏家 / 掌中財神 apps). Works from any browser incl. macOS.
2. **Futures activation is confirmed next-day** (「簽署完之後，要明天才會生效」) — plan the onboarding
   across two days. **Securities activation delay is unconfirmed** — no source pins the same
   next-day wait for the securities declaration; treat as "may also be next-day" until tested.
3. Without the relevant declaration signed: no trading accounts of that market from `GetUserAccount`
   (warning 2018/2019), no report connection, and — for futures specifically — even the commodity
   list fails (error 3031). Each market's declaration only unlocks that market's accounts; a user
   who only signs the securities one won't get a futures account back, and vice versa.

---

## Step 2 — Issue the Certificate on the Blave Agent Machine (RDP)

Since API 2.13.35, login is dual-factor: **a valid 群益 trading certificate must be installed on
the machine that runs the API — even for quote-only use.** The issuance tool (`RAWinApp.exe`) is
Windows-only, and the cert lands in the Windows certificate store, so issue it **directly on the
Blave Agent Windows machine** via RDP. The user does NOT need their own Windows PC.

Flow (agent orchestrates):

RDP is a standing feature of Windows Blave Agent machines — enabled at provision time, credentials
shown in the Blave Agent web dashboard (「遠端桌面連線」link: IP, Administrator, password).

1. Agent pre-downloads the issuance tool to the desktop (run as admin) so the user only has to
   run the wizard. **`www2.capital.com.tw` is a dead hostname as of 2026-07-16 (confirmed NXDOMAIN,
   not just a 404) — do not use it:**
   ```powershell
   # DEAD, do not use:
   # Invoke-WebRequest 'https://www2.capital.com.tw/download/RAWinApp.exe' -OutFile "$env:PUBLIC\Desktop\RAWinApp.exe"
   ```
   There is no stable static URL for `RAWinApp.exe` — the certificate area generates the link
   dynamically per visit, so this step can't be a hardcoded `Invoke-WebRequest` at all. Instead,
   browse **https://www.capitalfutures.com.tw** (群益期貨官網) → 「客戶常用功能」→「憑證專區」→
   「立即申請/展延」each time to reach the current download, and pull the .exe URL from there before
   running the download. If the agent has no browser automation on this machine, do this step
   inside the same RDP session with the user instead of pre-staging it silently.
2. Tell the user (Telegram):
   > 請連進你的 Blave Agent 機器桌面，跑一次群益的憑證精靈（約兩分鐘）：
   > 1. 到 Blave 網站的 Blave Agent 頁面，點「遠端桌面連線」看連線資訊（IP／帳號／密碼），用電腦內建的遠端桌面程式（Windows 按 Win+R 輸入 mstsc；Mac 裝 Windows App）連進去
   > 2. 點開桌面上的 RAWinApp.exe（我已下載好）
   > 3. 輸入身分證字號＋交易密碼登入，手機會收到簡訊驗證碼，照精靈完成憑證安裝
   > 4. 完成後跟我說一聲
   The GUI wizard itself cannot be driven by the agent (no GUI automation on the machine; the
   SMS OTP goes to the user's phone) — this 2-minute RDP session is the one manual step.
   **NEVER change the Administrator password** (`net user Administrator ...` / `Set-LocalUser`):
   the dashboard's 「遠端桌面連線」card serves the password stored platform-side, so a locally
   changed password silently locks the user out of their own machine (this exact incident,
   2026-08-08). RDP is already enabled and the password already set at provision time on every
   Blave Agent Windows machine. If you need the password yourself, read it locally from
   `C:\blave-agent\credentials\rdp_password.txt` (Blave Agent machines; BlaveClaw machines use
   `C:\openclaw\credentials\rdp_password.txt`; the oldest machines keep it in `.env` as
   `admin_password` and have no dashboard card). If no password can be found in any of those
   places, enabling RDP at the OS level (`fDenyTSConnections=0` + firewall rule) is fine — it
   does not touch the password — but never "fix" access by resetting the password; report to
   the user that the password is unavailable instead.
3. Certificate facts: needs 身分證字號 + 交易密碼 + SMS OTP; user sets a certificate password
   during issuance (not needed at API runtime, but keep it — it protects the desktop `.pfx`
   backup and private-key recovery needs both, see Step 5); **valid 1 year**, renew via
   the same RAWinApp flow (renewable from ~1 month before expiry) — warn the user it recurs.

**CONFIRMED (2026-07-16 POC, uid 12890):** SKCOM binds the certificate to the **Windows identity
that issued it** (always `Administrator` here, since issuance happens via RDP), not to a cert
store location. Exporting the cert and importing it into the `SYSTEM`/machine store does **not**
fix login — `SKCenterLib_Login` still returns 602 (cert validation failure) because SKCOM checks
the account identity, not just cert presence. **The only working fix: run the reconciler/strategy
service as the `Administrator` account, not `LocalSystem`** — see the Capital exception in
`references/manager.md`'s NSSM section (`nssm set ... ObjectName .\Administrator <password>`). Do
not attempt cert export/import as a workaround; it was tested and does not resolve 602.

**Also CONFIRMED: a *password logon* is required, not just the right account.** The same login
script run as Administrator succeeds or fails depending on how the session was created:
- SSH with public-key auth → **602** (Windows can't unlock the DPAPI-protected cert private key
  without password-derived credentials)
- Password-based logon (RDP, `schtasks /ru Administrator /rp <password>`, NSSM `ObjectName` with
  password) → **code=0 success**

Practical consequence for the agent: **you cannot run Capital login/order code directly from your
own shell** (the gateway service context) or via SSH — it will 602 even though everything is
installed correctly. Always execute Capital-touching scripts through a password-logon vehicle:
the NSSM service (production path) or a one-shot `schtasks /create ... /ru Administrator
/rp <password> /rl HIGHEST` + `/run` + `/delete` (ad-hoc testing path; read the password from
`C:\blave-agent\credentials\rdp_password.txt` — `C:\openclaw\credentials\rdp_password.txt` on
BlaveClaw machines, or `.env` `admin_password` on the oldest ones. Never reset it — see Step 2).

---

## Step 3 — Install the Capital API Component

**Only the download itself needs the user (login-gated) — every step after that is agent-executed.
Do not ask the user to extract, install, or register anything themselves; do not leave a zip on
the desktop for the user to double-click.**

1. The zip requires the user's own capital.com.tw login, so it can't be pre-staged like
   `RAWinApp.exe` — during the same RDP message where you ask them to run the cert wizard, also
   ask them to download this zip and forward it to you (or download it themselves in the RDP
   session's browser to `$env:PUBLIC\Desktop\`), then hand control back to the agent.
2. Once the zip is on the machine, agent extracts it via SSH/remote-exec — e.g.
   `Expand-Archive -Path <zip> -DestinationPath C:\skcom\x64\` — keeping `SKCOM.dll` together
   with its certificate/quote sub-components in one folder (they must be co-located for
   registration to work).
3. Agent installs the **VC++ 2010** redistributable — `CTSecuritiesATL.dll` (the cert component)
   links against `mfc100.dll`/`msvcr100.dll`, absent on a fresh Server 2022 image; without them
   `regsvr32 SKCOM.dll` fails with exit code 3 (LoadLibrary) and CreateObject gives "Class not
   registered". The 2.13.58 component zip does NOT bundle it — `vcredist_x64.exe` ships in the
   separate `SKCOMVerifyDJ` zip instead, **and that installer (both `/q` install and `/x` extract)
   hung past a 120 s timeout in a non-interactive SSH session and had to be killed** (measured
   2026-08-09). Install via `choco install vcredist2010 -y --no-progress` instead (choco verified
   present on the 12890 box; whether every Windows base image ships it is unverified — bootstrap
   choco first if missing), then verify `Test-Path C:\Windows\System32\mfc100.dll` before
   registering.
4. Agent runs `元件\x64\install.bat` **as administrator** (registers SKCOM.dll via regsvr32).
5. **Bitness must match Python**: x64 Python ↔ x64 component (mismatch → "Class not registered").
6. Agent verifies with the bundled `SKCOMTester.exe` CLI/silent mode if available; otherwise ask
   the user to eyeball it during the same RDP session as a final check (login there proves cert +
   agreement + component all work before writing any code).
7. **On any later API version upgrade:** uninstall old version, re-register, and **delete the
   comtypes cache** (`site-packages/comtypes/gen/SKCOMLib.py` + the GUID-named module) — stale
   generated wrappers cause silent quote/order anomalies.

Python packages: `pip install comtypes pywin32`. Verified on a Blave Agent Windows workspace's
Python 3.14 (2026-07-17, medium_win POC box): comtypes 1.4.16 + pywin32 312 install cleanly and
COM CreateObject / Dispatch / message pump all work. (SKCOM.dll itself untested there — the zip
is login-gated; confirm `GetModule` on first real onboarding.)

---

## Step 4 — Collect Credentials & Write `.env`

**Ask the user (in one message):**
> 請提供你的身分證字號和群益交易密碼（跟登入群益下單軟體同一組）。

```python
# Agent 執行：append keys to .env
with open('.env', 'a') as f:
    f.write(f"\ncapital_id={user_id}\n")          # 身分證字號 (login ID)
    f.write(f"capital_password={password}\n")     # trading password
```

Runtime needs only these two — the certificate is read from the machine automatically.
Confirm stored; never echo back.

---

## Step 5 — Test Login

```python
import comtypes.client
comtypes.client.GetModule(r'C:\skcom\x64\SKCOM.dll')
import comtypes.gen.SKCOMLib as sk
from dotenv import dotenv_values

env = dotenv_values('.env')
center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)

class ReplyEvents:
    def OnReplyMessage(self, bstrUserID, bstrMessages):
        return -1   # MUST return -1, and MUST be registered BEFORE login (else code 2017)

reply_handler = comtypes.client.GetEvents(reply, ReplyEvents())

code = center.SKCenterLib_Login(env['capital_id'], env['capital_password'])
print(code, center.SKCenterLib_GetReturnCodeMessage(code))
```

**成功**：`code == 0`（`2003` = 已登入，也算成功）。
**失敗常見代碼**：300 密碼錯誤 / 307 密碼被鎖定 / 600 憑證錯誤（未安裝或有過期舊憑證，刪除過期的那張）/
602 憑證驗證失敗（見下）/ 604 憑證過期或已註銷 / 2017 未先註冊 OnReplyMessage。
`SKCenterLib_GetLastLogInfo()` gives more detail on failures.

**602 with a cert visibly in the store — check for an orphaned private key** (measured 2026-08-09,
uid 12890). First rule out the two known 602 causes above (wrong Windows identity, key-auth SSH
instead of a password logon — see Step 2's CONFIRMED notes), and run the diagnostic itself under
the schtasks password vehicle too — a key-auth session cannot unlock DPAPI, so certutil there can
misreport a healthy key. Then: `certutil -user -store My` showing **"Missing stored keyset"**
means the cert lost its private key. Mechanism: an administrative Administrator password reset
(`net user`/`Set-LocalUser`, the exact thing Step 2 bans) breaks DPAPI access to keys created
before the reset — the likeliest cause in the measured case (timeline matches the 2026-08-08
reset incident), though not isolated experimentally. Fix from the issuance-time `.pfx` backup
(observed on the desktop after the wizard; its password is the **certificate password** the user
chose in the wizard — NOT the trading password):
```powershell
certutil -user -delstore My <serial>       # delete the keyless entry FIRST — importing over it
                                           # merges and keeps the broken key link
certutil -user -p <cert-password> -importpfx <backup.pfx>
```
Run under a password logon (schtasks vehicle) and confirm `certutil -user -store My` now says
"Encryption test passed". No pfx backup or password forgotten → re-run the RAWinApp wizard.

---

## Step 6 — Get Accounts (async — message pump required)

COM events NEVER fire in a plain console script unless you pump Windows messages:

```python
import pythoncom, time

order = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)

class OrderEvents:
    futures_accounts = []
    stock_accounts = []
    def OnAccount(self, bstrLogInID, bstrAccountData):
        f = bstrAccountData.split(',')                       # market,branch,branch_name,account,...
        account = f[1] + f[3]                                # branch/broker code (4) + account (7)
        if f[0] == 'TF':                                     # TF = domestic futures
            OrderEvents.futures_accounts.append(account)
        elif f[0] == 'TS':                                   # TS = domestic securities
            OrderEvents.stock_accounts.append(account)

order_handler = comtypes.client.GetEvents(order, OrderEvents())

order.SKOrderLib_Initialize()
order.ReadCertByID(env['capital_id'])   # dual-factor cert check — skipping it → order error 1038
order.GetUserAccount()                  # async, arrives via OnAccount (one event per account/market)

deadline = time.time() + 10
while not (OrderEvents.futures_accounts or OrderEvents.stock_accounts) and time.time() < deadline:
    pythoncom.PumpWaitingMessages()
    time.sleep(0.1)
print("Futures accounts:", OrderEvents.futures_accounts)
print("Stock accounts:", OrderEvents.stock_accounts)
```

Empty for a market → that market's declaration isn't active yet (next-day for futures; unconfirmed
for securities, see Step 1) or **no account of that type exists under this ID at all** — signing
the futures declaration does not create a futures account; a user with only a securities account
gets no `TF` row ever, and the fix is opening a 期貨戶, not waiting (measured 2026-08-10). Other
market codes seen live: `OS` = overseas securities (複委託); ignore rows that are neither `TF` nor
`TS`.

---

## Step 6b — Read Securities Inventory (positions)

`GetRealBalanceReport(bstrLogInID, bstrAccount)` — account = branch(4) + account(7), e.g. from
the `TS` OnAccount row. Returns 0 and delivers rows via `OnRealBalanceReport(bstrData)` (pump
required); a row starting with `##` marks end-of-report — wait for it, zero data rows before the
marker legitimately means an empty portfolio (verified 2026-08-10 after the market close — works
outside trading hours; login itself also verified on a Sunday).
Comma-separated fields, in order: 股票代號, 庫存種類 (`T` 集保 / `C` 融資 / `L` 融券), 資額度(原始),
資額度(可用), 券額度(原始), 券額度(可用), 昨日庫存股數, 今日委買, 今日委賣, 今日買進成交,
今日賣出成交, 今日資券可回補/集保庫存可賣出, 可資沖股數, 可券沖股數, 即時庫存, (ignore), 即時個股維持率,
LOGIN_ID, ACCOUNT_NO. Manual warns 可資沖/可券沖 were swapped in v2.13.42–2.13.54 — pin the
component version before trusting those two. Do NOT use `GetBalanceQuery` — no longer provided
after v2.13.54, `GetRealBalanceReport` is its replacement. Futures open interest is a different call
(`GetOpenInterest`, account = IB+帳號, via `OnOpenInterest`) — untested, verify on first futures
onboarding.

---

## Step 7a — Place Futures Orders

```python
pOrder = sk.FUTUREORDER()
pOrder.bstrFullAccount = OrderEvents.futures_accounts[0]
pOrder.bstrStockNo = 'TX00'     # near-month alias (MTX00 / TM0000 for mini/micro)
pOrder.sBuySell = 0             # 0 = buy, 1 = sell
pOrder.sTradeType = 1           # 0 = ROD, 1 = IOC, 2 = FOK
pOrder.bstrPrice = 'M'          # market ("M"/"P" ONLY valid with IOC or FOK); numeric string for limit
pOrder.nQty = 1                 # 口數
pOrder.sNewClose = 2            # 0 = 新倉, 1 = 平倉, 2 = auto
pOrder.sDayTrade = 0            # 1 = day trade
pOrder.sReserved = 0            # 0 = intraday (T/T+1盤), 1 = T盤預約

msg, ncode = order.SendFutureOrderCLR(env['capital_id'], False, pOrder)   # False = synchronous
print(ncode, msg)   # ncode 0 → msg is the 13-digit order sequence number
```

**Built-in throttle:** `SetMaxQty` / `SetMaxCount` cap per-second order flow; exceeding them locks
that market's orders until `UnlockOrder`. 群益 also monitors API 異常下單 (looping orders) — keep
order frequency sane by design.

---

## Step 7b — Place Stock (Securities) Orders

```python
pOrder = sk.STOCKORDER()
pOrder.bstrFullAccount = OrderEvents.stock_accounts[0]
pOrder.bstrStockNo = '2330'      # 4-digit TWSE/TPEx ticker
pOrder.sPrime = 0                # 0 = 上市上櫃, 1 = 興櫃
pOrder.sPeriod = 0                # 0 = 盤中, 1 = 盤後, 2 = 零股, 4 = 盤中零股 (reduced struct, see below)
pOrder.sFlag = 0                  # 0 = 現股, 1 = 融資, 2 = 融券, 3 = 無券
pOrder.sBuySell = 0                # 0 = buy, 1 = sell
pOrder.bstrPrice = '590.0'        # numeric string; or "M"/"H"/"L" (參考價/漲停/跌停)
pOrder.nQty = 1                   # 整股(sPeriod 0/1) = 張數(1000股); 零股(sPeriod 2/4) = 1-999股
pOrder.nTradeType = 0              # [逐筆交易] 0 = ROD, 1 = IOC, 2 = FOK
pOrder.nSpecialTradeType = 2       # [逐筆交易] 1 = 市價 (bstrPrice=0), 2 = 限價 (bstrPrice required)

msg, ncode = order.SendStockOrder(env['capital_id'], False, pOrder)   # note: NOT "...CLR" — futures-only suffix
print(ncode, msg)   # ncode 0 → msg is the 13-digit order sequence number
```

**盤中零股 (`sPeriod=4`) uses a reduced struct** — only `sFlag=0`(現股), `sBuySell`, `bstrPrice`,
`nQty`(1–999股); no `sPrime`/`nTradeType`/`nSpecialTradeType`. Same call, `SendStockOrder`; odd-lot
orders during 13:40–14:30 (盤後零股 window) use `SendStockOddLotOrder` instead (same signature).

**No day-trade flag on the order itself** — unlike futures' `sDayTrade`, 現股當沖 eligibility is a
per-stock attribute (check via quote, `SKSTOCKLONG.nDayTrade`), and day-trading is just placing an
offsetting `sFlag=0` order same-day, not a struct field.

---

## Order/Fill Reports (both markets)

Same mechanism for futures and stocks: call `reply.SKReplyLib_ConnectByID(env['capital_id'])`
(0 = success), then `OnNewData(bstrUserID, bstrData)` fires with comma-separated fields — key ones:
index 1 = market type (`TF`/`TS`/...), index 2 = type (`N`委託 `D`成交 `C`取消 `P`改價 `S`動態退單),
index 3 = error flag (`N` ok / `Y` fail / `T` timeout), index 11 = fill price, index 23 = fill time.
Same indices for both markets (confirmed against the manual's shared-field section); securities
orders additionally carry `BeforeQty`/`AfterQty` near the qty position. Field positions per
manual + community parser — verify against live data on first fill and pin them here.

---

## Step 8 — Wire into Portfolio

`portfolio_config.json` — futures and stock strategies both route through `"capital"`:

```json
{
  "exchanges": {
    "txf_strategy": "capital",
    "tsmc_strategy": "capital"
  },
  "asset_specs": {
    "txf_strategy": {
      "type": "futures_contracts",
      "contract_value": 200,
      "currency": "TWD",
      "lot_size": 1
    },
    "tsmc_strategy": {
      "type": "tw_stock",
      "lot_size": 1000,
      "currency": "TWD",
      "capital_symbol": "2330"
    }
  }
}
```

`contract_value` (futures only): 200 (TXF) / 50 (MXF) / 10 (TMF). `lot_size` for stocks is 1000
(整股/張) — for a strategy trading 零股 exclusively, use `lot_size: 1` and treat `nQty` as raw
shares. Order library + reconciler wiring is one atomic task — see `references/manager.md`.

---

## Trading Hours

| Market | Hours (台灣時間) |
|--------|-----------------|
| 台指期日盤 | 08:45 – 13:45 (Mon–Fri) |
| 台指期夜盤 | 15:00 – 05:00 (Mon–Fri) |
| 股票盤中 | 09:00 – 13:30 (Mon–Fri) |
| 股票盤中零股 | 09:10 起，每 5 秒撮合一次，同盤中收盤 |
| 股票盤後定價 | 14:00 – 14:30 |
| 股票盤後零股 | 13:40 – 14:30（盤中零股未成交不會 carry over 到這個時段）|

---

## Limits & Gotchas

- **No confirmed simulation environment.** A `morder1` sim server existed historically but its
  docs were removed and a "停止模擬平台服務" warning code exists — assume production-only. Validate
  the order path with a 1-lot micro futures order (TM0000) or a 1-share 零股 stock order the user
  approves, or an intentionally rejected order (e.g. unfunded account → 保證金不足).
- Futures agreement activation is **next-day**; securities activation delay is **unconfirmed**
  (GAP — no source pins it, test on first real onboarding and update this doc). Certificate
  expires **yearly**. All recurring support cases, not one-time.
- **`SendStockOrder`, not `SendStockOrderCLR`** — unlike futures, there's no CLR-suffixed variant
  for stocks; don't guess the futures naming pattern applies here.
- One cert per machine, shared by both markets: expired leftover certs cause login error 600 —
  delete old ones. Signing only one market's declaration doesn't require a second certificate.
- No broker attribution headers — 群益 is the broker itself.
- Every API version upgrade requires re-registration + comtypes cache deletion (Step 3.7).

---

## Verification Checklist for Agent

1. Windows x64 workspace + user signed the relevant declaration(s) (期貨/證券, wait ≥1 day for
   futures) → Step 1
2. Certificate issued on this machine via RDP (credentials from the web dashboard) → Step 2
3. `SKCOMTester.exe` login OK → component/cert/agreement all good → Step 3
4. Python login `code == 0` → Step 5
5. `OnAccount` returns the expected account(s) (`TF` and/or `TS`) → Step 6
6. One user-approved minimal live order per market being used (or intentional rejection)
   confirms the order path → Step 7a / 7b
7. 用戶確認後，方可設定 reconciler 上線（參考 `references/deployment.md`）
