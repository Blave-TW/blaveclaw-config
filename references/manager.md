# Manager & Reconciler

## Files

- `manager/management_backtest.py` — portfolio walk-forward backtest
- `manager/manager.py` — portfolio optimizer
- `manager/reconciler.py` — position reconciler (polling loop)
- `manager/portfolio_config.json` — gitignored; written by manager.py; also contains `"exchanges"` dict (see below)

**CRITICAL — `manager/` holds platform scripts and their own output.** All output (portfolio_config.json, pnl.png, stats.json) is written by the scripts themselves. Never create a `manager/manager/` or any other nested folder — it breaks path resolution in all three scripts. Never delete any file in `manager/` when removing strategies.

What may be added and edited in here is a closed list:

- **`manager/executors/<name>.py`** — a custom execution style, the only new file this directory takes (`references/lib.md` › *Custom executors*; the loader in `lib/execute.py` reads exactly that path).
- **`manager/reconciler.py`** — hand-wired venue plumbing, and only that: `get_positions()` / `place_order()` for a venue without an official lib (see *reconciler.py* below and `references/lib.md`). Official venues are auto-wired — do not touch it for them.
- **`manager/manager.py` and `manager/management_backtest.py` — never.** A new weighting method, including a variant of the built-in optimiser, is a new `allocators/<name>/allocator.py` (`references/allocator-code.md` › *Never edit the built-in*).

## management_backtest.py

Simulates the manager's dynamic allocation day by day (strictly out-of-sample). Compares against random static portfolios as benchmark. Run BEFORE going live to validate combined portfolio performance.

```
python3 manager/management_backtest.py [--lookback 365] [--random-n 1000]
```

When the user asks to backtest the portfolio / combined strategies, use THIS script — not individual strategy backtests.

## manager.py

Reads all `strategies/*/stats.json` (daily_returns) and computes portfolio weights with the chosen method — by default `equal`, every strategy the same share. Writes weights + leverage to `manager/portfolio_config.json` — **only with `--apply`**.

```
python3 manager/manager.py --members a,b,c --allocator equal            # dry-run
python3 manager/manager.py --members a,b,c --allocator equal --apply    # write config
```

`--target-vol`: sets target annual volatility; computes `leverage = target_vol / ann_vol`, where `ann_vol` is realized over the **trailing 90 days only** (`VOL_WINDOW`) — volatility clusters, so a regime from years back must not dilute what leverage is safe today. Sharpe and the walk-forward still use the full history; the proposal reports the days actually used as `vol_window_days`. A weighted strategy whose backtest has no data inside that window looks risk-free, which understates `ann_vol` and overstates the leverage derived from it — the script prints a `WARNING:` naming the strategy and how many days are missing. **Re-run those backtests before sizing anything on that leverage.** **Omit it and the account's own `target_vol_pct` is used** — passing a value overwrites that setting on `--apply`, so only pass one when the user asked to change it.

**Name the method explicitly on `--apply`.** Omitting `--allocator` resolves to the method the live config was applied with (a config with no `allocator`, or a null one, means `slope` — it predates `equal`); only a portfolio that has never been applied falls to the default. That keeps a bare re-run from silently re-weighting live positions, but the command reads clearer when the method is spelled out.

`--allocator <name>`: the weighting method — a built-in (`equal`, the default when the flag is omitted, or `slope`) or `allocators/<name>/allocator.py`. `management_backtest.py` takes the same flag and writes its output to `allocators/<name>/` so each method keeps its own `stats.json` + `pnl.png`. Contract, validation rules, and the create → backtest → dry-run → apply workflow: **`references/allocator-code.md`**. The `--apply` confirmation rule below applies identically to allocator runs.

**Members with different history.** The members rarely start and stop together, so both scripts build the union of their dates. A day a strategy had **no data at all** — before its first backtest day, or after its last — is charged `ABSENT_FILL_ANNUAL` (−2%/yr) *while the method fits*, and counted as 0 everywhere the numbers are reported. Filling with 0 on both sides is what produced the old failure: the built-in methods maximise a ratio, an absent leg adds neither return nor variance, so its weight cancels out of the objective and the optimiser allocated to strategies with no history at random, redrawing every day. The charge is proportional to how much of the window is missing, so it decays as a young strategy accumulates days — and, being small, it barely dents a leg that has merely gone stale for a few weeks. `stats.json` records `absent_fill_annual_pct` and `absent_days` (days on which some member had no data); the proposal records `absent_fill_annual_pct`. Both scripts also print the missing days per member, so you can see which one is short. Non-trading days are not absent: every strategy is resampled to calendar days with an explicit 0. Check: `python3 manager/check_absent_fill.py`.

**A walk-forward is only as meaningful as the overlap.** If the members coexist for a small share of the union, most of the run is not testing the portfolio at all — the curve before the last member exists is one strategy's. So the random comparison (`managed_beats_pct_sharpe`, the sharpe/return percentiles, and the `band`) is evaluated **only on the overlap**, the days every member has data: outside it the benchmark would be forced to hold members that did not exist, which biases the verdict in whichever direction the fill points. `stats.json` records `member_spans` (each member's first/last backtest day) and `overlap` (`start`/`end`/`eval_days`); the band carries `band_start` so the page can align it under the full managed curve. When no OOS day has every member live, the comparison fields are null and the band is empty — the managed stats still cover the whole walk-forward. Even inside the overlap the beats% is a **reference figure, not a verdict**: measured on real data, re-running it on a different sub-period moves it by tens of points, so never present it as a pass/fail bar.

Flags both scripts share (these are what the workspace's 投資組合 page drives; the agent can use them too):

- `--members a,b,c` — restrict to those strategies (directory names under `strategies/`). Unknown name → exit 2, nothing written.
- `--params-json '{"k": v}'` — override an allocator's `PARAMS` for this run (declared keys only). `slope` declares `lookback` (it fits on that window); `equal` declares nothing — it has no window, and the walk-forward treats every day as out of sample for it. `--target-vol` is not a method knob at all: it scales leverage and is no longer offered on the page.
- `manager.py --json PATH` — also write the dry-run proposal as JSON (weights, sharpe, leverage, `history_days`…). `management_backtest.py --progress PATH` — write `{day, total}` during the walk-forward. `stats.json` additionally carries `members`, `params`, `managed_cum`, `member_spans`, `overlap` (`start`/`end`/`eval_days`), `random_benchmark.band` + `band_start` (per-day p5/p50/p95 cumulative %, overlap only) and `absent_fill_annual_pct` / `absent_days`.
- Exit codes: 2 = bad input (reason is the last stderr line); `management_backtest.py` exits 3 when the union of member history is not longer than `--lookback`.

The page writes `manager/proposal.json`, `manager/mgmt_job.json`, `manager/mgmt_progress.json` — never edit or delete them by hand.

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

Polls every 5 seconds; only reconciles when a strategy's `state.json` mtime changes (plus `state/execution/kick`, touched when an async execution completes). `get_positions()` and `place_order()` are exchange-specific stubs to fill in once. `lib/portfolio.py` contains `reconcile()` logic; applies `leverage` from portfolio_config.

**Execution styles:** on auto-wired official venues, `place_order()` routes through `lib.execute.dispatch_order`, which reads `portfolio_config["execution"]` (per-strategy 市價/TWAP/custom — see `references/lib.md` › *Execution styles*). TWAP/custom run in a background thread; while one is in flight for a symbol, further legs for that symbol return `False` (deferred) and the residual gap re-reconciles after completion. Hand-wired venues (TW brokers) bypass this entirely.

**Wiring exchange order libraries (required, not optional):** `get_positions()` and `place_order()` must call into a `lib/order_*.py` helper — never remain as `raise NotImplementedError`. When writing a new order library, immediately update `reconciler.py` to import and call it in the same session.

**Auto-halt on exchange disconnect:** `reconciler.py` wraps `get_positions()` in `_get_positions_guarded()`, which counts consecutive failures (`DISCONNECT_HALT_AFTER = 3`) and calls `guard.trip_halt()` once that's hit — the reasoning is that a failed `get_positions()` means the exchange link itself is unreachable (bad/revoked key, IP unwhitelisted, outage), not a single order being rejected (which `reconcile()` already handles per-order without halting). This only ever halts, never auto-clears — same as every other halt, only the user resumes it. Applies uniformly to every exchange without needing a shared exception type, since it keys off `get_positions()` failing at all rather than inspecting the error — **one deliberate exemption (2026-08-14):** `CapitalCacheLagError` (capital's Read-Your-Writes guard, see `_capital_check_snapshot_caught_up` — the worker snapshot hasn't caught up to this process's own last capital order yet, a bounded ≤60s self-resolving condition, not a disconnect) is excluded from the counter. Keep this list short — a second exemption is a sign the counter design needs rethinking, not another special case.

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

## self_ledger — diffing against the bot's own book instead of the exchange

**Problem this solves:** on a single-account setup, `get_positions()` reads the
account's REAL position — which on the same account as the user's own manual
trading includes whatever they opened by hand. `reconcile()` then reads that
manual position as "already have it" and trades against it (scales it up or
sells it down toward target). `self_ledger` fixes this not by reading the
account differently, but by not reading it AT ALL for diffing — the bot tracks
what IT has bought/sold from its own order log (`manager/orders.jsonl`) and
diffs target against that running total instead. A position opened outside
this process never enters the ledger, so it can never be touched.

**Turning it on for an account — read every step; the wrong seed mode trades
the user's own money:**
1. **Flatten the BOT's own positions first** (set the strategies' amounts to
   0 from the web 下單設定 and let the reconciler close them, or confirm the
   bot is already flat). The user's own manual positions stay — that is the
   point. Why required: the fresh-start seed below treats everything on the
   account as the USER's; a live bot position at seed time becomes the
   user's, and the bot then re-buys its full target ON TOP of it — doubled
   exposure.
2. `python3 manager/seed_ledger.py` — ONE TIME, default (fresh-start) mode:
   the bot's book starts at ZERO and everything currently on the account is
   the user's. This is the correct mode for the feature's target user (holds
   manual positions, wants the bot to leave them alone).
   `--absorb` (adopt the account's current positions as bot-owned) is ONLY
   for migrating a bot-only account with no manual positions mixed in — on a
   mixed account it adopts the user's manual positions into the bot's book
   and the bot will later trade them away. **When unsure, never --absorb.**
3. Set `portfolio_config.json["self_ledger"] = true`.
4. No reconciler restart needed — it re-reads the config every poll, same as
   every other `portfolio_config.json` field.

**Workspace update ⇒ reconciler restart (REQUIRED, audit #3):** the reconciler
imports `lib/portfolio.py` once at process start — updating the workspace
files does NOT reload a running daemon. The runtime's `can_wait_start`
capability probes the file on DISK, so after a workspace update the web can
offer 「等新訊號才進場」 while the in-memory reconciler still runs the old
code with no gate support: the gate gets written, HALT clears, and the old
loop catches up at market against the user's explicit choice. Whenever
`lib/` or `manager/` files are updated on a machine, restart the reconciler
in the same session (`tmux kill-session -t reconciler` + the start wrapper,
or `restart_reconciler` from the web) before telling the user anything is
enabled.

**Fail-loud guard:** with `self_ledger` on and no baseline in
`manager/ledger_seed.json` (seed_ledger.py never run, or the file corrupt),
`reconcile()` REFUSES to trade — it raises, which surfaces through the
reconciler's normal error path (Telegram + retreat to heartbeat), and no
orders are placed. It does NOT fall back to summing the whole orders.jsonl
history (a plausible-looking but wrong book on any machine with prior
trading). If the user reports this error, run step 2.

**What changes, exactly** (`lib/portfolio.py`): `reconcile()` still calls the
real `get_positions_fn()` (kept for the `manager/last_reconcile.json` snapshot,
and `manager/reconciler.py`'s own disconnect/auto-halt wrapper around it is
untouched) — but when `self_ledger` is on, `compute_diff()` and every
downstream flip/reduce-only decision in the per-order loop are computed
against `lib.portfolio.ledger_positions()` instead. `ledger_positions()` sums
`ledger_seed.json`'s baseline plus every `manager/orders.jsonl` entry logged
after the seed's timestamp — nothing else. `manager/reconciler.py` itself
needs NO changes; the branch lives entirely in `lib/portfolio.py` and is
config-gated per account.

**What this does NOT solve:** the ledger can drift from the real account
(continued manual trading on the same symbol after the seed) — `self_ledger`
only guarantees the bot never treats someone else's position as its own, not
that the ledger and the real account always agree. There is currently no
drift alert; `manager/last_reconcile.json["ledger"]` vs `["actual"]` is
written every round for a future workspace view to compare, but nothing reads
it yet. Margin/liquidation risk checks (not yet built into `reconcile()`)
must always read the real `get_positions()`/`get_equity()` — never the
ledger, which only knows what the bot itself did.

**Ledger-integrity hardening (2026-08-20, audit P1 batch):** the known ways a
fill could silently go missing from the book now fail loud instead:
- async executions (TWAP/chase/custom) write a durable marker under
  `state/execution/inflight/`; a marker found at reconciler STARTUP means a
  previous process died mid-execution — under `self_ledger` that trips HALT
  with a "verify positions before resuming" message (`lib.execute.
  reap_dead_inflight`, wired in `manager/reconciler.py` startup) instead of
  silently re-buying fills the log never received;
- a failed `manager/orders.jsonl` append under `self_ledger` trips HALT (the
  file IS the book there; in account-read mode it stays best-effort);
- `manager/seed_ledger.py` REFUSES to seed while any execution is in flight
  (seeding mid-execution double-counts its fills);
- `manager/flatten.py` waits up to 30s after tripping HALT for in-flight
  executions to drain before closing, and records a visible order error for
  any that outlive the wait;
- a chase execution that CRASHES now records its real fills from the finally
  block (same pattern as custom executors); a TWAP that crashes mid-run can't
  recover its fill total, so under `self_ledger` it trips HALT instead;
- `lib/guard.trip_halt` sets an in-memory flag before its file write, so a
  FULL DISK (the fleet's measured failure mode — it fails the orders.jsonl
  append and the HALT write together) still halts the reconciler process even
  when `state/HALT` can't land; `reap_dead_inflight` exits the process
  outright when the halt can't persist, keeping its markers for the next boot
  to retry (halt/notify first, marker cleanup last).

**Capital (群益) / lot-based rows:** `ledger_positions()` is unit-agnostic —
it sums whatever `signed_diff` values `manager/orders.jsonl` legs carry, which
for a capital-routed strategy are already LOTS (see *`amounts` semantics*
below), so `self_ledger` composes with the hand-wired capital path with no
extra work. Not yet live-tested on a capital account — verify on the first
real capital `self_ledger` deployment.

**Fixed (2026-08-20, audit P0-2):** `reconcile()` used to log the leg's
PRE-rounding `sub_diff`, not what actually filled — on capital this drifted
the ledger by up to half a lot every round, permanently (crypto had the same
gap in principle, but its rounding is far below `threshold` so it was not a
practical issue there). It now prefers the exchange-confirmed `executed_qty`
when `place_order_fn` returns one — lots directly for `futures_contracts`/
capital rows, `executed_qty × fill_price` (base currency → account currency)
otherwise — falling back to `sub_diff` only when `executed_qty` is absent.
Matches the FIX protocol convention (CumQty, not OrderQty, is the field
position-keeping is built on) and what `lib.execute._finish()` already did
for the async TWAP/chase/custom path — the synchronous path was the outlier.
Also matches `web/`'s own preference (`agent/workspace.html`'s 交易歷史
rendering already prefers `Σ legs' executed_qty×fill_price` over `signed_diff`
when available) — this fix makes the field it falls back to more accurate,
it does not change what the frontend computes.

**The flatten (全部平倉) interaction — read this before wiring self_ledger to
anything live.** What `manager/flatten.py` closes depends on `self_ledger`
(matching the 3Commas/Cryptohopper panic semantics the 暫停下單 dialog was
modeled on): with `self_ledger` ON it closes ONLY the bot's own ledger
positions (`lib.portfolio.ledger_positions`) — a manually-opened position
self_ledger was never told about is untouched even by this button, and each
close is capped at what the account actually holds on that side. Spot stays on
the inventory scope either way (`spot_scope` — strategy-targeted symbols only;
personal coins are never sold). If the ledger is unreadable on the panic path,
swap closes are skipped loudly rather than silently widening scope to the
whole account — the failure mode must never close the manual positions the
feature exists to protect. With `self_ledger` OFF (every pre-feature machine)
it closes every open position on the account — under the old alignment logic
the whole account is the bot's world.

Either way `flatten()` logs its closes to `manager/orders.jsonl` the same as
any other order (`_append_reconciler_log`), and without more,
`ledger_positions()` would sum them in as if they were an ordinary bot trade —
driving the ledger to a phantom position (worst on an OFF-mode machine that
later switches ON: closing a 2000 manual long the bot never held reads back as
the bot now being 2000 short). The next `self_ledger` reconcile round would
then try to "correct" that phantom position — right after the user asked to
close everything.

`flatten()` fixes this itself: it tracks every symbol it actually closed
(including sub-minimum dust left behind — still "as flat as it gets") and
calls `lib.portfolio.zero_ledger_symbols(closed_symbols)` once at the end,
resetting exactly those symbols' ledger baseline to flat, timestamped now.
Every other symbol's seed and accumulated history is untouched — this is a
per-symbol operation (`manager/ledger_seed.json` stores a `{'size', 'ts'}` row
per symbol, not one global timestamp), unlike the whole-account
`seed_ledger()`. The call is unconditional (runs even when `self_ledger` is
currently off) — harmless, and correct the moment the account switches it on
later. Same fix pattern as MultiCharts' "Strategy Positions Tab Mismatch"
handling (its own strategy-position-vs-broker-position architecture has the
identical gap after a manual "Flatten Everything") — MultiCharts leaves the
resync as a manual step the trader must remember to run; this is one call
built into `flatten()` itself instead.

## Additional Rules

**Before running `manager/manager.py` for weight optimisation:** strategy files always keep `END = None` (see strategy-code.md › END and WARMUP), so there is nothing to edit — but re-run any member whose backtest is stale first, or the optimiser fits on an outdated tail.

**Deleting a strategy:** delete only its own directory (e.g. `strategies/btc_kd_long/`). Never touch `manager/`.

**Changing `account_value` (capital):** edit `portfolio_config.json["account_value"]` by hand — the ONLY way, and only when the user explicitly asks to change capital (never as a side effect of a weight update). Procedure: (1) the value is total account equity in the account currency (USD) — use the real figure, never a placeholder like 10000; (2) editing resizes every live position, so show the user the old → new value and get explicit confirmation BEFORE writing, same as `--apply`; (3) no restart needed — the reconciler re-reads the file on its next poll. `manager.py` never writes this field.

**Order-qty UNITS pitfall (measured live 2026-08, sCode 51008):** the order libs'
`place_market_order` / `close_position_partial` take **BASE-currency qty** (ETH, SOL…)
and convert to contracts internally. Never pass `format_qty`'s return onward — it is a
CONTRACT count, and re-converting divides by ctVal twice: invisible on SOL (ctVal=1),
10× oversized on ETH (ctVal=0.1). Use `format_qty` as the min-size gate only. Partial
reduces go through `close_position_partial`, not `close_position` (full close, no qty).

**OKX `get_positions()` pitfall:** OKX positions API returns `ctVal` as `None` for some instrument types. Do NOT compute notional as `pos * markPx * ctVal` — use the `notionalUsd` field directly instead. Zero notional causes the position to be ignored and reconciliation skipped.

**Account library — create `lib/account_{exchange}.py`:** To read equity/positions for an exchange, copy `lib/account_TEMPLATE.py` to `lib/account_{exchange}.py` and implement `get_equity(env)` and `get_positions(env)`. Position symbols follow the canonical dashless-uppercase contract (see `references/lib.md` § Exchange account libraries) — this also applies to a hand-wired reconciler `get_positions()`. Platform readers discover the file by its exact name — keep the naming convention. API keys go in `.env` (e.g. `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE` — match the casing already used for that exchange's keys elsewhere in `.env`). Before writing, read the relevant skill reference under `skills/blave-quant/references/` for the correct balance and position endpoints.

**BingX is already wired — `lib/account_bingx.py` ships implemented, no template copy needed.** Covers the SWAP (perp/futures) account only, via `/openApi/swap/v3/user/balance` and `/openApi/swap/v2/user/positions`. BingX keeps fund/spot/swap as three separate accounts with no auto-transfer (see `skills/blave-quant/references/bingx-api-reference.md`) — if the user's capital is in the spot or fund account, `get_equity()` will under-report; extend it rather than writing a second file.

**`portfolio_config.json["messages"]`** — Telegram message templates for reconciler and watchdog. Keys: `order_buy`, `order_sell`, `order_close_long`, `order_close_short`, `order_error`, `watchdog_started`, `watchdog_restart`. Placeholders: `{symbol}`, `{amount}`, `{error}`, `{code}`. Edit these to match the user's preferred language when deploying.

**Always start the reconciler via the watchdog wrapper**, not `reconciler.py` directly and never with `nohup &`. `nohup &` background processes are killed when the shell session ends.

**Linux — check for the systemd unit FIRST, tmux only as fallback:**
```
systemctl is-active blave-agent-reconciler.service
```
- If the command reports `active` (or the unit file exists at all — check with
  `systemctl cat blave-agent-reconciler.service`), the reconciler is supervised by
  systemd. Control it ONLY through systemd:
  start/restart: `sudo -n systemctl restart blave-agent-reconciler.service`
  stop: `sudo -n systemctl stop blave-agent-reconciler.service`
  **Never** start a tmux session while the unit is active, and **never** assume
  `tmux kill-session` stopped trading on such a machine — the systemd daemon keeps
  placing orders, and a tmux daemon started alongside it doubles every order.
- Only when the unit file does not exist (older machines) use the tmux session:
```
tmux new-session -d -s reconciler 'cd $BLAVECLAW_HOME/workspace && bash manager/start_reconciler.sh'
```
(resolve `$BLAVECLAW_HOME` first — same env var as `references/deployment.md`'s cron entries; defaults to `/root/.openclaw` if unset)
To check status: `tmux attach -t reconciler`. To stop: `tmux kill-session -t reconciler`.
Note: the systemd unit deliberately has no `[Install]` section — the reconciler must
NOT auto-start on reboot; the user re-enables trading explicitly after a reboot.

**Windows — NSSM service:**
```
nssm install blaveclaw-reconciler powershell.exe "-ExecutionPolicy Bypass -File %BLAVECLAW_HOME%\workspace\manager\start_reconciler_windows.ps1"
nssm set blaveclaw-reconciler AppDirectory %BLAVECLAW_HOME%\workspace
nssm set blaveclaw-reconciler Start SERVICE_DEMAND_START
nssm start blaveclaw-reconciler
```
(`%BLAVECLAW_HOME%` — resolve the actual env var on this machine before running these commands, don't type the literal placeholder; defaults to `C:\openclaw` if unset)
To check status: `nssm status blaveclaw-reconciler`. To stop: `nssm stop blaveclaw-reconciler`.
Note: `SERVICE_DEMAND_START` is required, never `SERVICE_AUTO_START` — same policy as the
Linux unit above: the reconciler must NOT auto-start on reboot; the user re-enables trading
explicitly. Crash recovery while the service is running is NSSM's AppExit restart, which is
independent of the start type.

**Capital (群益) reconciler wiring is hand-wired in `manager/reconciler.py`, not auto-wired.**
`lib.venue_wiring` deliberately excludes `"capital"` (`_NON_AUTO`) because its data shape differs
from every crypto venue — LOTS not account-currency notional, `buy`/`sell` not `long`/`short`, and
the order alias (`TM0000`) differs from the resolved contract code every position/report actually
carries (`TM2608`). `get_positions()`/`place_order()` in `reconciler.py` each contain a capital-only
branch (`_is_capital_routed()` / `exchange == 'capital'`) that:
- reads `lib.account_capital.get_positions()` — already lots, `buy`/`sell` — and translates
  `buy`→`long` / `sell`→`short`, size unchanged (**lots, not TWD notional** — see *`amounts`
  semantics* below)
- round-half-up's the target/actual lot diff to a whole lot (`math.floor(raw_lots + 0.5)`, not
  Python's `round()` — that does banker's rounding, which rounds a `0.5` tie down; a diff below
  0.5 lot rounds to 0 and places nothing. reconcile()'s account-currency `THRESHOLD` is
  crypto-notional scale and meaningless at lot count, so `lib.portfolio.compute_diff` skips it
  entirely for `futures_contracts` rows — this round-half-up is the only gate) and calls
  `lib.order_capital.place_futures_market_order()` with the near-month alias
  (`TX00`/`MTX00`/`TM0000`)
- is scoped to `asset_specs[strategy]["type"] == "futures_contracts"` only — a capital strategy
  configured as `"tw_stock"` (securities) raises loudly; that path is not wired yet

Every other machine (crypto exchanges — the overwhelming majority of the fleet) falls straight
through to the existing auto-wire (`auto_get_positions()` / `lib.execute.dispatch_order`),
untouched — the capital branches only activate when `portfolio_config.json["exchanges"]` actually
routes a strategy to `"capital"`.

`contract_value` and the alias↔resolved-contract-prefix mapping (`TX00`→`TX`/200,
`MTX00`→`MTX`/50, `TM0000`→`TM`/10) are a fixed table in `reconciler.py`
(`_CAPITAL_FUTURES_SPEC`), not user config — only the `TM0000`→`TM2608` prefix is live-verified
(2026-08-14); confirm `TX00`/`MTX00` on the first live TXF/MXF order and update the comment.
`contract_value` in that table is no longer read by any code path (see *`amounts` semantics*
below) — kept only as a documentation mirror of `capital-broker.md` Step 8's `asset_specs`.

**`amounts` semantics fork on `asset_specs[strategy]["type"]` (2026-08-14).** `strategy_amounts()`
returns `portfolio_config.json["amounts"]` verbatim (`lib/portfolio.py`); what that number MEANS
depends on the strategy's asset spec:
- `asset_specs[strategy]["type"] == "futures_contracts"` (currently only capital TW futures):
  the number IS a lot count — integer, no price involved anywhere in the chain (state.json
  `position` × `amounts[strategy]` = target lots directly; `_capital_get_positions()` reads actual
  lots directly; `_capital_place_order()` diffs lots directly).
- anything else (crypto, the default): the number is account-currency dollars, unchanged.

This was a same-day refactor away from a lots→TWD-notional→lots round trip (aggregate at save
time, convert back at order time) that priced BOTH conversions off `_txf_index_price()` — a ~1min
TXF close fetched fresh each time. Because the state.json snapshot and the reconciler poll happen
at different instants, the two price reads rarely matched, so the round trip introduced spurious
rounding drift with no economic meaning: a strategy with a constant signal (e.g.
`tmf_always_hold`, always emits `position=1`) should store exactly 1 lot forever, but the old path
could round it to 0 or 2 lots depending purely on how much the index moved between save and
execution. Storing/comparing lots directly removes the round trip and the drift with it —
`_txf_index_price()` no longer exists in `reconciler.py`.

**Consequence for `lib.portfolio.compute_diff`:** its `threshold` param (account-currency scale,
default 10) would otherwise swallow every capital order, since lot diffs are single/low-double
digits — `compute_diff` now skips `threshold` for any row where `asset_spec["type"] ==
"futures_contracts"` OR either side's `exchange == "capital"` (the latter covers a close-on-removal
row, which has no `asset_spec` because the strategy no longer appears in `target`).

**Consequence for `web/`:** the workspace's "交易所部位" (`buildPositionsSection` in
`agent/workspace.html`) renders target/actual/diff through `paintVenueMoney()` with a currency
suffix and a hardcoded `Math.abs(d) >= 10` highlight threshold — both currency-scale conventions.
For a capital-routed row this now displays a lot count formatted as money (e.g. "2 TWD" for 2
lots) and the order-eligible highlight no longer lines up with the real 0.5-lot gate. The 下單設定
amounts-input table (`pfClientTargets`, same file, lines ~3199-3339) was already updated same-day
to treat capital amounts as lots — this is the live-positions table's matching update, not yet
done; flagged for frontend-engineer.

**Capital (群益) broker exception:** NSSM services default to running as `LocalSystem`. Capital's
`SKCOM.dll` binds the certificate to the Windows identity that issued it (always `Administrator`
on Blave Agent machines — see `references/capital-broker.md` Step 2), so a service running as
`LocalSystem` fails `SKCenterLib_Login` with error 602 even though the cert is correctly installed.
If any portfolio in `portfolio_config.json["exchanges"]` uses `"capital"`, set the service identity
to Administrator before starting it:
```
nssm set blaveclaw-reconciler ObjectName .\Administrator "<Administrator password>"
```
Read the password from `C:\blave-agent\credentials\rdp_password.txt` on the machine itself
(`C:\openclaw\credentials\rdp_password.txt` on BlaveClaw machines; agent has local read access —
no need to ask the user, they'd only be repeating what's already on their own
「遠端桌面連線」dashboard card). Never change this password — the dashboard serves the
platform-stored copy, so a local reset locks the user out. No certificate export/import needed — this replaces the old
POC guidance about moving the cert to a different account store.

**After starting the reconciler, register it for health monitoring** — add to `state/deployments.json` (create the file if missing):
```json
{"reconciler": {"type": "daemon", "expect_every_minutes": 5,
                "registered_at": "<UTC now, %Y-%m-%dT%H:%M:%S>"}}
```
The reconciler touches `state/heartbeat/reconciler` on every poll loop; `manager/healthcheck.py` alerts the user if the heartbeat goes stale (see `references/deployment.md` › Deployment Healthcheck). Without this registration the healthcheck cannot see the daemon.

**Trace the full calculation chain before flagging an inconsistency.** If `state.json` shows a non-zero position but a field in `portfolio_config.json` (e.g. `weight=0`) seems contradictory, read `lib/portfolio.py` first. `contribution = account_value * leverage * weight * position` — a zero weight zeroes out the contribution by design. Do not report a bug until you have followed every variable through the aggregation logic.
