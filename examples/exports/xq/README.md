# examples/exports/xq/

XS (XQ 全球贏家 自動交易腳本) templates for the "export strategy code" flow in `references/xq-xs.md`. Adapt one of these — never write XS from scratch. All use `SetPosition` / `Position` / `Filled` (never EasyLanguage `Buy`/`Sell` statements) and put exits before entries, because XS executes only the first trading instruction per pass. None has been compiled here; `// UNVERIFIED` marks the lines not confirmed against xshelp.

- `sma_cross_long.xs` — SMA golden/death cross, long-only (↔ `examples/tsmc_ma/`, `examples/btc_sma_cross/`)
- `sma_cross_long_short.xs` — SMA cross, always in the market, one-instruction flips
- `threshold_long_short.xs` — indicator vs four thresholds with a flat band; `Position` replaces the Python stateful `pos` loop
- `breakout_nbar.xs` — N-bar high/low breakout using the `[1]`-shifted channel
- `rsi_mean_reversion.xs` — RSI turns up from oversold → long, exit at a mid level
- `stop_take_profit_block.xs` — fixed % stop-loss + take-profit block on `FilledAvgPrice`; copy the risk block into any template, keep it first
- `time_filter_intraday.xs` — intraday session window + forced flat before close (↔ `examples/txf_ma_1m/`, settlement mask and vol scaling dropped)
- `trailing_stop.xs` — % trailing stop from the peak since entry, `intrabarpersist` running high
