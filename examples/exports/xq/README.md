# examples/exports/xq/

XS (XQ 全球贏家 自動交易腳本) templates for the "export strategy code" flow in `references/xq-xs.md`. Adapt one of these — never write XS from scratch. All use `SetPosition` / `Position` / `Filled` (never EasyLanguage `Buy`/`Sell` statements) and put exits before entries, because XS executes only the first trading instruction per pass. The daily templates read every signal on the completed bar (`[1]`), because XQ's daily backtest runs intrabar. All eight templates compile in XQ 全球贏家's XS editor with 0 errors / 0 warnings. Backtested on 2330 daily against Blave (round trips): `sma_cross_long` 18/18, `sma_cross_long_short` 28/28, `threshold_long_short` 84/84, `rsi_mean_reversion` 7/7, `trailing_stop` 24/24, `stop_take_profit_block` 55/59 (the 4 diffs are price-basis edge cases: 3 stop/TP threshold edges, 1 MA-cross date shifted a day). `breakout_nbar` is behaviour-checked only (no High/Low in Blave's adjusted data to replicate it); `time_filter_intraday` (Min) is untested. An export the agent adapts from them is still not compiled by the agent.

- `sma_cross_long.xs` — SMA golden/death cross, long-only (↔ `examples/tsmc_ma/`, `examples/btc_sma_cross/`)
- `sma_cross_long_short.xs` — SMA cross, always in the market, one-instruction flips
- `threshold_long_short.xs` — indicator vs four thresholds with a flat band; `Position` replaces the Python stateful `pos` loop
- `breakout_nbar.xs` — N-bar high/low breakout: completed `Close[1]` vs the channel `Value1[2]` / `Value2[2]`
- `rsi_mean_reversion.xs` — RSI turns up from oversold → long, exit at a mid level
- `stop_take_profit_block.xs` — fixed % stop-loss + take-profit block on `FilledAvgPrice`; copy the risk block into any template, keep it first
- `time_filter_intraday.xs` — intraday session window + forced flat before close (↔ `examples/txf_ma_1m/`, settlement mask and vol scaling dropped)
- `trailing_stop.xs` — % trailing stop from the peak since entry, `intrabarpersist` running high
