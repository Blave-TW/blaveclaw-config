# examples/

Complete reference strategies — read one before writing the same kind. These are not user strategies; user strategies live in `strategies/`.

- `btc_sma_cross/` — Type A, SMA crossover, includes `scan.py` for parameter search
- `btc_ti_5min/` — Type A, Taker Intensity threshold (Blave alpha), 5min kline; `scan.py` = threshold-pair scan on a nice grid anchored on the current constants
- `tw2317_broker_zscore/` — Type A, Taiwan stock (2317) broker-flow z-score, contrarian; `scan.py` = the canonical **three-or-more-parameter** scan (pairwise coordinate descent, two rounds, one `scan.json`)
- `cl_sma/` — Type A, WTI crude oil with NYMEX settlement exit; uses `fetch_db_kline` + `settlement_signals_from_db()`
- `tsmc_ma/` — Type A, Taiwan stock (2330) SMA crossover
- `txf_ma_1m/` — Type A, Taiwan Index Futures (TXF) 1m SMA crossover
- `tw100_foreign_zscore/` — Type C, Taiwan 100-stock portfolio, foreign institutional z-score
- `twstock_momentum/` — Type C, Taiwan stock momentum, top-N equal weight

- `exports/{xq,mc,pine}/` — NOT Blave strategies: XS / PowerLanguage / Pine v6 templates the agent adapts when exporting a strategy to XQ, MultiCharts or TradingView (see `references/xq-xs.md`, `multicharts-powerlanguage.md`, `tradingview-pine.md`; lint with `lib/lint_export.py`)
