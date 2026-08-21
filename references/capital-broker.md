# Capital (群益) Broker — Agent Reference

Use this document when a user asks to connect their **Capital Securities (群益證券)** and/or
**Capital Futures (群益期貨)** account to Blave Agent — same corporate group, same underlying API,
one certificate covers both markets. The integration uses the official **Capital API**
(`SKCOM.dll`, a Windows COM component) — **Windows-only**, so this broker requires a
**Windows Blave Agent workspace**. There is no cross-platform package (unlike SinoPac/President).

Steps 1–6 (verification test + agreement, certificate, component install, login, accounts) are
**shared** by both markets. Order placement diverges — see Step 7a (Futures) / Step 7b (Securities).
Note the execution order: the 檢核 in Step 1 needs the machine set up first, so the real sequence
is Step 2 → Step 3 → Step 1 (檢核 + sign) → Steps 5–7.

Component download page: https://www.capital.com.tw/web/#/download/ApiTrading/ApiTradinginfo
(the component/verify-tool zips themselves are static, login-free URLs — see Steps 1/3)

**Acknowledge before you go quiet.** Steps 2–3 involve silent background work (downloading the
cert-issuance tool, downloading the component zip, installing the VC++ 2010 redistributable) that
can run several minutes with nothing to show for it. Before starting ANY of that — as your very
first reply to the connect handoff, before doing anything else — send a short message so the wait
doesn't read as the agent being stuck: e.g. 「收到，我先幫你把群益的憑證工具和連線元件準備好，
大概需要幾分鐘，準備好後會馬上跟你說。」 Then do the downloads/installs, then send the real Step 2
instructions (RDP + cert wizard).

**Checkpoint after each major step — never chain multiple steps into one turn.** A single turn
has a hard ~50 tool-call ceiling (`agent_turn.py`'s `max_turns`); Step 3 (component install) alone
can burn a large chunk of that (download, extract, choco install, several regsvr32/Test-Path
checks), and Step 1 (SKCOMVerifyDJ login + 2 simulated orders + polling 「查詢是否已驗證」, which
can itself need retries) adds more. Chaining Step 3 → Step 1 → Steps 5–6 into one attempt risks
silently hitting that ceiling mid-work with nothing to show for it (measured 2026-08-14: exactly
this happened, user got the generic 「處理到一半被中斷了」 SDK fallback after asking to proceed
through registration + verification + signing in one go). Instead: finish ONE step, report what
just happened and what's next, and stop — let that message end the turn. The user's next message
(or your own follow-up, if nothing more is needed from them) starts a fresh turn with a fresh
budget. Natural checkpoints: after Step 3 completes → after Step 1's 檢核 passes (before signing)
→ after signing → after Step 6's account check → after Step 8's worker install.

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

## Step 1 — Pass the API Verification Test, then Sign the API Agreement

**Signing is gated behind an API verification test (檢核). Confirmed on the live 群益金融網 API
download page (read 2026-08-13): its official「API申請步驟」is ① 憑證安裝&申請 → ② 下載
API連線測試小程式,完成連線測試 → ③ 線上簽署 → ④ 開始使用. The gate is enforced in the API
itself as error 321「測試未完成:請先至API下載專區 完成測試後再進行下一步」(v2.13.53 changelog,
which introduced the SKCOMVerifyDJ tool). The 2026-08-09 onboarding (uid 12890) never hit this
gate — why is unknown (possibly a pre-existing 檢核 on that ID); treat the gate as the rule.**

0. Run the verification test first: the tool is labeled **「API連線測試小程式」** on the download
   page (「API申請步驟」block, item 2 — below the three 下載元件 buttons); the zip is a static,
   login-free URL the agent can pre-stage:
   `https://www.capital.com.tw/Service2/download/api_zip/CapitalAPI_v5.0_SKCOMVerifyDJ.zip`
   After extracting, **copy the whole `...\元件\x64\` folder to `$env:PUBLIC\Desktop\SKCOMVerifyDJ\`**
   (or drop a shortcut to the exe on the desktop) — NOT the bare exe. `SKCOMVerifyDJ.exe` is a
   28 KB .NET shell that P/Invokes `SKCOM.dll` from its own directory; copied alone it starts,
   but clicking 登入 dies with `DllNotFoundException: Unable to load DLL 'SKCOM.dll'` (and the
   form may first show a vague "no certificate"-style message) — measured 2026-08-21, uid
   29026, after the agent had copied just the exe. The desktop staging is still the point: the
   zip buries the tool several levels deep (`CapitalAPI_v5.0_SKCOMVerifyDJ\元件\x64\`), a bad
   time for the user to hunt through Explorer over RDP (measured 2026-08-14). Tell the user it's
   "on the desktop, in the SKCOMVerifyDJ folder", not the nested path.
   Run `SKCOMVerifyDJ.exe`, log in with 身分證字號 + trading password, submit both
   **模擬國內證券下單** and **模擬國內期貨下單**, then — **required, not optional — click
   「查詢是否已驗證」**: the two simulated orders alone do NOT complete the verification;
   without this final query the signing portal still refuses the declaration (field-verified
   2026-08-13). Signing unlocks **immediately** after the full 檢核 (no next-day wait —
   same-day sign verified 2026-08-13).
   The tool runs against the API environment, so do Steps 2–3 (certificate + component install
   on the agent machine) **before** this test.
   Verification status appears to be stored **per 身分證字號 account, not per machine**
   (consistent with uid 12890 never hitting the gate on a fresh machine) — so a user who
   relaunches their machine should NOT need to re-run the 檢核; what a new machine does need
   is a fresh certificate (Step 2) + component install (Step 3). **Isolated 2026-08-21 (uid
   29026, ID previously verified on another machine):** fresh machine, new cert, 檢核 NOT
   re-run (the tool was opened but never completed) → login `code 0` and both TF/TS accounts
   returned. So: if the user says they passed the 檢核 before, skip this step entirely and go
   to Step 5 — do not send them back to the tool. Only if login returns 321 re-run it.
   **Verify tool says "no certificate" / crashes on 登入 (observed 2026-08-21):** with a
   valid cert in Administrator's `CurrentUser\My` (private key present, chain built, same RDP
   identity) the tool still complained, and its error detail was
   `DllNotFoundException: Unable to load DLL 'SKCOM.dll'` — the exe had been copied to the
   desktop ALONE (see item 0 above). It is a tool-staging problem, not a certificate problem. Do
   NOT read it as "the cert wasn't installed": verify the store via the schtasks vehicle (Step
   2), and re-stage the tool with its sibling DLLs if the 檢核 is needed at all.
1. User signs the declaration(s) for whichever market(s) they want, on the same 同意書簽署 portal:
   **證券API服務下單聲明書** (securities) and/or **期貨API服務下單聲明書** (futures) — two separate
   checkbox items on one page: https://tradeweb.capital.com.tw/TSWEB/agreeList.aspx (also available
   in 群益行動贏家 / 掌中財神 apps). Works from any browser incl. macOS. The securities download
   page calls the combined document「應用程式介面(API)服務申請暨委託交易風險預告書」and lists
   群益E櫃檯/同意書專區 and 新金融網/線上簽署專區 as signing portals — same signing step,
   different entry points.
2. **Activation is same-day for both markets** — field-verified 2026-08-13: `GetUserAccount`
   returned both the TF and TS accounts within the hour after signing both declarations
   (the earlier "futures activates next-day"「簽署完之後，要明天才會生效」claim did not
   reproduce; it may date from before the 檢核-gated flow). Whether **order acceptance** also
   unlocks same-day is unverified — if a same-day order bounces with 2018-family errors,
   retry next day before diagnosing anything else.
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

**HARD RULE — never judge the cert store from your own shell.** Your shell runs as
`nt authority\system`; `certutil -user -store My` there lists SYSTEM's (empty) store, not the
Administrator store the wizard installs into. Two onboardings in a row (2026-08-20 and
2026-08-21, uid 29026) the agent looked from its own shell, saw "empty", and told the user
their cert "wasn't really installed" / asked for the .pfx password to re-import — both times
the cert was fine. Check (and log in) ONLY through the password-logon schtasks vehicle
described in the CONFIRMED notes below, or by running `lib/capital_worker.py` once (Step 5).

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
2. Tell the user (Telegram) — **NEVER paste the IP / account / password into the chat message
   itself, even though you can read them** (`C:\blave-agent\credentials\rdp_password.txt`, see
   below): a plaintext credential in chat sits in session history/logs indefinitely, and the
   user already has a proper place to see it. Point them at the machine page instead:
   > 請連進你的 Blave Agent 機器桌面，跑一次群益的憑證精靈（約兩分鐘）：
   > 1. 到 Blave 網站的 Blave Agent 機器頁，點「遠端桌面連線」看連線資訊（IP／帳號／密碼），裡面也有「怎麼連線」的操作教學連結
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

**Just run the script — one call does this whole step:**
```
powershell -NoProfile -ExecutionPolicy Bypass -File C:\blave-agent\workspace\lib\capital_setup.ps1
```
`lib/capital_setup.ps1` installs VC++ 2010 + Python deps, downloads/extracts/registers the
component (proves it with `CreateObject`), and stages the verify tool **with its DLLs**. It is
idempotent (re-runs skip what's already registered — keyed on `CreateObject` working, not a marker)
and prints a JSON summary; exit 0 = every step ok, else read `errors`. Runs fine from your own
(SYSTEM) shell — nothing here touches the certificate. This replaces ~20 hand tool-calls that took
7–12 min per onboarding (2026-08-20/21). Only fall back to the manual steps below if the script
reports a failure you need to diagnose, or 群益 bumps the version (`-Version 2.13.60`).

<details><summary>Manual fallback (what the script automates)</summary>

**Every step is agent-executed (both zips are static, login-free URLs — see below).
Do not ask the user to extract, install, or register anything themselves; do not leave a zip on
the desktop for the user to double-click.**

1. The component zip is a **static, login-free URL** (verified 2026-08-13 — the earlier belief
   that it was login-gated is wrong, at least currently), so the agent can pre-stage it:
   `https://www.capital.com.tw/Service2/download/api_zip/CapitalAPI_<version>.zip`
   (e.g. `CapitalAPI_2.13.59.zip`; check the download page for the current version number).
   Install the **latest** version: v2.13.59 (2026-08-10) added error 9996
   `SK_ERROR_UPDATE_API_REQUIRED`「此版本已無法登入」 — 群益 can force-expire old versions, so
   pinning an old zip risks a fleet-wide login break. If the static URL pattern ever breaks,
   fall back to asking the user to download from the page in the RDP session's browser.
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
was believed login-gated at the time; confirm `GetModule` on first real onboarding.)

</details>

---

## Step 4 — Collect Credentials & Write `.env`

**Ask the user (in one message):**
> 請提供你的身分證字號和群益交易密碼（跟登入群益下單軟體同一組）。

```python
# Agent 執行：append keys to .env
with open('.env', 'a') as f:
    f.write(f"\ncapital_api_key={user_id}\n")     # 身分證字號 (login ID)
    f.write(f"capital_password={password}\n")     # trading password
```

Runtime needs only these two — the certificate is read from the machine automatically.
Confirm stored; never echo back.

**Canonical env names (decided 2026-08-14): `capital_api_key`(身分證字號)+ `capital_password`.**
Rationale: the runtime's `account_reader.py` / `portfolio_reporter.venues()` discover venues by
the `{PREFIX}_API_KEY` env pattern — an id stored as `capital_id` is invisible to discovery and
the venue never reaches the workspace page. The web `CX_VENUES` capital entry writes these same
names. Legacy note: the very first test machine (uid=1) also carries `capital_id` — harmless
duplicate; new onboardings write only the canonical pair. Reporter caveat: `pair` stays `false`
for capital (no `*_SECRET_KEY`) — the web treats manual-venue credentials as paired; if the
runtime's pair rule ever learns venue-specific secrets, drop the web workaround together.

---

## Step 5 — Read-Only Verify (login + accounts + equity)

**Just run the probe — one call does Steps 5–6:**
```
powershell -NoProfile -ExecutionPolicy Bypass -File C:\blave-agent\workspace\lib\capital_probe.ps1
```
`lib/capital_probe.ps1` wraps `lib/capital_worker.py --once` in the schtasks Administrator
password vehicle (the ONLY context where SKCOM login passes — see Step 2), logs in, reads both
accounts + one equity/position tick, and prints `state/capital_probe.json`. Exit 0 =
`{"ok":true,...login_code 0, accounts, equity}`; exit 2 = ran but failed (read `stage`/`error` —
`login`/`cert`/`accounts`); exit 3 = worker hung. Safe to run alongside the live worker service.

**Do NOT hand-write a login script, and do NOT judge the cert store from your own shell** — both
misfired repeatedly (2026-08-20/21, uid 29026: SYSTEM-shell `certutil` showed an empty store and
the agent wrongly told the user the cert "wasn't installed"). The probe is the whole story: if it
returns `ok`, login and accounts work; if `stage:"cert"` (login 602), re-read Step 2's 602 notes.

<details><summary>Manual fallback — raw login snippet (what the probe runs, must go through a password logon)</summary>

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

code = center.SKCenterLib_Login(env['capital_api_key'], env['capital_password'])
print(code, center.SKCenterLib_GetReturnCodeMessage(code))
```

If you hand-write a login script, keep every `comtypes.client.GetEvents(...)` return value in a
live variable (as above): discard it and the COM event sink is garbage-collected, the registration
silently disappears, and login returns 2017 even though your code "registered" the handler
(measured 2026-08-20, uid 29026 — burned ~9 min re-deriving this against a working lib). This is
exactly why Step 5's `capital_probe.ps1` / `capital_worker.py --once` exists — prefer it.

</details>

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
order.ReadCertByID(env['capital_api_key'])   # dual-factor cert check — skipping it → order error 1038
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
(`GetOpenInterest(bstrLogInID, bstrAccount)`, account = the full `TF` OnAccount string e.g.
`F0200001603963`, rows via `OnOpenInterest`) — verified 2026-08-13 on an empty account: call
returns 0; an empty account delivers one `001,查無資料,<account>` row and the report — like the other reports — ends with a comma-padded `##` terminator row (observed live 2026-08-13; wait for it, don't stop at the first row). The message field is Big5/CP950
in console redirects — inside Python the BSTR is proper unicode; the mojibake is console-encoding
only. Non-empty row fields (official manual V2.13.59 §4-2-x, comma-separated): 市場別, 帳號, 商品,
買賣別, 未平倉部位, 當沖未平倉部位, 平均成本(3 decimals), 一點價值, 單口手續費, 交易稅(萬分之X),
LOGIN_ID — confirm against live data after the first real futures fill.

## Step 6c — Futures Equity (`GetFutureRights`)

`GetFutureRights(bstrLogInID, bstrAccount, 1)` (account = full TF string), rows via
`OnFutureRights(bstrData)`; a `##`-prefixed row marks end-of-report. Multi-currency accounts
return one row per currency, **first row = base currency**. Rate-limited — poll gently
(error 1019 `SK_ERROR_QUERY_IN_PROCESSING` when called too often); ~60s cadence measured safe.
Comma-separated fields (official manual V2.13.59 §4-2-i, 0-based; cross-checked against a live
row 2026-08-13 — 幣別/存提款/昨日餘額/LOGIN_ID anchors all matched):
0 帳戶餘額, 1 浮動損益, 2 已實現費用, 3 交易稅, 4 預扣權利金, 5 權利金收付, **6 權益數**,
7 超額保證金, 8 存提款, 9 買方市值, 10 賣方市值, 11 期貨平倉損益, 12 盤中未實現,
13 原始保證金, 14 維持保證金, 15 部位原始保證金, 16 部位維持保證金, 17 委託保證金,
18 超額最佳保證金, 19 權利總值, 20 預扣費用, 21 原始保證金, 22 昨日餘額,
23 選擇權組合單加不加收保證金, 24 維持率, **25 幣別** (`NTD`), 26 足額原始保證金,
27 足額維持保證金, 28 足額可用, 29 抵繳金額, 30 有價可用, 31 可用餘額, 32 足額現金可用,
33 有價價值, 34 風險指標, 35 選擇權到期差異, 36 選擇權到期差損, 37 期貨到期損益,
38 加收保證金, 39 LOGIN_ID, 40 ACCOUNT_NO.
維持率/風險指標 come back masked as `*********` when the account has no positions.
Equity for sizing/display = **權益數 (index 6)**; currency from index 25 (`NTD` → report `TWD`).

**Do this next, before anything else — and don't ask first, just do it:** install the Account
Snapshot Worker (Step 8's `blave-agent-capital` NSSM service, below) now — not after order
testing, not after strategy wiring. It's read-only infrastructure (a background balance/position
poller, no orders, no exposure), same trust tier as everything else up through Step 6c — asking
"要我接著裝嗎？" here just adds a needless round-trip (measured 2026-08-14: user's reaction was
"他也不用問，直接裝好就好"). Install it, confirm the snapshot is fresh, THEN tell the user it's
done — asking permission is for Step 7a/7b (placing an actual order), not this. Until the worker
is running and has written a fresh `state/capital_account.json`, the platform's own `get_equity()`
(`lib/account_capital.py`) has nothing to read and fails on every call — the user's web dashboard
shows a hard "連線失敗" card for as long as this step is skipped, even though everything up
through Step 6c already works.

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

msg, ncode = order.SendFutureOrderCLR(env['capital_api_key'], False, pOrder)   # False = synchronous
print(ncode, msg)   # ncode 0 → msg is the 13-digit order sequence number
```

**Built-in throttle:** `SetMaxQty` / `SetMaxCount` cap per-second order flow; exceeding them locks
that market's orders until `UnlockOrder`. 群益 also monitors API 異常下單 (looping orders) — keep
order frequency sane by design.

**Live round trip 2026-08-14 (TM0000 buy 1 → close):** order accepted with `ncode=0`, `msg` =
13-digit order seq; fills within a second. `TM0000` resolves to the actual near-month contract
(`TM2608`) in every report AND in `GetOpenInterest` positions — reconcilers must match on the
resolved code, not the alias. TMF original margin observed 35,050 TWD at index ≈46,100 (margin
scales with the index — don't hardcode). Same-day order acceptance confirmed (signed the
declarations the prior evening).

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

msg, ncode = order.SendStockOrder(env['capital_api_key'], False, pOrder)   # note: NOT "...CLR" — futures-only suffix
print(ncode, msg)   # ncode 0 → msg is the 13-digit order sequence number
```

**盤中零股 (`sPeriod=4`) is NOT a reduced struct** — live-tested 2026-08-14: omitting
`nSpecialTradeType` rejects with `1067 "Special Trade type value should be 1 or 2"`. Send the
full struct (`nTradeType=0` ROD + `nSpecialTradeType=2` 限價 + limit `bstrPrice`; odd lots are
limit-only) with `nQty` = shares (1–999). Odd-lot reports come back under market type **`TC`**,
not `TS` (live row: `...,TC,D,N,913Y,...,2002,...,19.0500,...`). Odd-lot orders during
13:40–14:30 (盤後零股 window) use `SendStockOddLotOrder` instead (same signature).

**No day-trade flag on the order itself** — unlike futures' `sDayTrade`, 現股當沖 eligibility is a
per-stock attribute (check via quote, `SKSTOCKLONG.nDayTrade`), and day-trading is just placing an
offsetting `sFlag=0` order same-day, not a struct field.

**No pre-funding check on buys (live-tested 2026-08-14):** a fresh account with an empty
settlement bank account had both a whole-lot and an odd-lot buy accepted and filled — settlement
is T+2, and the broker does not pre-block. The agent MUST remind the user to fund the settlement
account by T+2 or they default (違約交割). `GetRealBalanceReport` reflects the fill immediately
(今日買進成交 + 即時庫存 columns — field map in Step 6b verified against these live fills).

---

## Order/Fill Reports (both markets)

Same mechanism for futures and stocks: call `reply.SKReplyLib_ConnectByID(env['capital_api_key'])`
(0 = success), then `OnNewData(bstrUserID, bstrData)` fires with comma-separated fields.
**Field indices pinned against live fills 2026-08-14** (TF futures round trip + TS whole-lot +
TC odd-lot), 0-based:

- 0 委託序號 KeyNo 13碼 — **empty on futures `D` rows** (present on stock `D` rows); the
  reliable copy for ALL rows is index 45 (second-to-last field)
- 1 market (`TF` futures / `TS` stock / `TC` 盤中零股), 2 type (`N`委託 `D`成交 `C`取消
  `P`改價 `S`動態退單), 3 error flag (`N` ok)
- 4 branch, 5 account, 6 order-kind code (futures `BNI10`/`SOI10` = buy-new/sell-offset IOC;
  stock `B00R2`), 8 **symbol — the real contract** (`TM2608`, not the `TM0000` alias you sent;
  stocks: ticker)
- 11 price — `0.0000` on `N` rows, fill price on `D` rows (1314 limit 7.72 filled at 7.69:
  price improvement is normal)
- 20 qty (futures: lots; stocks: SHARES — 1000 for one 張), 21/22 BeforeQty/AfterQty
  (stock `N` rows)
- 23 date, 24 time (HH:MM:SS), 30 exchange seq (`1010/2110...`=委託, `1020/2120...`=成交),
  32/33 futures: series+month (`FITM`,`202608`), 38 成交編號 (`D` rows only),
  45 KeyNo, 46 time with milliseconds

**Two consumer-side rules (both live-observed):** ① `SKReplyLib_ConnectByID` **replays every
report from the whole session day** on each connect — a fresh process re-receives this morning's
fills; ② stock (`TS`) reports arrived **duplicated** (same row delivered twice back-to-back).
Any consumer MUST dedupe on (KeyNo idx45, type idx2, exchange seq idx30) before acting.

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

`contract_value` and the Capital order code (futures only) derive MECHANICALLY from the strategy's
`SYMBOL` — which declares the traded contract; data auto-aliases to the TXF series
(`references/lib.md`): `TXF` → `TX00` / 200, `MXF` → `MTX00` / 50, `TMF` → `TM0000` / 10.
`lot_size` for stocks is 1000
(整股/張) — for a strategy trading 零股 exclusively, use `lot_size: 1` and treat `nQty` as raw
shares. The order library ships as `lib/order_capital.py` (live-tested paths only — surface and
units in `references/lib.md`; do NOT hand-write SKCOM order calls anymore); reconciler wiring is
the hand-wired signed-diff pattern per `references/manager.md`, and the reconciler service needs
the `.\Administrator` ObjectName exception there (602).

### Account Snapshot Worker (`blave-agent-capital` service)

Account/position reads are split in two because the platform account_reader runs as LocalSystem,
which SKCOM's certificate check rejects (602): `lib/capital_worker.py` polls the venue over COM
every 60 s (`GetFutureRights` is rate-limited — error 1019 — 60 s measured safe) and writes
`state/capital_account.json`; `lib/account_capital.py` only reads that file (reports it stale
after 300 s). Install the worker as an NSSM service under the Administrator identity — the same
ObjectName exception as the reconciler (`references/manager.md`):

```
nssm install blave-agent-capital "<python.exe>" "C:\blave-agent\workspace\lib\capital_worker.py"
nssm set blave-agent-capital AppDirectory C:\blave-agent\workspace
nssm set blave-agent-capital ObjectName .\Administrator "<Administrator password>"
nssm set blave-agent-capital AppStdout C:\blave-agent\workspace\state\capital_worker.log
nssm set blave-agent-capital AppStderr C:\blave-agent\workspace\state\capital_worker.log
nssm set blave-agent-capital Start SERVICE_AUTO_START
nssm start blave-agent-capital
```

Resolve `<python.exe>` to the machine's actual interpreter (`where python`) and adjust the
workspace path if `BLAVE_AGENT_WORKSPACE` differs; read the password from
`C:\blave-agent\credentials\rdp_password.txt` — never reset it (Step 2). On any tick failure the
worker writes an error snapshot, backs off 30 s, and exits so NSSM restarts it with a fresh COM
session — a stale/error snapshot therefore means the service is down or the venue is failing,
never a silently-wrong number.

`lib/capital_worker.py` touches `state/heartbeat/capital_worker` at the top of each 60 s loop
tick (`references/deployment.md`'s daemon heartbeat convention). Register it once in
`state/deployments.json` so `manager/healthcheck.py` alerts on a dead worker instead of it going
silently stale:
```json
{"capital_worker": {"type": "daemon", "expect_every_minutes": 5,
                    "registered_at": "<UTC now, %Y-%m-%dT%H:%M:%S>"}}
```

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

1. Windows x64 workspace + `SKCOMVerifyDJ.exe` 檢核 passed (both simulated orders) + user signed
   the relevant declaration(s) (期貨/證券 — activation verified same-day, see Step 1) → Step 1
2. Certificate issued on this machine via RDP (credentials from the web dashboard) → Step 2
3. `SKCOMTester.exe` login OK → component/cert/agreement all good → Step 3
4. Python login `code == 0` → Step 5
5. `OnAccount` returns the expected account(s) (`TF` and/or `TS`) → Step 6
6. **Account Snapshot Worker (`blave-agent-capital` NSSM service) installed and running,
   `state/capital_account.json` fresh** → Step 8's "Account Snapshot Worker" section. **This is
   what makes the platform's web dashboard show "connected" instead of a failure card — do this
   right after Step 6/6c, before order testing or strategy wiring, not as an afterthought.**
7. One user-approved minimal live order per market being used (or intentional rejection)
   confirms the order path → Step 7a / 7b
8. 用戶確認後，方可設定 reconciler 上線（參考 `references/deployment.md`）
9. Libs are SHIPPED (verified live 2026-08-14, uid=1) — do not hand-write SKCOM calls:
   `lib/order_capital.py` (futures IOC market / whole-lot limit+market / intraday odd-lot limit;
   everything else raises NotImplementedError), `lib/capital_worker.py` (NSSM service
   `blave-agent-capital` → `state/capital_account.json`, install commands in Step 8) and
   `lib/account_capital.py` (snapshot reader). Both the worker and any reconciler service need
   the `.\Administrator` ObjectName exception (`references/manager.md`).
