# examples/exports/mc/

Classic MultiCharts PowerLanguage **signal** templates for the export feature
(`references/multicharts-powerlanguage.md`). Copy the closest one, change Inputs /
indicator lines / conditions, keep the `// --- indicators / signal / orders ---` markers,
lint, save to `strategies/<name>/exports/mc.txt`. None of these were compiled here —
every file says so in its header, and so must yours.

- `sma_cross_long.txt` — long-only SMA state (fast > slow long, fast < slow flat); mirrors `examples/btc_sma_cross`, `tsmc_ma`, `txf_ma_1m` minus the settlement mask
- `sma_cross_long_short.txt` — stop-and-reverse SMA (+1 / -1, always in the market); shows MC reverse-on-opposite-entry semantics
- `nbar_breakout.txt` — Donchian breakout with `Highest(High,N)[1]` / `Lowest(Low,M)[1]` (prior-bars window, next-bar-open fill; buy-stop variant noted)
- `rsi_mean_reversion.txt` — RSI oversold entry, dead-zone hold, exit above a recovery level (Wilder RSI caveat)
- `fixed_stop_target.txt` — percent stop-loss + profit target as resting price orders off `EntryPrice`; currency-based `SetStopLoss`/`SetProfitTarget` alternative and their position-vs-contract basis in the header
- `session_time_filter.txt` — intraday time window + forced flat before session end; UTC bar-open (Blave) vs bar-close chart time (MC) conversion spelled out
- `trailing_stop.txt` — percent trailing stop off the highest high since entry, tracked in a Variable; `SetPercentTrailing` noted as a different rule
- `threshold_long_short.txt` — four-threshold long/short with flat band, exits before entries (`MarketPosition` as the loop state)
