# Strategy Library API

Use Blave API credentials from `.env` for all requests.
Base URL: `https://api.blave.org`
Headers: `api-key: {blave_api_key}`, `secret-key: {blave_secret_key}`

> **「安裝 / install / 載入 / 部署 / deploy 我（買的）策略」= this API.**
> Loading a purchased/shared/official strategy is ALWAYS a plain HTTP call to the endpoints below — the user is identified by the `.env` Blave key, so they never supply an identifier, code, or install command. The verb the user used (安裝/載入/部署/install/load/deploy) does not change the flow: for a purchased strategy, go straight to `GET /openclaw/marketplace/my/purchases`. (Skills are a separate runtime layer, provisioned automatically — never relevant to loading a strategy.)

## Strategy categories

The **Strategy Library** is the umbrella system. Within it, every strategy falls into exactly one of four categories. "**Marketplace**" specifically means the *paid public store* (category 2) — not the whole library.

| Category | Source | Cost | Who can see / download code | List endpoint |
|---|---|---|---|---|
| **Official** | Listed by Blave | Free | All users, no purchase needed | `GET /my/official` |
| **Marketplace (paid)** | Listed for sale by other users | Paid | Anyone can browse; code downloadable only **after purchase** | Browse `GET /strategies`; after buying `GET /my/purchases` |
| **Shared** | Privately shared to specific users | Free | Only the named recipients; not public, not browsable | `GET /my/shared-with-me` |
| **Private** | Uploaded by you | — | Only you (plus anyone you explicitly share with) | `GET /my/private` |

Distinguishing dimensions: **source** (who created it), **cost** (free / paid), **visibility** (public / named recipients / yourself only).

- **"What strategies can I use / load?"** → the first three categories (official + purchases + shared-with-me) are the strategies authored by others that you can load. Merge and dedupe them, then let the user pick. See [My accessible strategies](#my-accessible-strategies).
- **"Which private strategies have I uploaded?"** → the fourth category, via `GET /my/private`. See [Private strategies](#private-strategies).
- A private strategy can be **promoted** to the Marketplace (submit for sale) or **shared** with specific users, but by default it is visible to you alone.

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
4. Save code to `tmp/<filename>.py` (`mkdir -p tmp` first — it is gitignored and absent on a fresh machine)
5. **Check for multi-strategy bundle** — scan the file for lines matching `# ===== STRATEGY \d+:`:
   - If found: split into separate files (see "Deploying a multi-strategy bundle" below), security scan and deploy each one individually
   - If not found: proceed as single strategy
6. **Security scan** — run `python3 lib/security_check.py tmp/<filename>.py`
   - Exit 0 (clean) → move to `strategies/<name>/strategy.py` and proceed (`mkdir -p strategies/<name>`; `<name>` = the file's `STRATEGY_NAME`)
   - Exit 1 (warnings) → show findings to user, ask for confirmation; if confirmed, move to `strategies/<name>/strategy.py` and run
   - Exit 2 (critical) → show findings, delete `tmp/<filename>.py`, do NOT run
   - The layout is always `strategies/<name>/strategy.py`, never a flat `strategies/<name>.py` — the template's `sys.path.insert(0, parent.parent.parent)` and the runner's `stats.json` output both assume that depth; a flat file dies with `No module named 'lib'`
7. **Quality scan** (Type A and C — skip only Type B) — run `python3 lib/quality_check.py strategies/<name>/strategy.py`
   - Exit 0 (clean) → proceed
   - Exit 1 (warnings) → show findings to user, ask for confirmation before running
   - Exit 2 (critical) → show findings, do NOT run — a broken `compute_signals()` contract means the backtest about to run produces garbage results
8. **Run it — MANDATORY, never skip:** `python3 strategies/<name>/strategy.py`. Every run writes `strategies/<name>/stats.json` (metrics + daily returns); that file is what makes the strategy selectable in the web workspace's 下單設定 › 選擇策略 picker — a downloaded-but-never-run strategy is invisible there and reads as a broken install. Report the resulting stats to the user.

Purchases and shared-with-me are separate lists — checking only purchases will miss shared strategies.

## Forking a strategy (use one as a base for the user's own)

**Fork ≠ install.** When the user wants an existing strategy as a *starting point to modify* — 「用 X 當底」, "fork", "copy it into my own strategy" — do NOT run the install flow above. (A web-workspace library pick shaped like 「幫我下載官方策略「{title}」（#{id}），跑一次回測看看結果」 is a plain install, NOT a fork — run the install flow above with the given id.) Instead:

1. Identify the base strategy: if the message names it (title or #id), use that; otherwise list accessible strategies (official + purchases + shared-with-me, merged) and let the user pick.
2. `GET /openclaw/marketplace/strategies/{id}/code` → save to `tmp/<filename>.py`.
3. **Security scan** — `python3 lib/security_check.py tmp/<filename>.py`, same exit rules as installing (exit 2 → delete, do not proceed).
4. **Rename before anything runs.** Pick a NEW `STRATEGY_NAME` (ask the user or default to `<orig>_custom`), set `DISPLAY_NAME`/`DESCRIPTION` to describe the user's variant, set `MODE = "backtest"` (a fork is a draft — never leave the source's `"live"` in place), and save to `strategies/<new_name>/strategy.py`. Never overwrite or collide with an installed copy of the original — the fork is a separate strategy from day one.
5. **Run the baseline backtest immediately** (Type A/C): `python3 lib/quality_check.py strategies/<new_name>/strategy.py` first, then `python3 strategies/<new_name>/strategy.py`. This baseline run is part of the fork request itself, not an extra iteration under Iteration Brakes. It writes `stats.json`, which also makes the fork selectable in the 下單設定 › 選擇策略 picker — without it the fork is invisible there.
6. **A fork is a draft, not a deployment.** Do not schedule it, do not add it to the 下單組合. From here it is the user's own strategy: follow `references/strategy-code.md`, and backtest again after any change before live use. Iteration Brakes apply as usual after the baseline run. (One exception: a fork built to replace a LIVE strategy is deployed and funded at the end of the fork-and-switch flow in `references/strategy-code.md` › *Editing a live strategy* — that flow's own steps govern when.)
7. Report the baseline stats, tell the user what the base strategy does (from its description/report), and ask what they want to change — do not invent modifications on your own.

## Deploying a multi-strategy bundle

When the downloaded code contains `# ===== STRATEGY N: <name> =====` markers, treat it as a bundle:

1. Split the code at each `# ===== STRATEGY N:` line into N separate strings
2. Save each to `tmp/<name_slug>.py` (derive slug from the strategy name after the colon)
3. Run `python3 lib/security_check.py` on **each** file separately
   - If any file exits 2 (critical) → delete that file, do NOT run it; continue with the others
   - If any file exits 1 (warnings) → show findings, ask user for confirmation before moving
4. Move approved files to `strategies/<name_slug>/strategy.py` (one directory per strategy) — the directory name MUST equal the file's `STRATEGY_NAME` (the runner writes `stats.json` under `strategies/<STRATEGY_NAME>/`, and the web only sees a backtest whose `stats.json` sits next to its `strategy.py`): set `STRATEGY_NAME = "<name_slug>"` in each split file
5. Run `python3 lib/quality_check.py strategies/<name_slug>/strategy.py` on each Type A/C file (skip only Type B) — exit 1: confirm with user; exit 2: do NOT run that file
6. Run each: `python3 strategies/<name_slug>/strategy.py`

Example: a file containing two strategies marked as `# ===== STRATEGY 1: BTC SMA Cross =====` and `# ===== STRATEGY 2: ETH RSI Fade =====` should produce `strategies/btc_sma_cross/strategy.py` and `strategies/eth_rsi_fade/strategy.py`.

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
3. Save code to `tmp/<filename>.py` (NOT strategies/ yet; `mkdir -p tmp` first)
4. **Security scan** — run `python3 lib/security_check.py tmp/<filename>.py`
   - Exit 0 (clean) → move to `strategies/<name>/strategy.py` and proceed (`<name>` = the file's `STRATEGY_NAME`)
   - Exit 1 (warnings) → show findings to user, ask for confirmation; if confirmed, move to `strategies/<name>/strategy.py` and run
   - Exit 2 (critical) → show findings, delete `tmp/<filename>.py`, do NOT run
5. **Quality scan** (Type A and C — skip only Type B) — run `python3 lib/quality_check.py strategies/<name>/strategy.py`; exit 1: confirm with user before running; exit 2: do NOT run
6. **Run it — MANDATORY, never skip:** `python3 strategies/<name>/strategy.py` — writes `stats.json`, which the 下單設定 › 選擇策略 picker requires (same as step 8 of the install flow above). Report the stats to the user.

## Strategy report (performance data)

> **Admin only (user_id == 1)** — for ALL strategies, official or community. Sellers cannot submit
> their own report; they send numbers to admin out-of-band.

Get backtest performance for a strategy:
```
GET /openclaw/marketplace/strategies/{id}/report
```
Response: `{total_return, annual_return, sharpe, max_drawdown, pnl_image_url, backtest_start, backtest_end}`

Submit metrics + optional P&L chart image in one call:
```
POST /openclaw/marketplace/strategies/{id}/report
Content-Type: multipart/form-data
Fields: total_return, annual_return, sharpe, max_drawdown, backtest_start, backtest_end (all required)
        symbol, interval (optional)
        image = pnl.png (optional file field)
```
**Do not include `pnl_curve`** — P&L chart is displayed as an uploaded image, not rendered from data.
Response: `{"status": "ok", "strategy_id": ..., "pnl_image_url": "https://..." | null}`

**Admin flow — after running backtest:**
1. `python3 strategies/{name}/strategy.py` → generates `strategies/{name}/pnl.png` + `strategies/{name}/stats.json`
2. Read `stats.json` for metrics. Compute `annual_return` from total return + date range if not present.
3. POST metrics + `strategies/{name}/pnl.png` together to `POST /strategies/{id}/report` (multipart)

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
Custom lib: lib/orders_bybit.py — place_order("BUY"|"SELL"|"SHORT"|"COVER"). Requires BYBIT_API_KEY, BYBIT_SECRET_KEY.
```

## Submit a strategy for sale

**Before submitting a Type A or C strategy** (skip only for Type B — no backtest, no FEE),
run `python3 lib/quality_check.py strategies/<name>/strategy.py` — catches a `FEE=0` backtest
(inflates the Sharpe/return you're about to advertise to buyers), an unfilled
`compute_signals()` template, and a TAIFEX futures strategy missing the mandatory
`txf_settlement_mask` (its backtest books fake roll-gap PnL). Fix any findings before
calling the endpoint below.

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

Private strategies are your own uploads — only you can see them (plus anyone you explicitly share with). They are free, skip review, and are immediately accessible.

**List your private strategies:**
```
GET /openclaw/marketplace/my/private
```
Response: `{"strategies": [{id, title, description, category, created_at}, ...]}`
This is the only category not covered by the three "accessible strategies" endpoints — use it when the user asks which strategies they have uploaded, or to list their own private strategies.

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
4. Tell the user to inform the recipient: **請對方跟他的 Blave Agent 說「幫我看一下有沒有人分享策略給我」**，agent 會自動去 shared-with-me 撈取並載入。

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
Response: `{"code": "..."}` — save to `strategies/<name>/strategy.py` and run with `python3 strategies/<name>/strategy.py`.

If execution fails with `ImportError` on a custom lib module, read the strategy's description "Custom lib dependencies" section and create the missing file in `lib/` before re-running.
