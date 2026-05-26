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
5. Only after all confirmations: change `MODE = "live"`, update portfolio_config.json, and set up the cron job

Never assume the user wants to go live just because they described a strategy or said "let's try it."
Even if the user says "deploy it" or "run it", always confirm with one message before touching cron or MODE = "live".
Once deployed live, send a confirmation message with the strategy name, cron schedule, account_value, and target_vol_pct.

## Cron Job Format
Always use this exact format — the `cd` is mandatory; without it the script runs from an unknown directory and fails silently:
```
5 * * * * cd /root/.openclaw/workspace && python3 strategies/<name>/strategy.py
```
Never write `python3 strategies/<name>/strategy.py` alone without the `cd` prefix.

## Type B (Everything else) — mandatory flow:
1. Skip backtest entirely
2. Ask the user to confirm before deploying: "Do you want to deploy this live? Reply YES to confirm."
3. After YES, ask **Spot or futures/perpetual?** and **Align positions?** (same as Type A step 3) before writing any code.
4. Only after all confirmations: set up the cron job

## Live vs Backtest
Live trading uses the SAME script as backtest — only `MODE` changes. Keep `START` the same long date range as backtest so the website report shows full history. Always keep `END = None` for live — setting it to a specific date will cap data fetch at that date and break live operation.

## State Initialisation (First Live Run)
On the first live cron tick there is no `state.json` yet. The runner initialises state from the last signal: `signals.ffill().fillna(0).iloc[-1]`. This correctly reflects the current intended position without replaying history.
