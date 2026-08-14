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

## Additional Rules

**Before running `manager/manager.py` for weight optimisation:** temporarily set `END = None` in every strategy file so the optimiser uses the latest data. After the run, restore each strategy's fixed past date (roughly one week ago) for normal cache-backed backtests.

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
