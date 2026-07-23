# Strategy Deployment

## Confirmation Required
CRITICAL: You MUST NEVER deploy a live strategy or set up a cron job without explicit user confirmation.

## No LLM in the Execution Loop

Strategy execution MUST be scheduled as a system cron job (Linux) or Scheduled Task (Windows) that runs Python directly through `manager/run_strategy.sh` — NEVER as an OpenClaw agent cron that wakes the agent to "run the strategy and report the result".

- Every agent wake-up burns the user's LLM credit (a full session trajectory per tick). A `*/5` agent cron costs orders of magnitude more than the identical system cron, for zero added value — the strategy script is deterministic Python and needs no reasoning to run.
- Failure notification is already handled deterministically: `run_strategy.sh` catches crashes and sends the Telegram alert via `manager/alert_failure.py`. An agent cron adds no reliability.
- Agent crons are reserved for work that genuinely needs reasoning: daily/weekly report narration, anomaly triage, reconcile summaries. Frequency: at most a few per day, never per tick.
- If the same strategy has both a system cron AND an agent cron that runs it, that is a bug — remove the agent cron (keep the system one) after telling the user why.
- If the user explicitly asks you to run the strategy on every tick via agent cron, explain the credit cost first and offer the system-cron path; only proceed if they still insist.

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
5. Only after all confirmations: change `MODE = "live"`, update portfolio_config.json, and set up the schedule (cron on Linux, Scheduled Tasks on Windows — see OS check in `AGENTS.md`):
   a. Add the strategy schedule entry (see Cron Job Format / Scheduled Task Format below)
   b. **Add the daily snapshot schedule if not already present** — check with `crontab -l | grep snapshot` (Linux) or `schtasks /query /tn blaveclaw-snapshot` (Windows) first
   c. **Add the healthcheck schedule if not already present** and register the deployment — see Deployment Healthcheck below
   d. **Run snapshot immediately** to verify Telegram delivery: `python3 manager/snapshot.py`
      If the snapshot message does not arrive on Telegram, debug before considering deployment complete.

Never assume the user wants to go live just because they described a strategy or said "let's try it."
Even if the user says "deploy it" or "run it", always confirm with one message before touching the schedule or MODE = "live".
Once deployed live, send a confirmation message with: strategy name, schedule, daily snapshot time (08:00 UTC), account_value, target_vol_pct, and one line noting the healthcheck will alert them if the strategy stops running.

## Deployment Healthcheck

`manager/healthcheck.py` alerts the user when something that should be running has gone quiet — a lost cron entry, a dead daemon, or a deployment that was registered but never scheduled. It complements `alert_failure.py` (which only fires when a run happened and crashed). Heartbeats are written automatically: `run_strategy.sh` touches `state/heartbeat/<name>` on every successful run, and repo daemons (reconciler) touch their own each loop.

At deployment time:

1. **Add the healthcheck schedule once** (same "add once" pattern as the snapshot cron). Check first with `crontab -l | grep healthcheck` (Linux) or `schtasks /query /tn blaveclaw-healthcheck` (Windows):
```
*/30 * * * * cd $BLAVECLAW_HOME/workspace && python3 manager/healthcheck.py
```
```
schtasks /create /tn "blaveclaw-healthcheck" /tr "cmd /c cd /d %BLAVECLAW_HOME%\workspace && python manager\healthcheck.py" /sc minute /mo 30 /ru SYSTEM /f
```
(`$BLAVECLAW_HOME` / `%BLAVECLAW_HOME%` — see the note above "Cron Job Format" below for how to resolve and set this once.)
2. **Register the deployment** in `state/deployments.json` (create the file if missing):
```json
{"<strategy_name>": {"type": "cron", "expect_every_minutes": 60,
                     "registered_at": "<UTC now, %Y-%m-%dT%H:%M:%S>"}}
```
Strategies scheduled through `run_strategy.sh` are also auto-registered by the healthcheck from crontab, so registration is a safety net, not a hard dependency — but daemons (e.g. reconciler, `{"type": "daemon", "expect_every_minutes": 5}`) MUST be registered manually or the healthcheck cannot see them.

Alert behavior: at most one alert per deployment per 6 hours; a one-line recovery message when the heartbeat returns; deployment health also appears in the daily snapshot. Weekday-only schedules (day-of-week restricted cron) use a conservative 3-day threshold so weekends never false-alarm.

## Cron Job Format (Linux)
**The `cd` is mandatory in every cron entry.** Cron runs from `/root` by default. All scripts in this repo use relative paths (`manager/`, `strategies/`, `lib/`, `cache/`). Without `cd`, every relative path resolves from `/root` → `FileNotFoundError` → the script crashes silently before sending any Telegram notification.

**Resolve `$BLAVECLAW_HOME` before writing any cron entry** — workspace root is `$BLAVECLAW_HOME/workspace`, not a fixed path. This is the same env var `lib/notify.py`/`auth_service.py` already use: if `BLAVECLAW_HOME` is set in your shell environment, use it; if not, it defaults to `/root/.openclaw`. Every cron entry below writes `$BLAVECLAW_HOME` literally into the line — set it once at the top of the crontab so all entries (present and future) expand it the same way:
```
BLAVECLAW_HOME=/root/.openclaw
*/30 * * * * cd $BLAVECLAW_HOME/workspace && python3 manager/healthcheck.py
5 * * * * cd $BLAVECLAW_HOME/workspace && bash manager/run_strategy.sh <name>
0 8 * * * cd $BLAVECLAW_HOME/workspace && python3 manager/snapshot.py
```
(check `crontab -l` first — if a `BLAVECLAW_HOME=` line already exists, don't add a second one; if this runtime's own `BLAVECLAW_HOME` differs from `/root/.openclaw`, use that value instead — never assume, resolve it from the actual environment on this machine.)

**Always call `strategy.py` through `manager/run_strategy.sh`, never directly.** A crash inside `strategy.py` — including an import error at the top of the file, before `lib/runner.py` ever runs — otherwise has no path to notify the user: cron just swallows the non-zero exit. `run_strategy.sh` is the only layer that can catch a failure the Python side never got a chance to handle, and it sends the Telegram alert itself (via `manager/alert_failure.py`) rather than relying on the strategy's own code to do it.

**Two separate cron entries are required for every deployment:**

1. Strategy execution cron:
```
5 * * * * cd $BLAVECLAW_HOME/workspace && bash manager/run_strategy.sh <name>
```

2. Daily snapshot cron (add once; survives across strategy additions):
```
0 8 * * * cd $BLAVECLAW_HOME/workspace && python3 manager/snapshot.py
```

Never write `python3 strategies/<name>/strategy.py` directly in a cron entry — always go through `manager/run_strategy.sh <name>`, and the `cd &&` prefix is still not optional.

## Scheduled Task Format (Windows)
Same two entries, via `schtasks`. The `cd /d` is mandatory for the same reason as Linux's `cd &&` — all scripts use relative paths. Resolve `%BLAVECLAW_HOME%` the same way as Linux (defaults to `C:\openclaw` if unset) rather than assuming a fixed path.

1. Strategy execution task (hourly, mirrors `5 * * * *`):
```
schtasks /create /tn "blaveclaw-strategy-<name>" /tr "cmd /c cd /d %BLAVECLAW_HOME%\workspace && python strategies\<name>\strategy.py" /sc hourly /mo 1 /st 00:05 /ru SYSTEM /f
```

2. Daily snapshot task (add once; mirrors `0 8 * * *`):
```
schtasks /create /tn "blaveclaw-snapshot" /tr "cmd /c cd /d %BLAVECLAW_HOME%\workspace && python manager\snapshot.py" /sc daily /st 08:00 /ru SYSTEM /f
```

Check for an existing snapshot task with `schtasks /query /tn "blaveclaw-snapshot"` (non-zero exit if absent) before creating it.

## Type B (Everything else) — mandatory flow:
1. Skip backtest entirely
2. Ask the user to confirm before deploying: "Do you want to deploy this live? Reply YES to confirm."
3. After YES, ask **Spot or futures/perpetual?** and **Align positions?** (same as Type A step 3) before writing any code.
4. Only after all confirmations: set up the schedule (cron or Scheduled Task per above), add the daily snapshot schedule if not already present, and run `python3 manager/snapshot.py` immediately to verify Telegram delivery

## Live vs Backtest
Live trading uses the SAME script as backtest — only `MODE` changes. Keep `START` the same long date range as backtest so the website report shows full history. Always keep `END = None` for live — setting it to a specific date will cap data fetch at that date and break live operation.

## State Initialisation (First Live Run)
On the first live cron tick there is no `state.json` yet. The runner initialises state from the last signal: `signals.ffill().fillna(0).iloc[-1]`. This correctly reflects the current intended position without replaying history.
