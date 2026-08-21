# Exchange Connect (web-initiated)

When the user connects an exchange from the web workspace's order-settings page, the
platform writes the API keys into the workspace `.env` directly (command channel — the
keys never pass through you), then a chat message asks you to finish the integration.
This file is that procedure. The goal: `lib/account_{id}.py` + `lib/order_{id}.py`
exist and `manager/reconciler.py` is wired through them.

**Taiwan brokers (`sinopac`/`president`/`capital`) are NOT covered by this file, even
though their web handoff message matches the trigger above** — see AGENTS.md's Broker
Onboarding section and go straight to that broker's own reference doc instead. This
matters most for `capital`: its libs ship pre-built (not agent-written), so rule 2's
"files already exist → skip to rule 5" fast path below matches on the first message,
runs `get_equity()` before the user has done ANY of the certificate/RDP/agreement
onboarding, and rule 5's crypto-flavored "auth / permission / IP whitelist" framing
would misreport that as a bad key — Capital has no IP whitelist concept at all, and the
actual fix is `capital-broker.md`'s guided RDP flow, not "check your key."

`{id}` is the lowercase env-key prefix — the keys arrive as `{ID}_API_KEY` /
`{ID}_SECRET_KEY` (and sometimes `{ID}_PASSPHRASE`). Read the exact names from `.env`;
do not assume casing or invent names.

## Rules

1. **Never echo a key.** Read credentials with `dotenv_values()` inside code only. Key
   values must never appear in chat, logs, Telegram, error messages, or committed files.

2. **Check what already ships before writing anything.** BingX
   (`lib/account_bingx.py` + `lib/order_bingx.py`), Binance
   (`lib/account_binance.py` + `lib/order_binance.py`), OKX
   (`lib/account_okx.py` + `lib/order_okx.py`), Gate.io
   (`lib/account_gateio.py` + `lib/order_gateio.py`), Paper trading
   (`lib/account_paper.py` + `lib/order_paper.py` — simulated venue, no keys;
   the web handoff for it needs only rule 5 plus one sentence on how fills
   are priced, see `references/lib.md`) and SinoPac
   (`lib/order_sinopac.py`) are implemented — extend, never rewrite. For everything else, work from
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
   On success: report the numbers (every wallet, when the venue splits them), ask
   the user to check them against the exchange app, and **continue straight into
   rules 6–7 without waiting for the answer** — the integration finishes in ONE
   pass (measured 2026-08-04: stopping here read as "the agent said done but the
   page still says incomplete"). The gate's job is catching a dead key cheaply,
   not pausing on a live one; a number mismatch reported later is fixed in the
   account lib without touching the order code. Place NO order — live trading
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

   **Limit layer (execution styles need it — see `references/lib.md` › Execution
   styles).** The chase-limit executor calls FIVE more fixed names; a venue missing
   any of them silently degrades every chase order to market (loud Telegram fallback),
   so ship the full set with any new integration:
   - `place_limit_order(env, symbol, direction, qty, price, client_order_id=None,
     reduce_only=False, time_in_force="GTC", post_only=False)` — MUST return a dict
     containing `order_id` (the venue's own id: every later cancel/status call is by
     order_id, never by client id — client-id lookup is not portable across venues).
     `post_only=True` maps to the venue's own dialect (Binance `timeInForce=GTX`,
     OKX `ordType=post_only`, Gate.io `tif=poc`, …); a post-only rejection (the
     order would have crossed) RETURNS `{'status': 'post_only_rejected'}` instead
     of raising — the chase executor re-reads the book and re-posts on it. The lib
     formats `price` to the instrument's tick itself (callers pass a raw float).
   - `cancel_order(env, symbol, order_id)` — cancel by order_id; idempotent-safe:
     "already filled / not found" returns a status instead of raising, the caller
     always re-reads fills afterwards.
   - `get_order(env, symbol, order_id)` — MUST include `orig_qty` alongside
     `executed_qty` / `avg_price` / `status` (partial-fill accounting needs the
     original size, not just the filled part).
   - `get_bbo(env, symbol)` → `{'bid': float, 'ask': float}` — real top-of-book,
     never mark/last price (several venues' tickers already carry bid/ask fields —
     read them; do not substitute `get_mark_price`).
   - `get_open_orders(env, symbol=None)` — open resting orders; each row MUST
     carry `symbol` (canonical dashless-uppercase), `order_id` and
     `client_order_id` — the reconciler's startup sweep matches the
     `rc<timestamp>` client-id fingerprint and cancels by (symbol, order_id).
   Spot-capable venues ship the spot twins (`place_spot_limit_order`,
   `cancel_spot_order`, `get_spot_order` with `orig_qty`, `get_spot_bbo`,
   `get_spot_open_orders`). New mutating endpoints (cancel, limit place) must be
   registered in the lib's `_MUTATING_PATHS`/guard classification — the existing
   `_order_intent` logic keys off reduce semantics, not order type, so limit orders
   classify correctly without guard changes.

7. **Reconciler wiring is AUTOMATIC for official venues** — the template's
   `get_positions`/`place_order` route through `lib/venue_wiring.py`, which
   detects the bound venue and maps the USD-diff contract (swap + spot) for
   you; do not touch `manager/reconciler.py` for Binance/BingX/OKX, and a
   rebind to another official venue needs no reconciler change. For a venue
   WITHOUT official libs, replace the two function bodies in the same session
   as the order lib (order library → reconciler is one atomic task —
   AGENTS.md); their docstrings carry the contract.

8. **Done = the two files exist and rule 5 passed.** The machine's portfolio
   reporter detects the files and the web page flips the venue to ready on its
   own — no extra reporting step.
