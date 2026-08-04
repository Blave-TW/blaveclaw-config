# Exchange Connect (web-initiated)

When the user connects an exchange from the web workspace's order-settings page, the
platform writes the API keys into the workspace `.env` directly (command channel — the
keys never pass through you), then a chat message asks you to finish the integration.
This file is that procedure. The goal: `lib/account_{id}.py` + `lib/order_{id}.py`
exist and `manager/reconciler.py` is wired through them.

`{id}` is the lowercase env-key prefix — the keys arrive as `{ID}_API_KEY` /
`{ID}_SECRET_KEY` (and sometimes `{ID}_PASSPHRASE`). Read the exact names from `.env`;
do not assume casing or invent names.

## Rules

1. **Never echo a key.** Read credentials with `dotenv_values()` inside code only. Key
   values must never appear in chat, logs, Telegram, error messages, or committed files.

2. **Check what already ships before writing anything.** BingX
   (`lib/account_bingx.py` + `lib/order_bingx.py`) and SinoPac (`lib/order_sinopac.py`)
   are implemented — extend, never rewrite. For everything else, work from
   `lib/account_TEMPLATE.py` and the patterns in `lib/order_bingx.py`
   (see `references/lib.md`, `references/manager.md` § Account library).
   **If both files already ship for this venue, skip straight to rule 7** — a stored
   key is not a working key (wrong permissions, missing IP whitelist, capital sitting
   in an account the lib doesn't read); validate read-only and report. A shipped lib
   is a starting point, not a fence: when the user wants different behaviour (e.g.
   count spot balance into equity), modify it in place — keep the contract, never
   rewrite from scratch. **Every path ends at rule 7**; writing or changing code just
   comes before it.

3. **Find the API docs in this order:**
   - ① `skills/blave-quant/references/{id}-skill.md` / `{id}-api-reference.md` — if a
     skill reference exists for this exchange, it is the source of truth (endpoints,
     auth/signing, broker attribution).
   - ② No skill reference → the exchange's official API docs on the web. Verify every
     response shape against a second independent source (official SDK code beats doc
     tables) before parsing it — doc tables are frequently wrong.

4. **`lib/account_{id}.py`** — implement `get_equity(env)` and `get_positions(env)`
   (reconciler shape: `{symbol: {'side': 'long'|'short', 'size': <account currency>}}`).
   **Symbol keys MUST be canonical: dashless uppercase (`BTCUSDT`), never the venue's
   own format (`BTC-USDT`, `BTC-USDT-SWAP`)** — the reconciler keys target and actual
   on this format; a venue-format key splits one instrument into two rows and churns
   buy/close orders against itself every round.
   Platform readers discover it by its exact filename — keep the naming convention.
   **Multi-account venues (spot / futures / funding / unified): `equity` means the
   equity of the account orders are placed on** — it is the position-sizing base, so
   summing across accounts would overstate what the strategies can actually trade
   with. Also return an optional `'accounts': {name: float}` breakdown (lowercase
   names like `trading` / `swap` / `spot` / `fund` / `funding`, USDT-denominated,
   best-effort per wallet) — the web shows it so money parked outside the trading
   account doesn't look like it vanished. **Include EVERY wallet the venue has**:
   wallet-split venues list each wallet (BingX: fund/spot/swap —
   `lib/account_bingx.py`); unified-account venues list the unified account as
   `trading` plus the separate `funding` wallet (OKX — `lib/account_okx.py` on a
   machine that has it). Once the file exists, the
   platform's account reader starts polling it automatically (every ~2 min, written
   to `manager/account.json`) — failures show on the web as `error`, so raise with
   readable messages (env key NAMES are fine, values never).

5. **`lib/order_{id}.py`** — copy `lib/order_TEMPLATE.py` (perp venues; Taiwan brokers
   follow `lib/order_sinopac.py` instead) and follow `lib/order_bingx.py`'s contract:
   confirmed orders (poll to terminal state; report exchange-reported fills, never the
   intent), unique client-order-id for idempotency, `lib/guard` halt + audit
   integration, and the exchange's broker attribution header/field (in the skill
   reference; without it the order still fills, so the omission is silent — include it
   from the first order). The reconciler calls FOUR fixed names —
   `get_contract_rules` / `format_qty` / `place_market_order` /
   `close_position_partial` — missing any of them crashes at reconcile time
   (mid-trade), not at integration time. Never rename or omit them.

6. **Wire `manager/reconciler.py`** through both files in the same session
   (order library → reconciler is one atomic task — AGENTS.md).

7. **Validate read-only, then stop.** Run `get_equity()` and `get_positions()`, show
   the numbers (every wallet, when the venue splits them), and ask the user to
   confirm they match the exchange app. Place NO order — live trading starts later
   via the reconciler, not as an integration test. Never clear a halt as part of
   this flow. **Do not tell the user to move funds to any particular wallet** —
   spot strategies trade the spot wallet, perp strategies the futures wallet, and
   which applies is unknown until a strategy is deployed; presenting the balances
   without a transfer instruction is the correct ending. Transfer advice belongs
   at deployment time, matched to the strategy actually being deployed.

8. **Done = the two files exist.** The machine's portfolio reporter detects them and
   the web page flips the venue to ready on its own — no extra reporting step.
