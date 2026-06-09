# Strategy Deployment

## Confirmation Required
CRITICAL: You MUST NEVER deploy a live strategy or set up a cron job without explicit user confirmation.

## Type A (Signal Strategy) — mandatory flow:
1. Write the strategy with `MODE = "backtest"` and run a backtest — show the results
2. Ask the user to confirm deployment: "Do you want to deploy this live? Reply YES to confirm."
3. After YES, ask the following **in a single message** before writing any code:
   - **Spot or futures/perpetual?** This determines which order API and position sizing logic to use.
   - **Align positions?** Do you want to reconcile current open positions before the first live run? If YES, fetch current positions from the exchange and align them with what the strategy's initial state expects — place orders for any difference. If NO, the strategy will align naturally over the next few signals.
4. After all three are answered, confirm portfolio_config.json settings with the user:
   - **`account_value`**: total USDT capital allocated to the portfolio
   - **`target_vol_pct`**: target annual volatility % (default 30%). If the user prefers to think in terms of acceptable MDD, use the approximation `target_vol ≈ MDD / 2` (e.g. willing to lose 20% → target_vol ≈ 10%). Show both the vol and the implied MDD so the user can decide.
   - Show current values from portfolio_config.json if it exists, and ask the user to confirm or update them before proceeding.
5. Only after all confirmations: change `MODE = "live"`, update portfolio_config.json, and set up the cron jobs:
   a. Add the strategy cron (see Cron Job Format below)
   b. **Add the daily snapshot cron if not already present** — check with `crontab -l | grep snapshot` first:
      ```
      0 8 * * * cd /root/.openclaw/workspace && python3 manager/snapshot.py
      ```
   c. **Run snapshot immediately** to verify Telegram delivery: `python3 manager/snapshot.py`
      If the snapshot message does not arrive on Telegram, debug before considering deployment complete.

Never assume the user wants to go live just because they described a strategy or said "let's try it."
Even if the user says "deploy it" or "run it", always confirm with one message before touching cron or MODE = "live".
Once deployed live, send a confirmation message with: strategy name, cron schedule, daily snapshot time (08:00 UTC), account_value, and target_vol_pct.

## Cron Job Format
**The `cd` is mandatory in every cron entry.** Cron runs from `/root` by default. All scripts in this repo use relative paths (`manager/`, `strategies/`, `lib/`, `cache/`). Without `cd`, every relative path resolves from `/root` → `FileNotFoundError` → the script crashes silently before sending any Telegram notification.

**Two separate cron entries are required for every deployment:**

1. Strategy execution cron:
```
5 * * * * cd /root/.openclaw/workspace && python3 strategies/<name>/strategy.py
```

2. Daily snapshot cron (add once; survives across strategy additions):
```
0 8 * * * cd /root/.openclaw/workspace && python3 manager/snapshot.py
```

Never write `python3 strategies/<name>/strategy.py` alone — the `cd &&` prefix is not optional.

## Type B (Everything else) — mandatory flow:
1. Skip backtest entirely
2. Ask the user to confirm before deploying: "Do you want to deploy this live? Reply YES to confirm."
3. After YES, ask **Spot or futures/perpetual?** and **Align positions?** (same as Type A step 3) before writing any code.
4. Only after all confirmations: set up the cron job, add the daily snapshot cron if not already present, and run `python3 manager/snapshot.py` immediately to verify Telegram delivery

## Live vs Backtest
Live trading uses the SAME script as backtest — only `MODE` changes. Keep `START` the same long date range as backtest so the website report shows full history. Always keep `END = None` for live — setting it to a specific date will cap data fetch at that date and break live operation.

## State Initialisation (First Live Run)
On the first live cron tick there is no `state.json` yet. The runner initialises state from the last signal: `signals.ffill().fillna(0).iloc[-1]`. This correctly reflects the current intended position without replaying history.
