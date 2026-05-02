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

## Submit a strategy for sale

```
POST /openclaw/marketplace/strategies/submit
Content-Type: application/json

{
  "title": "Strategy Name",
  "description": "What it does and how",
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
Response: `[{id, title, price, status, created_at}, ...]`
Status values: `pending` | `approved` | `unlisted`
