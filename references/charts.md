# Charts & Image Sending Reference

## Blave Chart Style — always apply first

Never plot with matplotlib defaults (default blue lines, boxed axes, gray styles). Every chart you draw must start with:

```python
from lib import chart_style
chart_style.apply()   # once, before creating any figure
```

This sets the Blave look: white background, light gridlines, no top/right spines, brand color cycle, 150 dpi.

Color rules:

- **Single series** → first cycle color (brand orange `#ff9960`) — just plot, the cycle handles it.
- **Multiple categories** (symbols, exchanges, params) → let the color cycle assign `chart_style.DATA_COLORS` in order.
- **Direction only** (up/long/win vs down/short/loss, PnL fills, drawdown) → `chart_style.GREEN` / `chart_style.RED`. Never use green/red for anything that is not a direction, and never use other colors for direction.
- **Direction fills**: line in `GREEN`/`RED` + same-color fill at `alpha=0.2` (drawdown, PnL area). In-position background spans → `GREEN` at `alpha=0.08`.
- **Text annotations in direction colors** (e.g. `+12%` labels) → `chart_style.GREEN_TEXT` / `chart_style.RED_TEXT`. `GREEN`/`RED` are for lines and fills only — too light for text on white.
- Other text: titles/labels use `chart_style.TEXT`; do not pick your own grays or hex colors.

Layout rules:

- One message, one chart. Prefer 1–3 stacked panels sharing the x-axis over many tiny subplots.
- No chartjunk: no shadows, no heavy boxes, no background color bands unless they encode data (e.g. in-position spans → `GREEN` at `alpha=0.08`).
- Title = what the chart shows (symbol, timeframe, metric); put secondary detail (date range, candle count) in a smaller subtitle or axis label, not the title.

## matplotlib — English Only

All chart text must be in English. Chinese characters render as garbled boxes (□□□) on the server — the default font has no CJK glyphs.

```python
# ✓ correct
plt.title("Cumulative Return")
plt.xlabel("Date")
plt.ylabel("Return (%)")
plt.legend(["Strategy", "Benchmark"])

# ✗ wrong — will show □□□
plt.title("累積報酬")
```

## tight_layout Spacing

`tight_layout()` does not accept `hspace`/`wspace` on this matplotlib version. Use `subplots_adjust` first, then `tight_layout` with no spacing args:

```python
# ✓ correct
plt.subplots_adjust(hspace=0.4)
plt.tight_layout()

# ✗ wrong — TypeError
plt.tight_layout(hspace=0.4)
```

## Sending Images to Telegram

```python
from lib.notify import send_photo, send_text

plt.savefig("/tmp/chart.png", dpi=150, bbox_inches="tight")
plt.close()
send_photo("/tmp/chart.png")
send_text("Backtest complete — Sharpe 1.42, MDD -12%")
```

`lib/notify.py` resolves delivery automatically — token from `openclaw.json`, paired chat IDs from `telegram-default-allowFrom.json` (broadcasts to all). Never add a `chatId` key to `openclaw.json`; it is invalid and crashes the gateway on restart.

## Charts the lib sends for you

Two lib-generated charts are sent to Telegram automatically — do NOT add a manual `send_photo` for these:

- **Backtest `pnl.png`** — `run()` sends it when you pass `send_telegram_fn` (see `lib/runner.py`).
- **Scan `heatmap.png`** — `plot_heatmap()` sends it by default (`send_telegram=True`); pass `send_telegram=False` to suppress.

Any *other* chart you generate yourself (custom analysis, regime plots, ad-hoc figures) is NOT auto-sent — call `send_photo(path)` explicitly.
