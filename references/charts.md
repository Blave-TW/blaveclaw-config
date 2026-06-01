# Charts & Image Sending Reference

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

Token and chat_id are read automatically from `/root/.openclaw/openclaw.json`.
