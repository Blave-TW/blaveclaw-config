# SinoPac (永豐金) Broker — Agent Reference

Use this document when a user asks to connect their SinoPac account to Blave Agent.

**Stock order placement is implemented in `lib/order_sinopac.py` — import it, never hand-write
Shioaji order calls in a strategy or one-off script.** Every rule in the Field-Verified Lessons
section below was learned from real rejected orders on a live account; the lib encodes all of
them.

---

## Supported Products

| Product | Symbol Example | Notes |
|---------|---------------|-------|
| 台灣期貨 TXF | `TXFR1` | 台指期近月，1 口 = 200 × 指數點位 (TWD) |
| 台灣股票 Stocks | `2330` (TSE) | Board lot (整股) = 1000 shares; odd lot (零股) = 1-999 shares. `lib/order_sinopac.py` trades odd lot. |

---

## Field-Verified Lessons (live account, 2026-07)

The first live Taiwan-stock deployment lost **three rounds of orders to silent rejections** —
Shioaji's `place_order` does not raise on rejection; a dead order just sits with status `Failed`
or never leaves `PendingSubmit`. Each lesson below cost a real debugging round:

1. **`SINOPAC_LIVE=true` must be set in `.env`, or nothing is real.** Without it the connection
   runs in simulation mode against a fake account whose holdings have nothing to do with the
   user's real account (the simulation account showed 24 fake positions worth $24.4M while the
   real account was empty; sell orders bounced with 「集保賣出餘股數不足」 against holdings that
   didn't exist). Never diagnose position mismatches before confirming which mode you're in.
2. **CA certificate must be activated, and activation happens AFTER `login()`.** Live orders
   before activation fail with `CA not activated for: <person_id>`. If `activate_ca()` itself
   fails, the user must first activate the certificate in SinoPac's desktop app — the agent
   cannot do this remotely. `lib/order_sinopac.py` raises at connect time so this surfaces
   before the first order, not as a rejected order.
3. **Enums, never strings.** An order built with the bare string `"MKT"` as `order_type` was
   silently rejected — it never entered the broker's system and sat unnoticed for 13 hours. Use
   `sj.OrderType.ROD`, `sj.StockPriceType.MKT`, `sj.StockOrderLot.Odd` (stocks) /
   `sj.FuturesPriceType.MKT`, `sj.FuturesOCType.Auto` (futures).
4. **Share counts under 1000 MUST be odd-lot orders.** A Common-lot (整股) order's `quantity` is
   in 張 (1000-share board lots); submitting a share count (e.g. 66 shares of 2303) as a
   Common-lot order silently vanishes. Use `order_lot=sj.StockOrderLot.Odd`
   (verified live on Shioaji 1.5.4; the enum also has `IntradayOdd`, which the verified
   deployment did not use).
5. **Always confirm against order status, never trust `place_order`'s return.** Poll
   `api.update_status(account)` + `api.list_trades()` until the order is acknowledged
   (`Submitted`/`Filled`); `Failed`/`Cancelled`/`Inactive` means rejected — read `status.msg`.
   All three incidents above would have been caught immediately by this check.
6. **Warning-flagged stocks (警示股)** may require pre-funded settlement (「警示股預收條件」) —
   surface the status message to the user instead of retrying.
7. **Orders placed outside market hours rest as `PendingSubmit`/`PreSubmitted` until the next
   session.** That is not a rejection — report it honestly rather than resubmitting.

---

## Step 0 — Determine Account Type

**Ask the user first:**
> 你要交易台灣期貨（台指期 TXF）還是台灣股票？或兩者都要？

- Futures account → `futopt_account`; margin via `margin()` → `equity`
- Stock account → `stock_account`; balance via `account_balance()`
- Most users have both under the same login

---

## Step 1 — Apply for API Key

**URL:** https://eservice.sinotrade.com.tw/
(永豐金證券 e-MANAGER 開發人員中心)

**Steps:**
1. Log in to e-MANAGER (requires an active SinoPac account)
2. Open「API 金鑰管理」
3. Click「申請 API 金鑰」
4. Record the `API Key` and `Secret Key` (the Secret Key is not shown again after leaving the page)
5. **Digital certificate (CA, required for live trading):**
   - Download and install the SinoPac CA certificate
   - Remember the certificate password (`sinopac_ca_passwd`)
   - Certificate file usually lands in `~/.shioaji/` or a user-chosen path
   - The CA password defaults to the user's national ID (身分證字號), which is why
     `sinopac_person_id` can usually be omitted (the lib falls back to `sinopac_ca_passwd`)
   - If live orders later fail with `CA not activated`, the user must activate the certificate
     in SinoPac's desktop app (e-Leader/iLeader) first

**Simulation mode:** no CA needed; test connectivity with `simulation=True` — but remember
lesson 1: simulation account state is fake.

---

## Step 2 — Collect Credentials & Write `.env`

**Ask the user (in one message):**
> 請提供你的永豐 API Key 和 Secret Key（從 e-MANAGER 取得）。
> 如果你有數位憑證，也請告訴我憑證檔案的路徑和密碼。

The agent writes `.env` directly — the user never edits files:

```python
with open('.env', 'a') as f:
    f.write(f"\nsinopac_api_key={api_key}\n")
    f.write(f"sinopac_secret_key={secret_key}\n")
    if ca_path:
        f.write(f"sinopac_ca_path={ca_path}\n")
        f.write(f"sinopac_ca_passwd={ca_passwd}\n")
```

For live trading, additionally set `SINOPAC_LIVE=true` — **only after the user has explicitly
confirmed they want real orders.** Confirm the keys are stored and will not be displayed again.

---

## Step 3 — Test Connection

Verify the account with `simulation=True`:

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

**Success:** prints the accounts list with stock/futopt accounts.
**Common failures:** API key not enabled, wrong secret, system clock drift (>30 s → login
timeout).

Live-mode connection (login, then CA activation — order matters, lesson 2):

```python
api = sj.Shioaji(simulation=False)
accounts = api.login(
    api_key=env['sinopac_api_key'],
    secret_key=env['sinopac_secret_key'],
    fetch_contract=True,
)
api.activate_ca(
    ca_path=env['sinopac_ca_path'],
    ca_passwd=env['sinopac_ca_passwd'],
    person_id=env.get('sinopac_person_id') or env['sinopac_ca_passwd'],
)
```

---

## Step 4 — Check Account Equity & Positions

```python
# Futures account equity
margin = api.margin(account=api.futopt_account)
print("Futures equity:", margin.equity, "TWD")

# Stock account balance
balance = api.account_balance(account=api.stock_account)
print("Stock balance:", balance[0].acc_balance if balance else 0, "TWD")

# Positions — unit=Share so odd-lot holdings are counted in shares
fut_positions = api.list_positions(api.futopt_account)
stk_positions = api.list_positions(api.stock_account, unit=sj.Unit.Share)
print("Futures positions:", fut_positions)
print("Stock positions:", stk_positions)
```

---

## Step 5 — Wire into Portfolio

Add a sinopac route in `portfolio_config.json`'s `exchanges`:

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

Wire `manager/reconciler.py`'s `get_positions()` / `place_order()` through
`lib/order_sinopac.py` (`get_sinopac_positions` / `place_order_sinopac`) — see
`references/lib.md` § order_sinopac.

---

## Trading Hours

| Market | Hours (台灣時間) |
|--------|-----------------|
| 台灣股票 | 09:00 – 13:30 (Mon–Fri) |
| 台指期日盤 | 08:45 – 13:45 (Mon–Fri) |
| 台指期夜盤 | 15:00 – 05:00 (Mon–Fri) |

---

## Order Placement

### Stocks — use the lib, do not hand-roll

```python
from dotenv import dotenv_values
from lib.order_sinopac import place_order_sinopac, get_sinopac_positions

env = dotenv_values('.env')

# Reconciler-style: diff in TWD (>0 buy, <0 sell); splits into ≤999-share
# odd-lot market orders, polls each to acknowledgement, raises SinopacError
# on rejection (with the broker's message) instead of failing silently.
result = place_order_sinopac(env, '2330', 10_000, client_tag='twm7')
```

For a single odd-lot order with explicit share count, use
`place_odd_lot_order(env, symbol, 'buy'|'sell', shares, client_tag=...)`. Both functions
return exchange-confirmed fill data (`status`, `filled_qty`, `avg_fill_price`, `msg`) —
report those numbers, never the intent. Full API: `references/lib.md`.

### TXF futures (raw Shioaji — no lib yet, not live-verified)

```python
contract = api.Contracts.Futures.TXF.TXFR1  # near-month, auto-rolls
order = api.Order(
    action=sj.Action.Buy,
    price=0,                                # 0 for market order
    quantity=1,                             # 口數
    price_type=sj.FuturesPriceType.MKT,     # enum, never the string "MKT" (lesson 3)
    order_type=sj.OrderType.IOC,            # IOC for market orders
    octype=sj.FuturesOCType.Auto,           # Auto = open/close decided by system
    account=api.futopt_account,
)
trade = api.place_order(contract, order)
# MANDATORY: confirm — place_order does not raise on rejection (lesson 5)
api.update_status(api.futopt_account)
assert str(trade.status.status) not in ('Failed', 'Cancelled', 'Inactive'), trade.status.msg
```

No futures deployment has run live yet — when one does, harvest the working pattern into
`lib/order_sinopac.py` alongside the stock functions.

---

## Limits & Gotchas

| Limit | Value |
|-------|-------|
| Max concurrent connections | 5 |
| Quote query rate | 50 / 5 s |
| Order rate | 250 / 10 s |
| Over-rate penalty | 1-minute suspension |
| Max clock drift | 30,000 ms (login timeout beyond this) |

**Notes:**
- Simulation mode (`simulation=True`) receives no order-report callbacks; only live accounts do
- `fetch_contract=True` downloads all contract data at login (~5-10 s); contract lookups fail
  until it completes
- One Shioaji connection per account at a time; multi-process setups must each login/logout
- System clock must be accurate — NTP sync fixes login timeouts
- `TXFR1` always points to the near-month contract (auto-rolls after settlement)

---

## Verification Checklist for Agent

After the user completes setup, in order:
1. Connection test (simulation mode) → accounts list visible
2. Query account equity → confirm equity > 0
3. Query positions → no exceptions
4. Run `python3 manager/snapshot.py` → Telegram daily report includes sinopac equity
5. Only after the user confirms: enable the reconciler (see `references/deployment.md`).
   Live orders go through `lib/order_sinopac.py`, which requires `SINOPAC_LIVE=true` and an
   activated CA certificate (lessons 1-2).
