# Strategy Deployment

## Confirmation Required
CRITICAL: You MUST NEVER deploy a live strategy or set up a cron job without explicit user confirmation.

The mandatory flow is:
1. Write the strategy with `MODE = "backtest"` and run a backtest — show the results
2. Ask the user TWO questions before going live:
   a. "Do you want to deploy this live? Reply YES to confirm."
   b. "How much capital and position sizing? Options:
      • Fixed amount — buy $X USDT each time
      • Fixed % — use X% of account balance each time
      • Vol-Targeting — size position so portfolio vol = X% annualized (recommended, matches backtest)"
3. Only after receiving YES AND position sizing confirmation: implement `place_order()` with the correct sizing, change `MODE = "live"`, and set up the cron job

Never assume the user wants to go live just because they described a strategy or said "let's try it."
Even if the user says "deploy it" or "run it", always confirm with one message before touching cron or MODE = "live".
Once deployed live, send a confirmation message with the strategy name, cron schedule, and position sizing method.

## Live vs Backtest
Live/paper trading uses the SAME script as backtest — only `MODE` changes. Keep `START` the same long date range as backtest so the website report shows full history. `END` is ignored in live mode (code always fetches to today).

## State Bootstrap (First Live Run)
When switching to live for the first time, the script bootstraps state from history automatically (already in TEMPLATE.py):
- Replays `compute_signal` through all historical candles without placing orders
- Finds the correct current position before the first live cron tick
This ensures `in_position` and `trades_log` reflect reality from day one, even if the entry signal fired weeks ago.
