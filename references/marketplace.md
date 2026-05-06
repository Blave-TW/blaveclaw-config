# Strategy Marketplace API

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

## Load purchased strategies

List purchased strategies:
```
GET /openclaw/marketplace/my/purchases
```

Fetch strategy code (requires purchase):
```
GET /openclaw/marketplace/strategies/{id}/code
```
Response: `{"code": "..."}` — save to `.py` and run with `python3`.

**Flow when user wants to run a purchased strategy:**
1. `GET /openclaw/marketplace/my/purchases` — show the list
2. User picks one → `GET /openclaw/marketplace/strategies/{id}/code`
3. Save to file → `python3 filename.py`

## Description format (required for all uploads)

Every strategy uploaded to the marketplace (private or submit) must use this structured description so recipients can reconstruct any missing custom lib:

```
## Strategy logic
[Entry/exit rules in plain language]

## Parameters
- SYMBOL, INTERVAL, BUDGET, etc.

## Standard lib used
[List of functions from lib/data, lib/execute, lib/report, lib/analysis]

## Custom lib dependencies
### lib/filename.py — function_name(param: type, ...) -> return_type
[What it does, accepted values for each param, side effects, required env vars or headers]
[Omit this section if the strategy only uses standard lib]
```

Example custom lib entry:
```
## Custom lib dependencies
### lib/orders_bybit.py — place_order(side: str)
Places a market order on Bybit. side accepts "BUY" | "SELL" | "SHORT" | "COVER".
BUY/SHORT open a full-budget position; SELL/COVER close the entire position.
Requires env vars: BYBIT_API_KEY, BYBIT_API_SECRET. Must include header: referer: Ue001036
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
