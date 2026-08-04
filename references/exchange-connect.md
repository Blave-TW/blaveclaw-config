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
   **If both files already ship for this venue, skip straight to rule 5, and once
   it passes go directly to rule 8** (rules 6–7 are for venues whose order lib
   doesn't exist yet — don't rewrite what already ships) — a stored
   key is not a working key (wrong permissions, missing IP whitelist, capital sitting
   in an account the lib doesn't read); validate read-only and report. A shipped lib
   is a starting point, not a fence: when the user wants different behaviour (e.g.
   count spot balance into equity), modify it in place — keep the contract, never
   rewrite from scratch. **Every path passes through the rule-5 validation gate**;
   writing or changing account code just comes before it.

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
   **The auth-failure path is part of the contract, not an afterthought**: a bad /
   revoked / IP-blocked key MUST raise with the exchange's own error code and
   message. Never swallow an auth error into zeros (a user with a typo'd key then
   sees "equity $0" and thinks their money vanished), and never let the error
   response crash the parser (`'str' object has no attribute 'get'` tells the user
   nothing). Test the module against a deliberately broken signature before calling
   it done — the error string is what the web shows the user.

5. **Read-only validation gate — BEFORE any order code.** Run `get_equity()` and
   `get_positions()` now. **If the read fails (auth / permission / IP whitelist),
   STOP HERE**: report the readable error and wait for the user to fix the key —
   do not write `lib/order_{id}.py`, do not touch the reconciler. A wrong key must
   cost one small module, not a whole integration (measured 2026-08-04: an agent
   wrote a 15KB order lib against a dead key — minutes of work validating nothing).
   On success: show the numbers (every wallet, when the venue splits them) and ask
   the user to confirm they match the exchange app. Place NO order — live trading
   starts later via the reconciler, not as an integration test. Never clear a halt
   as part of this flow. **Do not tell the user to move funds to any particular
   wallet** — spot strategies trade the spot wallet, perp strategies the futures
   wallet, and which applies is unknown until a strategy is deployed; presenting
   the balances without a transfer instruction is the correct ending. Transfer
   advice belongs at deployment time, matched to the strategy actually deployed.

6. **`lib/order_{id}.py`** — copy `lib/order_TEMPLATE.py` (perp venues; Taiwan brokers
   follow `lib/order_sinopac.py` instead) and follow `lib/order_bingx.py`'s contract:
   confirmed orders (poll to terminal state; report exchange-reported fills, never the
   intent), unique client-order-id for idempotency, `lib/guard` halt + audit
   integration, and the exchange's broker attribution header/field (in the skill
   reference; without it the order still fills, so the omission is silent — include it
   from the first order). The reconciler calls FOUR fixed names —
   `get_contract_rules` / `format_qty` / `place_market_order` /
   `close_position_partial` — missing any of them crashes at reconcile time
   (mid-trade), not at integration time. Never rename or omit them.

7. **Wire `manager/reconciler.py`** through both files in the same session
   (order library → reconciler is one atomic task — AGENTS.md).

8. **Done = the two files exist and rule 5 passed.** The machine's portfolio
   reporter detects the files and the web page flips the venue to ready on its
   own — no extra reporting step.
