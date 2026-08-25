# examples/exports/pine/

Pine Script v6 `strategy()` templates for the TradingView export flow (`references/tradingview-pine.md`). Copy the closest one, change inputs and the `// --- signal ---` block, keep the rest. Every file: `//@version=6` on line 1, `strategy()` on the next statement, sections `inputs / signal / orders / plots`, next-bar-open fills and 0.05 % commission to mirror Blave defaults. None of these were compiled here; `// UNVERIFIED:` marks the one behaviour not confirmed against the reference.

- `sma_cross_long.pine` — long-only SMA state (fast > slow long, fast < slow flat); mirrors `examples/btc_sma_cross`
- `sma_cross_long_short.pine` — SMA long/short flip via `strategy.entry` reversal; four-threshold flat-band variant in comments
- `breakout_nbar.pine` — long above prior N-bar high, flat below prior M-bar low (`ta.highest(high, n)[1]`)
- `rsi_mean_reversion.pine` — two-threshold oscillator entry/exit with hold zone (`examples/btc_ti_5min` shape), own pane
- `stop_loss_take_profit.pine` — signal entry + fixed-% bracket via `strategy.exit(stop =, limit =)` absolute prices
- `session_filter.pine` — entries only inside `input.session`, flatten at the session's last bar close (`immediately = true`); TW futures shape
- `trailing_stop.pine` — signal entry + `strategy.exit(trail_points =, trail_offset =)` with % converted to ticks at entry
