# Manager & Reconciler

## Files

- `manager/management_backtest.py` — portfolio walk-forward backtest
- `manager/manager.py` — portfolio optimizer
- `manager/reconciler.py` — position reconciler (polling loop)
- `manager/portfolio_config.json` — gitignored; written by manager.py; also contains `"exchanges"` dict (see below)

**CRITICAL — never create files or subdirectories inside `manager/`.** All output (portfolio_config.json, pnl.png, stats.json) is written by the scripts themselves. Never create a `manager/manager/` or any nested folder — it breaks path resolution in all three scripts. Never delete any file in `manager/` when removing strategies.

## management_backtest.py

Simulates the manager's dynamic allocation day by day (strictly out-of-sample). Compares against random static portfolios as benchmark. Run BEFORE going live to validate combined portfolio performance.

```
python3 manager/management_backtest.py [--lookback 365] [--random-n 500]
```

When the user asks to backtest the portfolio / combined strategies, use THIS script — not individual strategy backtests.

## manager.py

Reads all `strategies/*/stats.json` (daily_returns). Maximizes `slope/std` of the combined portfolio equity curve (365-day lookback). Writes optimal weights + leverage to `manager/portfolio_config.json` — **only with `--apply`**.

```
python3 manager/manager.py [--lookback 365] [--target-vol 0.30]            # dry-run
python3 manager/manager.py [--lookback 365] [--target-vol 0.30] --apply    # write config
```

`--target-vol`: sets target annual volatility; computes `leverage = target_vol / ann_vol`

`--allocator <name>`: weight with `allocators/<name>/allocator.py` instead of the built-in slope/std optimiser. `management_backtest.py` takes the same flag and writes its output to `allocators/<name>/` so each method keeps its own `stats.json` + `pnl.png`. Contract, validation rules, and the create → backtest → dry-run → apply workflow: **`references/allocator-code.md`**. The `--apply` confirmation rule below applies identically to allocator runs.

**manager.py never touches `account_value`.** It only writes `weights` and `leverage`. `account_value` is the live position-sizing base (`contribution = account_value * leverage * weight * position`), so changing capital is a separate, explicit action: edit `portfolio_config.json["account_value"]` by hand. There is intentionally no `--account` flag — updating weights must not be able to resize live positions.

**`--apply` flag — protects live trading.** Default is dry-run: weights are computed and printed but `portfolio_config.json` is untouched, so a research run can never silently change the weights the live reconciler is trading on. The optimiser is seeded (`np.random.seed(42)`), so the `--apply` re-run produces exactly the weights shown in the dry-run (same stats.json inputs).

Required workflow:
1. Run `python3 manager/manager.py` (no `--apply`) and show the user the proposed weights.
2. Ask for explicit confirmation that these weights should go live.
3. Only after the user confirms, re-run the SAME command with `--apply` appended.
4. Never pass `--apply` on the first run, and never assume confirmation from context.

## portfolio_config.json — Exchange Routing

`manager.py` writes `weights` and `leverage`. You must manually add (or update) the `"exchanges"` dict whenever a strategy is deployed or moved to a different exchange:

```json
{
  "account_value": 10000,
  "leverage": 1.2,
  "weights":   { "btc_ti_long": 0.5, "btc_ti_short": 0.5 },
  "exchanges": { "btc_ti_long": "okx", "btc_ti_short": "okx" }
}
```

- Exchange routing is **not** in the strategy file — strategy files have no `EXCHANGE` field.
- Strategies missing from `"exchanges"` are silently skipped by the reconciler.
- The same strategy can be pointed at a different exchange by changing only this file.
- Valid values: any string the `place_order()` implementation in `reconciler.py` recognises (e.g. `"okx"`, `"taifex"`).

## reconciler.py

Polls every 5 seconds; only reconciles when a strategy's `state.json` mtime changes. `get_positions()` and `place_order()` are exchange-specific stubs to fill in once. `lib/portfolio.py` contains `reconcile()` logic; applies `leverage` from portfolio_config.

**Wiring exchange order libraries (required, not optional):** `get_positions()` and `place_order()` must call into a `lib/order_*.py` helper — never remain as `raise NotImplementedError`. When writing a new order library, immediately update `reconciler.py` to import and call it in the same session.

**Qty precision (most common cause of rejected orders):** before placing any order, the order library must know the symbol's qty step, min qty, and min notional — fetch them from the exchange and cache at startup:

| Exchange | Endpoint | Fields |
|---|---|---|
| Binance futures | `GET /fapi/v1/exchangeInfo` | `LOT_SIZE.stepSize`, `LOT_SIZE.minQty`, `MIN_NOTIONAL` |
| Binance spot | `GET /api/v3/exchangeInfo` | same filter names |
| OKX | `GET /api/v5/public/instruments?instType=SWAP` | `lotSz`, `minSz`, `ctVal` |
| Bybit | `GET /v5/market/instruments-info?category=linear` | `lotSizeFilter.qtyStep`, `lotSizeFilter.minOrderQty` |

Floor the qty to the step with `Decimal` — never float arithmetic:

```python
from decimal import Decimal, ROUND_DOWN
qty = Decimal(str(raw_qty)).quantize(Decimal(step_str), rounding=ROUND_DOWN)
params['quantity'] = format(qty, 'f')   # plain string — never 1e-05 notation
```

After flooring: if qty < min qty or `qty * price` < min notional → `return False` (skip, no phantom-trade notification). Never guess precision from memory — read it from the exchange API or the relevant `skills/blave-quant/references/` file.

```
bash manager/start_reconciler.sh
```

Run via `start_reconciler.sh` (Linux) / `start_reconciler_windows.ps1` (Windows) — never `reconciler.py` directly — the wrapper restarts on crash and sends a Telegram alert on each exit. Determine the OS first per `AGENTS.md`.

**Before starting the reconciler (or triggering a manual reconcile):** always show the user the pending order summary from `aggregate_portfolio()` + `compute_diff()` and ask for explicit confirmation. Only proceed if the user confirms.

---

## Additional Rules

**Before running `manager/manager.py` for weight optimisation:** temporarily set `END = None` in every strategy file so the optimiser uses the latest data. After the run, restore each strategy's fixed past date (roughly one week ago) for normal cache-backed backtests.

**Deleting a strategy:** delete only its own directory (e.g. `strategies/btc_kd_long/`). Never touch `manager/`.

**Changing `account_value` (capital):** edit `portfolio_config.json["account_value"]` by hand — the ONLY way, and only when the user explicitly asks to change capital (never as a side effect of a weight update). Procedure: (1) the value is total account equity in the account currency (USD) — use the real figure, never a placeholder like 10000; (2) editing resizes every live position, so show the user the old → new value and get explicit confirmation BEFORE writing, same as `--apply`; (3) no restart needed — the reconciler re-reads the file on its next poll. `manager.py` never writes this field.

**OKX `get_positions()` pitfall:** OKX positions API returns `ctVal` as `None` for some instrument types. Do NOT compute notional as `pos * markPx * ctVal` — use the `notionalUsd` field directly instead. Zero notional causes the position to be ignored and reconciliation skipped.

**Account library — create `lib/account_{exchange}.py`:** To wire snapshot for an exchange, copy `lib/account_TEMPLATE.py` to `lib/account_{exchange}.py` and implement `get_equity(env)` and `get_positions(env)`. `snapshot.py` auto-discovers this file by name — **do NOT modify snapshot.py**. API keys go in `.env` (e.g. `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE` — match the casing already used for that exchange's keys elsewhere in `.env`). Before writing, read the relevant skill reference under `skills/blave-quant/references/` for the correct balance and position endpoints.

**BingX is already wired — `lib/account_bingx.py` ships implemented, no template copy needed.** Covers the SWAP (perp/futures) account only, via `/openApi/swap/v3/user/balance` and `/openApi/swap/v2/user/positions`. BingX keeps fund/spot/swap as three separate accounts with no auto-transfer (see `skills/blave-quant/references/bingx-api-reference.md`) — if the user's capital is in the spot or fund account, `get_equity()` will under-report; extend it rather than writing a second file.

**`portfolio_config.json["messages"]`** — Telegram message templates for reconciler and watchdog. Keys: `order_buy`, `order_sell`, `order_close_long`, `order_close_short`, `order_error`, `watchdog_started`, `watchdog_restart`. Placeholders: `{symbol}`, `{amount}`, `{error}`, `{code}`. Edit these to match the user's preferred language when deploying.

**`manager/snapshot.py`** — daily account equity snapshot. Reads unique exchanges from `portfolio_config.json["exchanges"]`, auto-imports `lib/account_{exchange}.py` per exchange, records to `manager/snapshots.jsonl`, sends Telegram report. Scheduled daily at 08:00 UTC — see `references/deployment.md` for the exact cron (Linux) / schtasks (Windows) entry. The working-directory prefix (`cd &&` / `cd /d &&`) is mandatory on both OSes — the scheduler does not run from the workspace by default and all paths in this repo are relative; without it every file open fails silently before Telegram is reached.

**Always start the reconciler via the watchdog wrapper**, not `reconciler.py` directly and never with `nohup &`. `nohup &` background processes are killed when the shell session ends.

**Linux — tmux session:**
```
tmux new-session -d -s reconciler 'cd $BLAVECLAW_HOME/workspace && bash manager/start_reconciler.sh'
```
(resolve `$BLAVECLAW_HOME` first — same env var as `references/deployment.md`'s cron entries; defaults to `/root/.openclaw` if unset)
To check status: `tmux attach -t reconciler`. To stop: `tmux kill-session -t reconciler`.

**Windows — NSSM service** (also survives instance reboot, unlike the Linux tmux session — a deliberate improvement, not a gap):
```
nssm install blaveclaw-reconciler powershell.exe "-ExecutionPolicy Bypass -File %BLAVECLAW_HOME%\workspace\manager\start_reconciler_windows.ps1"
nssm set blaveclaw-reconciler AppDirectory %BLAVECLAW_HOME%\workspace
nssm set blaveclaw-reconciler Start SERVICE_AUTO_START
nssm start blaveclaw-reconciler
```
(`%BLAVECLAW_HOME%` — resolve the actual env var on this machine before running these commands, don't type the literal placeholder; defaults to `C:\openclaw` if unset)
To check status: `nssm status blaveclaw-reconciler`. To stop: `nssm stop blaveclaw-reconciler` (add `nssm set blaveclaw-reconciler Start SERVICE_DEMAND_START` if it should not restart on next boot).

**Capital (群益) broker exception:** NSSM services default to running as `LocalSystem`. Capital's
`SKCOM.dll` binds the certificate to the Windows identity that issued it (always `Administrator`
on Blave Agent machines — see `references/capital-broker.md` Step 2), so a service running as
`LocalSystem` fails `SKCenterLib_Login` with error 602 even though the cert is correctly installed.
If any portfolio in `portfolio_config.json["exchanges"]` uses `"capital"`, set the service identity
to Administrator before starting it:
```
nssm set blaveclaw-reconciler ObjectName .\Administrator "<Administrator password>"
```
Read the password from `C:\openclaw\credentials\rdp_password.txt` on the machine itself (agent has
local read access — no need to ask the user, they'd only be repeating what's already on their own
「遠端桌面連線資訊」dashboard card). No certificate export/import needed — this replaces the old
POC guidance about moving the cert to a different account store.

**After starting the reconciler, register it for health monitoring** — add to `state/deployments.json` (create the file if missing):
```json
{"reconciler": {"type": "daemon", "expect_every_minutes": 5,
                "registered_at": "<UTC now, %Y-%m-%dT%H:%M:%S>"}}
```
The reconciler touches `state/heartbeat/reconciler` on every poll loop; `manager/healthcheck.py` alerts the user if the heartbeat goes stale (see `references/deployment.md` › Deployment Healthcheck). Without this registration the healthcheck cannot see the daemon.

**Trace the full calculation chain before flagging an inconsistency.** If `state.json` shows a non-zero position but a field in `portfolio_config.json` (e.g. `weight=0`) seems contradictory, read `lib/portfolio.py` first. `contribution = account_value * leverage * weight * position` — a zero weight zeroes out the contribution by design. Do not report a bug until you have followed every variable through the aggregation logic.
