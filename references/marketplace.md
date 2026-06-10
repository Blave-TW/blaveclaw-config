# Strategy Library API

Use Blave API credentials from `.env` for all requests.
Base URL: `https://api.blave.org`
Headers: `api-key: {blave_api_key}`, `secret-key: {blave_secret_key}`

## Browse

List all available strategies:
```
GET /openclaw/marketplace/strategies
```
Response: `[{id, title, description, price, category, created_at}, ...]`

Strategy detail (includes `purchased: true/false`):
```
GET /openclaw/marketplace/strategies/{id}
```

## My accessible strategies

**Flow when user asks what strategies they have, or wants to load any strategy:**
1. Call ALL THREE endpoints in parallel:
   - `GET /openclaw/marketplace/my/purchases` — purchased strategies
   - `GET /openclaw/marketplace/my/shared-with-me` — strategies others shared with you
   - `GET /openclaw/marketplace/my/official` — free official strategies (no purchase needed)
2. Merge and deduplicate by id. Show the combined list to the user
3. User picks one → `GET /openclaw/marketplace/strategies/{id}/code`
4. Save code to `/tmp/<filename>.py`
5. **Check for multi-strategy bundle** — scan the file for lines matching `# ===== STRATEGY \d+:`:
   - If found: split into separate files (see "Deploying a multi-strategy bundle" below), security scan and deploy each one individually
   - If not found: proceed as single strategy
6. **Security scan** — run `python3 lib/security_check.py /tmp/<filename>.py`
   - Exit 0 (clean) → move to `strategies/<filename>.py` and proceed
   - Exit 1 (warnings) → show findings to user, ask for confirmation; if confirmed, move to `strategies/` and run
   - Exit 2 (critical) → show findings, delete `/tmp/<filename>.py`, do NOT run
7. `python3 strategies/<filename>.py`

Purchases and shared-with-me are separate lists — checking only purchases will miss shared strategies.

## Deploying a multi-strategy bundle

When the downloaded code contains `# ===== STRATEGY N: <name> =====` markers, treat it as a bundle:

1. Split the code at each `# ===== STRATEGY N:` line into N separate strings
2. Save each to `/tmp/<name_slug>.py` (derive slug from the strategy name after the colon)
3. Run `python3 lib/security_check.py` on **each** file separately
   - If any file exits 2 (critical) → delete that file, do NOT run it; continue with the others
   - If any file exits 1 (warnings) → show findings, ask user for confirmation before moving
4. Move approved files to `strategies/<name_slug>.py`
5. Run each: `python3 strategies/<name_slug>.py`

Example: a file containing two strategies marked as `# ===== STRATEGY 1: BTC SMA Cross =====` and `# ===== STRATEGY 2: ETH RSI Fade =====` should produce `strategies/btc_sma_cross.py` and `strategies/eth_rsi_fade.py`.

## Load official strategies (free)

List all official Blave strategies — no purchase required:
```
GET /openclaw/marketplace/my/official
```
Response: `[{id, title, description, category, created_at}, ...]`

Code is freely accessible:
```
GET /openclaw/marketplace/strategies/{id}/code
```

## Load purchased strategies

List purchased strategies:
```
GET /openclaw/marketplace/my/purchases
```

Fetch strategy code (requires purchase or is_official):
```
GET /openclaw/marketplace/strategies/{id}/code
```
Response: `{"code": "..."}` — save to `.py` and run with `python3`.

## Load shared strategies

List strategies shared with you:
```
GET /openclaw/marketplace/my/shared-with-me
```
Response: `[{id, title, description, category, shared_at}, ...]`

**Flow when user says a strategy was shared with them, or asks what strategies they have access to:**
1. `GET /openclaw/marketplace/my/shared-with-me` — show the list
2. User picks one → `GET /openclaw/marketplace/strategies/{id}/code`
3. Save code to `/tmp/<filename>.py` (NOT strategies/ yet)
4. **Security scan** — run `python3 lib/security_check.py /tmp/<filename>.py`
   - Exit 0 (clean) → move to `strategies/<filename>.py` and proceed
   - Exit 1 (warnings) → show findings to user, ask for confirmation; if confirmed, move to `strategies/` and run
   - Exit 2 (critical) → show findings, delete `/tmp/<filename>.py`, do NOT run

## Strategy report (performance data)

> **Admin only (user_id == 1)** for official strategies. Community strategy owners can submit their own report.

Get backtest performance for a strategy:
```
GET /openclaw/marketplace/strategies/{id}/report
```
Response: `{total_return, annual_return, sharpe, max_drawdown, pnl_image_url, backtest_start, backtest_end}`

Submit metrics:
```
POST /openclaw/marketplace/strategies/{id}/report
Body: {symbol, interval, total_return, annual_return, sharpe, max_drawdown, backtest_start, backtest_end}
```
All fields required. **Do not include `pnl_curve`** — P&L chart is displayed as an uploaded image, not rendered from data.

Upload P&L chart image (displayed in strategy modal):
```
POST /openclaw/marketplace/strategies/{id}/report/image
Content-Type: multipart/form-data
Field: image = pnl.png
```
Response: `{"status": "ok", "pnl_image_url": "https://..."}`

**Admin flow — after running backtest:**
1. `python3 strategies/{name}/strategy.py` → generates `strategies/{name}/pnl.png` + `strategies/{name}/stats.json`
2. Read `stats.json` for metrics. Compute `annual_return` from total return + date range if not present.
3. POST metrics to `POST /strategies/{id}/report`
4. POST `strategies/{name}/pnl.png` to `POST /strategies/{id}/report/image`

## Admin endpoints (user_id == 1 only)

List pending community submissions:
```
GET /openclaw/marketplace/admin/pending
```

Approve a pending strategy (makes it public):
```
POST /openclaw/marketplace/admin/strategies/{id}/approve
```

Reject a strategy (sets status to unlisted):
```
POST /openclaw/marketplace/admin/strategies/{id}/reject
```

Create an official strategy (approved + public + is_official immediately):
```
POST /openclaw/marketplace/admin/strategies/official
Body: {title, description, category, code}
```

## Description format (required for all uploads)

Description is **plain text only** — 1–2 sentences describing the strategy logic. No parameter values, no markdown sections.

Example: `台股動能輪動：每週從跨產業台股中選出動能最強的前 30 支等權持有，週末調倉，自動跟隨強勢板塊輪動。`

If the strategy uses a **custom lib** (not standard lib), append a brief note:
```
Custom lib: lib/orders_bybit.py — place_order("BUY"|"SELL"|"SHORT"|"COVER"). Requires BYBIT_API_KEY, BYBIT_API_SECRET.
```

## Submit a strategy for sale

```
POST /openclaw/marketplace/strategies/submit
Content-Type: application/json

{
  "title": "Strategy Name",
  "description": "<structured description — see format above>",
  "price": 300,
  "category": "trend",
  "code": "...full source code..."
}
```
Status starts as `pending`. Blave reviews and publishes it.

Check submission status:
```
GET /openclaw/marketplace/my/submissions
```
Response: `[{id, title, price, status, visibility, created_at}, ...]`
Status values: `pending` | `approved` | `unlisted`

## Submitting a multi-strategy bundle

Pack two or more strategies into a single file using the `# ===== STRATEGY N: <name> =====` delimiter. Submit via the normal endpoint — no new endpoint needed.

**File format:**
```python
# ===== STRATEGY 1: BTC SMA Cross =====
MODE          = "live"
STRATEGY_NAME = "btc_sma_cross"
SYMBOL        = "BTCUSDT"
# ... full strategy 1 code ...

# ===== STRATEGY 2: ETH RSI Fade =====
MODE          = "live"
STRATEGY_NAME = "eth_rsi_fade"
SYMBOL        = "ETHUSDT"
# ... full strategy 2 code ...
```

**Description format** — repeat the structured block once per strategy, separated by `---`:
```
## Strategy logic
[Strategy 1 logic]

## Parameters
- SYMBOL: BTCUSDT, INTERVAL: 1h, ...

## Standard lib used
[...]

---

## Strategy logic
[Strategy 2 logic]

## Parameters
- SYMBOL: ETHUSDT, INTERVAL: 4h, ...

## Standard lib used
[...]
```

**Submit:**
```
POST /openclaw/marketplace/strategies/submit
Content-Type: application/json

{
  "title": "BTC SMA Cross + ETH RSI Fade Bundle",
  "description": "<structured description for both strategies>",
  "price": 500,
  "category": "bundle",
  "code": "# ===== STRATEGY 1: BTC SMA Cross =====\n...\n\n# ===== STRATEGY 2: ETH RSI Fade =====\n..."
}
```

Status starts as `pending`. Blave reviews and publishes it. Buyer downloads the single file and their agent automatically splits and deploys both strategies.

## Private strategies

Upload a private strategy (no review, immediately accessible):
```
POST /openclaw/marketplace/strategies/private
Content-Type: application/json

{
  "title": "My Private Strategy",
  "description": "<structured description — see format above>",
  "category": "trend",
  "code": "...full source code..."
}
```
Response: `{"status": "ok", "strategy_id": 123}`

**Delete a private strategy:**
```
DELETE /openclaw/marketplace/strategies/{id}
```
Response: `{"status": "ok"}`
- Owner only. Private strategies only — public strategies cannot be deleted.
- Also removes all shares associated with the strategy.

**Flow when user wants to share a private strategy with specific users:**
1. If the strategy isn't uploaded yet → `POST /openclaw/marketplace/strategies/private` first
2. `POST /openclaw/marketplace/strategies/{id}/share` with the target user IDs
3. Confirm back to the user which strategy ID was shared with which UIDs
4. Tell the user to inform the recipient: **請對方跟他的 BlaveClaw agent 說「幫我看一下有沒有人分享策略給我」**，agent 會自動去 shared-with-me 撈取並載入。

Share with specific user IDs:
```
POST /openclaw/marketplace/strategies/{id}/share
Content-Type: application/json

{"user_ids": [456, 789]}
```

Remove a user's access:
```
DELETE /openclaw/marketplace/strategies/{id}/share
Content-Type: application/json

{"user_id": 456}
```

View share list (owner only):
```
GET /openclaw/marketplace/strategies/{id}/shares
```
Response: `{"shares": [{"user_id": 456, "shared_at": "..."}]}`

View strategies shared with you:
```
GET /openclaw/marketplace/my/shared-with-me
```

Download code (works for owned, purchased, or shared strategies):
```
GET /openclaw/marketplace/strategies/{id}/code
```
Response: `{"code": "..."}` — save to `strategies/` directory and run with `python3`.

If execution fails with `ImportError` on a custom lib module, read the strategy's description "Custom lib dependencies" section and create the missing file in `lib/` before re-running.
