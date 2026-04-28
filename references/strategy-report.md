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
    {"name": "RSI", "type": "histogram", "data": [[time, value], ...]},
    {"name": "MA20", "type": "line",      "data": [[time, value], ...]}
  ],
  "returns": [0.002, -0.001, 0.003, ...]
}

Notes:
- klines: use [[r["time"],r["open"],r["high"],r["low"],r["close"]] for r in rows] from GET /kline — include full OHLC, do NOT fill all fields with close price
- returns: bar-level position-weighted strategy returns (strat_ret, no NaN). Used for Sharpe, MDD, equity curve. See TEMPLATE.py compute_strat_returns()
- interval: use the same format as the Blave /kline period param (e.g. "5min", "1h", "4h", "1d")
- POST with the same strategy_name overwrites the previous report

CRITICAL — indicators format:
Each element MUST be a series object: {"name": str, "type": "line"|"histogram", "data": [[time, value], ...]}
The server will reject any other format with HTTP 400.

WRONG (per-bar dict — will be rejected):
  indicators = [{"hc": 0.37, "time": 1682640000}, {"hc": 0.35, "time": 1682643600}, ...]

CORRECT (one object per indicator series):
  indicators = [{"name": "HC", "type": "line", "data": [[1682640000, 0.37], [1682643600, 0.35], ...]}]

How to build indicators correctly in your strategy loop:
  hc_data = []
  for candle in candles:
      hc = compute_hc(candle)
      hc_data.append([candle["time"], hc])
  indicators = [{"name": "HC", "type": "line", "data": hc_data}]

Rules:
- klines, indicators, and returns MUST cover the full backtest/live period — do NOT truncate or cap any array (no [:N] slicing)
- P&L stats are calculated server-side — do NOT compute them yourself

## DELETE — remove report

DELETE https://api.blave.org/openclaw/strategy/report
Headers: api-key: KEY, secret-key: SECRET  (read from .env)
Body: {"strategy_name": "MA Cross"}
