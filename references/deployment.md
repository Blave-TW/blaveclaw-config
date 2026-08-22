# Strategy Deployment

## Confirmation Required
CRITICAL: You MUST NEVER deploy a live strategy or set up a cron job without explicit user confirmation.

## No LLM in the Execution Loop

Strategy execution MUST be scheduled as a system cron job (Linux) or Scheduled Task (Windows) that runs Python directly through `manager/wait_for_bar.py` (Type A/C) or `manager/run_strategy.sh`/direct `strategy.py` (Type B — see its own section below) — NEVER as an OpenClaw agent cron that wakes the agent to "run the strategy and report the result".

- Every agent wake-up burns the user's LLM credit (a full session trajectory per tick). A `*/5` agent cron costs orders of magnitude more than the identical system cron, for zero added value — the strategy script is deterministic Python and needs no reasoning to run.
- Failure notification is already handled deterministically: `run_strategy.sh` (Type B) and `wait_for_bar.py` (Type A/C) both catch crashes and send the Telegram alert via `manager/alert_failure.py`. An agent cron adds no reliability.
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
   - **`amounts`**: per-strategy dollar allocation — "what this strategy trades with at position=1", in account currency (contracts for Capital Taiwan futures). `amounts` is canonical (see `lib/portfolio.py` `strategy_amounts`); ask the user for this strategy's amount directly. Legacy configs without `amounts` fall back to `account_value × leverage × weight` — do not create new deployments on the legacy fields.
   - Show current values from portfolio_config.json if it exists, and ask the user to confirm or update them before proceeding.
5. Only after all confirmations: change `MODE = "live"`, update portfolio_config.json, and set up the schedule (cron on Linux, Scheduled Tasks on Windows — see OS check in `AGENTS.md`):
   a. Add the strategy schedule entry (see Cron Job Format / Scheduled Task Format below)
   b. **Add the healthcheck schedule if not already present** and register the deployment — see Deployment Healthcheck below

Never assume the user wants to go live just because they described a strategy or said "let's try it."
Even if the user says "deploy it" or "run it", always confirm with one message before touching the schedule or MODE = "live".
Once deployed live, send a confirmation message with: strategy name, schedule, amount, and one line noting the healthcheck will alert them if the strategy stops running.

## Editing a FUNDED Strategy (in the 下單組合) — identity vs tunables

A funded strategy's positions track its target live, so what you may change
in place splits cleanly in two (the industry split too — 3Commas locks the
pair while deals run; Pionex locks grids entirely):

**Tunables — edit freely, keep the name.** Thresholds, vol targets, windows,
logic: change `strategy.py` in place; the next signal run picks it up and the
reconciler adjusts the position to the new target. No confirmation needed —
this live-follow is the product working as designed.

**Identity — NAME / SYMBOL / MARKET — never change in place on a funded
strategy.** Changing any of them means "this is a different strategy": the
old target vanishes (or moves to another instrument/inventory), so the
reconciler WILL close the old position — correct behaviour, but it must never
be a surprise. Measured 2026-08-05: an agent renamed a funded strategy to
flip MARKET to spot; the futures position auto-closed, the web's picker
showed a ghost entry, and the user read all of it as breakage. The flow for
an identity change is REMOVE + REBUILD:
1. Tell the user up front: the old strategy's futures position will be
   auto-closed / spot inventory auto-sold, and their 下單設定 needs
   re-saving. Get their OK.
2. Build the new strategy under its own name (backtest as usual).
3. Have them re-save 下單設定 (uncheck old name if it lingers, fund the new).

Also: `strategies/<name>/strategy.py` is the only layout the schedule can
run — never `strategies/<name>.py` at the top level (healthcheck flags it).

## Deployment Healthcheck

`manager/healthcheck.py` alerts the user when something that should be running has gone quiet — a lost cron entry, a dead daemon, or a deployment that was registered but never scheduled. It complements `alert_failure.py` (which only fires when a run happened and crashed). Heartbeats are written automatically: `run_strategy.sh` (Type B) and `wait_for_bar.py`'s own launcher (Type A/C — same contract, reimplemented in Python so it works without bash on Windows) both touch `state/heartbeat/<name>` on every successful run, and repo daemons (reconciler) touch their own each loop.

At deployment time:

1. **Add the healthcheck schedule once.** Check first with `crontab -l | grep healthcheck` (Linux) or `schtasks /query /tn blaveclaw-healthcheck` (Windows):
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
Strategies scheduled through `wait_for_bar.py` (or the older direct `run_strategy.sh`) are also auto-registered by the healthcheck from crontab, so registration is a safety net, not a hard dependency — but daemons (e.g. reconciler, `{"type": "daemon", "expect_every_minutes": 5}`) MUST be registered manually or the healthcheck cannot see them.

Alert behavior: at most one alert per deployment per 6 hours; a one-line recovery message when the heartbeat returns. Weekday-only schedules (day-of-week restricted cron) use a conservative 3-day threshold so weekends never false-alarm.

## Long-running processes — memory discipline

RAM on the machine is limited and shared with the agent runtime itself (check with `free -m`); a process that grows without bound freezes the ENTIRE machine — the bot dies with it and the user is locked out (it has happened: an in-memory trade log grew to 1.9 GB and froze a machine for 24 hours). For any process that runs continuously (live monitors, scanners, paper-trading engines):

- Every in-memory list/dict that grows per tick, per signal, or per trade MUST be bounded — `deque(maxlen=N)` or trim to the last N entries. No exceptions.
- Records that must be kept forever go to disk (append to a `.jsonl` file), NOT into a Python list.
- NEVER attach large snapshots (full feature caches, candle histories, whole DataFrames) to per-trade/per-signal records. Store IDs or the few fields you need.
- Keep only the candles a computation needs (e.g. last 50 bars), not the full history.
- After starting a long-running process, check its memory once (`ps -o rss= -p <pid>`) and tell the user roughly how much it uses; if it grows run over run, treat that as a bug and fix it before leaving it running.
- Every daemon must heartbeat: touch `state/heartbeat/<name>` at the top of each loop iteration, and register the daemon in `state/deployments.json` so `manager/healthcheck.py` alerts the user when it dies (see *Deployment Healthcheck* above). A daemon nobody watches WILL die silently and the user finds out weeks later.

## Cron Job Format (Linux)
**The `cd` is mandatory in every cron entry.** Cron runs from `/root` by default. All scripts in this repo use relative paths (`manager/`, `strategies/`, `lib/`, `cache/`). Without `cd`, every relative path resolves from `/root` → `FileNotFoundError` → the script crashes silently before sending any Telegram notification.

**Resolve `$BLAVECLAW_HOME` before writing any cron entry** — workspace root is `$BLAVECLAW_HOME/workspace`, not a fixed path, and the correct value depends on which RUNTIME this machine is (same distinction `AGENTS.md` already has you determine once per session for OS): old BlaveClaw machines default to `/root/.openclaw`; the newer Blave Agent runtime (layout signal: `/opt/blave-agent/openclaw.json` exists — check the file, not just the directory, or a half-provisioned machine reads as this runtime and fails a different way) defaults to `/opt/blave-agent` instead — `lib/notify.py` resolves this same way, so a cron entry that gets it wrong doesn't error, it just silently drops every Telegram alert (measured live on a Blave Agent machine 2026-08-19: `send_text()` degraded to a no-op log line with `/root/.openclaw`, no error surfaced anywhere). If `BLAVECLAW_HOME` is already set in your shell environment, trust that over guessing from the layout. Every cron entry below writes `$BLAVECLAW_HOME` literally into the line — set it once at the top of the crontab so all entries (present and future) expand it the same way:
```
BLAVECLAW_HOME=/opt/blave-agent    # or /root/.openclaw — whichever this machine's layout resolved to, see above
*/30 * * * * cd $BLAVECLAW_HOME/workspace && python3 manager/healthcheck.py
* * * * * cd $BLAVECLAW_HOME/workspace && python3 manager/wait_for_bar.py <name>
```
(check `crontab -l` first — if a `BLAVECLAW_HOME=` line already exists, don't add a second one and don't assume it's wrong; only replace it if you've confirmed the existing value doesn't match this machine's actual layout.)

**Type A/C: always go through `manager/wait_for_bar.py`, never call `strategy.py` or `run_strategy.sh` straight off the cron.** A fixed "N minutes after the boundary" offset either wastes time on days the data lands fast or isn't enough on days it's slow (and users notice — "why does it always wait 5 minutes"). `wait_for_bar.py` polls every minute, checks whether the bar the strategy actually needs (via the strategy's own `fetch_data`) has landed yet, and only then runs it; once that bar is processed it exits immediately with no fetch and no log until the next bar boundary — see the docstring in `manager/wait_for_bar.py` for the exact freshness check (Type C waits for every symbol in the universe, not just the first to update). If the bar still hasn't landed after 15 minutes it sends one Telegram alert (not a repeat per minute) and keeps polling — this is a different signal from a crashed run and from the healthcheck (schedule went quiet entirely); a data source that's actually down can trip more than one of the three, and that overlap is expected, not a bug.

**`wait_for_bar.py` does NOT shell out to `manager/run_strategy.sh`** — that script is bash-only, and Capital(群益)/Taiwan-broker strategies run on Windows, which has no bash. Instead `wait_for_bar.py` launches `strategy.py` directly with `sys.executable` (whatever interpreter cron is already using) and reimplements the same crash-safety contract in pure Python: capture output, log + Telegram-alert via `manager/alert_failure.alert()` on a non-zero exit or a hung run (killed after a timeout), touch the heartbeat on success. This is why it's safe to schedule identically on Linux and Windows — see both cron forms below.

Strategy execution cron (Type A/C):
```
* * * * * cd $BLAVECLAW_HOME/workspace && python3 manager/wait_for_bar.py <name>
```

Never write `python3 strategies/<name>/strategy.py` or `bash manager/run_strategy.sh <name>` directly in a cron entry for a Type A/C strategy — always go through `manager/wait_for_bar.py <name>`, and the `cd &&` prefix is still not optional.

## Scheduled Task Format (Windows)
Same entry, via `schtasks`. The `cd /d` is mandatory for the same reason as Linux's `cd &&` — all scripts use relative paths. Resolve `%BLAVECLAW_HOME%` the same way as Linux (defaults to `C:\openclaw` if unset) rather than assuming a fixed path.

Strategy execution task, Type A/C (every minute — `wait_for_bar.py` itself decides when the real run fires; note this calls `wait_for_bar.py`, not `strategy.py` directly — the old direct-`strategy.py` form had no crash protection on Windows at all, since `run_strategy.sh` never ran there either):
```
schtasks /create /tn "blaveclaw-strategy-<name>" /tr "cmd /c cd /d %BLAVECLAW_HOME%\workspace && python manager\wait_for_bar.py <name>" /sc minute /mo 1 /ru SYSTEM /f
```

## Type B (Everything else) — mandatory flow:
Type B strategies (screener, grid, arbitrage, one-off execution, alert bot) have no `INTERVAL`/`fetch_data` contract to poll a bar against, so they do NOT go through `wait_for_bar.py` — they keep a plain fixed-cadence schedule, same as before this mechanism existed.
1. Skip backtest entirely
2. Ask the user to confirm before deploying: "Do you want to deploy this live? Reply YES to confirm."
3. After YES, ask **Spot or futures/perpetual?** and **Align positions?** (same as Type A step 3) before writing any code.
4. Only after all confirmations: agree a run cadence with the user (there's no bar to wait for, so this is just "how often"), set up the schedule, and add the healthcheck schedule if not already present:
```
<M> * * * * cd $BLAVECLAW_HOME/workspace && bash manager/run_strategy.sh <name>
```
```
schtasks /create /tn "blaveclaw-strategy-<name>" /tr "cmd /c cd /d %BLAVECLAW_HOME%\workspace && python strategies\<name>\strategy.py" /sc minute /mo <N> /ru SYSTEM /f
```
(Linux still goes through `run_strategy.sh` for the same crash-safety reason as always; Windows Type B has no equivalent wrapper yet — same pre-existing gap this whole mechanism didn't set out to fix — so a Type B crash on Windows is silent. Flag this to the user if they're deploying Type B live on Windows.)

## Live vs Backtest
Live trading uses the SAME script as backtest — only `MODE` changes. Keep `START` the same long date range as backtest so the website report shows full history. Always keep `END = None` for live — setting it to a specific date will cap data fetch at that date and break live operation.

## State Initialisation (First Live Run)
On the first live cron tick there is no `state.json` yet. The runner initialises state from the last signal: `signals.ffill().fillna(0).iloc[-1]`. This correctly reflects the current intended position without replaying history.
