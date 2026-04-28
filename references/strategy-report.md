# Strategy Report API

After every backtest AND every cron run (live/paper), upload the report so the user can track it on the Blave website.

## POST — upload / update report

Endpoint: POST https://api.blave.org/openclaw/strategy/report
Headers: api-key: KEY, secret-key: SECRET  (read blave_api_key / blave_secret_key from .env)
         Content-Type: application/json
         Content-Encoding: gzip  (required when payload is large — always use gzip for safety)

Body (JSON):
{
  "strategy_name": "MA Cross",
  "symbol": "BTCUSDT",
  "interval": "1h",
  "mode": "backtest" or "paper" or "live",
  "code": "...full source code...",
  "trades": [
    {"time": 1700000000, "action": "BUY",  "price": 50000},
    {"time": 1700003600, "action": "SELL", "price": 51000}
  ],
  "klines": [[time, open, high, low, close], ...],
  "indicators": [
    {"name": "RSI", "type": "histogram", "data": [[time, value], ...]}
  ],
  "returns": [0.002, -0.001, 0.003, ...]
}

Notes:
- klines: use [[r["time"],r["open"],r["high"],r["low"],r["close"]] for r in rows] from GET /kline — include full OHLC, do NOT fill all fields with close price
- returns: bar-level position-weighted strategy returns (strat_ret, no NaN). Used for Sharpe, MDD, equity curve. See TEMPLATE.py compute_strat_returns()
- interval: use the same format as the Blave /kline period param (e.g. "5min", "1h", "4h", "1d")
- POST with the same strategy_name overwrites the previous report

Rules:
- klines, indicators, and returns MUST cover the full backtest/live period — do NOT truncate or cap any array (no [:N] slicing)
- P&L stats are calculated server-side — do NOT compute them yourself

## DELETE — remove report

DELETE https://api.blave.org/openclaw/strategy/report
Headers: api-key: KEY, secret-key: SECRET  (read from .env)
Body: {"strategy_name": "MA Cross"}
