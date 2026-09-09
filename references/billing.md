# Billing — what costs what

The single answer sheet for 「這樣會不會扣錢」/「扣什麼錢」questions. Everything below was read
from the platform code, not remembered — sources are named per section. If a question is not
covered here, say you are not sure and point the user to the usage page; never invent a number.

Sources (api repo): `account/credit.py` (`PRICING`, `LLM_PRICING`, `WEB_SEARCH_PRICING_TWD`,
`deduct_credit`, `deduct_blave_api_credit`, `deduct_server_credit`), `openclaw/lightsail.py`
(`SERVER_TIERS`, `WINDOWS_SERVER_TIERS`), `openclaw/proxy.py` (`_deepseek_peak_multiplier`,
`deduct_llm_credit` call sites), `snapshot/cron_deduct_server_credit.py`, `account/agent_trial.py`,
`decorators.py` (`api_plan_required`).

## The wallet

- One prepaid credit balance in TWD. Top-up sizes: 300 / 800 / 1500 / 3000 TWD. With a card bound,
  auto top-up adds 300 TWD when the balance drops below 100 TWD.
- Three meters draw from it, and only three: **server** (`usage_vm`), **LLM** (`usage_llm`),
  **data** (`usage_blave`). Marketplace purchases are a fourth line item but not a meter.
- Itemised list: web usage page `/agent/<lang>/usage` (every deduction with its description).

## Meter 1 — server hourly (`usage_vm`) — includes Blave data

A flat hourly rate for the machine, charged once per hour by a platform cron
(`cron_deduct_server_credit.py`). Description on the usage page reads
`Server hourly (<tier>, data included): 1h`.

**Lead with the month, not the hour** — `month = hour x 720` (30 days). Answer "what does this
machine cost?" with the monthly figure and give the hourly rate beside it as the mechanism.
Billing itself is unchanged: still one deduction per clock hour.
**Use the hourly figure instead** when explaining a specific deduction row, the usage page, or a
stop/start question — the month is only for "what does this cost" questions.
**This rule is the server fee only.** Never multiply the 3 TWD data fee (Meter 2) by 720: it is
charged per *active* hour, so a monthly figure for it would be fiction.
Amounts here are TWD, which is what the wallet holds and what every deduction is in; the English,
Japanese, Vietnamese, Spanish and Portuguese site faces display USD at 30 TWD/USD
(48 / 144 / 276 per month on Linux, 132 / 204 / 396 on Windows).

| OS | Tier | vCPU / RAM | TWD per month (30 d) | TWD per hour |
|---|---|---|---|---|
| Linux | Starter (default) | 2 / 4 GB | 1,440 | 2.0 |
| Linux | Premium | 4 / 16 GB | 4,320 | 6.0 |
| Linux | Max | 8 / 32 GB | 8,280 | 11.5 |
| Windows | Starter (default) | 2 / 8 GB | 3,960 | 5.5 |
| Windows | Premium | 4 / 16 GB | 6,120 | 8.5 |
| Windows | Max | 8 / 32 GB | 11,880 | 16.5 |

- **The monthly figure is exact, not an estimate** — the meter runs whether the machine is
  running or stopped (see next bullet), so a machine that exists for a full 30 days costs exactly
  that. Say "per 30 days" rather than "about"; a 31-day calendar month is 24 hours more.
  **Exceptions that make a month short of full** — check before quoting a month to someone who
  just signed up: the 14-day card-bound free trial waives the server fee entirely (last bullet of
  this section, Linux Starter only, so a new user's first 30 days is 768 TWD not 1,440); a machine
  still `installing` is not yet metered; and a balance under 50 TWD stops the machine.

- **Billed while the machine exists — running OR stopped.** The cron selects
  `status IN ('running', 'stopped')`. Stopping the machine does not stop the meter; only deleting
  it does.
- **Blave data is bundled.** An account that owns a machine (running or stopped) is never charged
  `usage_blave` (`deduct_blave_api_credit` short-circuits on `has_machine`). Every `lib/data.py`
  fetch, backtest, param scan, cron-scheduled strategy, watchboard script and scheduled report is
  covered by the hour already paid.
- Free trial (card-bound, 14 days, Linux Starter only): the server hour is not charged and no
  transaction row is written for it.

## Meter 2 — data fee (`usage_blave`) — machine owners never pay it

Kept for external API-key callers only. Rule in `deduct_blave_api_credit`:

- Exempt outright: API-plan subscribers; any account with a Blave Agent machine (running or stopped).
- Everyone else: **3 TWD per UTC clock hour in which at least one Blave data call was made** — a
  Redis key `blave:api_hourly:<uid>:<YYYY-MM-DD-HH>` is set on the first call and short-circuits
  the rest of that hour. It is **never per call**: 1 call and 1,000 calls in the same hour cost the
  same 3 TWD, and an hour with no calls costs nothing.
- Applies to every endpoint behind `@api_plan_required` / `token_or_api_plan_required` — i.e. the
  data endpoints `lib/data.py` talks to. Nothing else is metered as data.
- History: before the data fee was bundled into the server hour, machine owners did see this
  3 TWD line item once per active hour. Users who remember "being charged every hour for the API"
  are describing that old regime; it no longer applies to them.

## Meter 3 — LLM (`usage_llm`) — every chat turn

Charged per proxy request (`openclaw/proxy.py`), i.e. every model call inside a turn — a turn that
uses several tools is several model calls, each billed on its own token counts (input, cache
write, cache read, output). Prices are TWD per 1M tokens (`LLM_PRICING`):

| Model | input | cache write | cache read | output |
|---|---|---|---|---|
| Haiku | 40 | 50 | 4 | 200 |
| Sonnet (default) | 120 | 150 | 12 | 600 |
| Opus | 200 | 250 | 20 | 1000 |
| Fable | 400 | 500 | 40 | 2000 |
| deepseek-v4-flash | 8.25 | 8.25 | 0.275 | 24.75 |
| deepseek-v4-pro | 24.75 | 24.75 | 0.825 | 74.25 |

- Claude prices = list price USD × 1.25 × 32 TWD/USD; DeepSeek = list price RMB × 4.4 × 1.25.
- **DeepSeek peak surcharge: ×2 during Beijing 09:00–12:00 and 14:00–18:00** (the proxy doubles
  the token counts before deducting). No surcharge on Claude models.
- **Web search: 0.4 TWD per search, Claude models only** (Anthropic server-side tool). DeepSeek
  paths never bill it.
- Model match order (`_get_llm_pricing`): `deepseek-v4-pro` → any other `deepseek` (flash) →
  `haiku` → `opus` → `fable` → otherwise Sonnet. Switching is per session and applies from the
  next message (`references/models.md`).
- Free trial: LLM usage draws from a separate 100 TWD allowance instead of the balance; when the
  allowance is used up the proxy refuses further model calls until the trial ends.
- Long conversations cost more per turn (the whole context is input every call); cache reads are
  10× cheaper than fresh input, so the runtime's prompt caching is what keeps that in check.

## What does NOT call an LLM (Blave Agent runtime)

None of these add anything beyond the server hour already paid:

- A backtest, param scan or MCPT run — CPU on the machine. (The chat turn that launches it and
  reads its output is billed as LLM tokens like any other turn.)
- A deployed strategy on the system cron / Scheduled Task (`wait_for_bar.py`, `run_strategy.sh`).
- A watchboard widget script (`lib/watch.py`; deterministic code by contract).
- A scheduled report job (`report_jobs/<id>/run.py`, data-only `publish(pack)`).
- Any `lib/data.py` fetch, cached or not — data is bundled.
- Telegram / web notifications sent by scripts.

On the **old OpenClaw runtime** only, an *agent cron* is a real chat turn and burns LLM tokens on
every wake-up — that is why `references/deployment.md` forbids per-tick agent crons.

## Answering the common questions

- 「在這邊聊天會消耗 token 嗎？」— Yes. Every message, on Telegram or the web workspace, is
  billed as LLM tokens at the current model's rate. Nothing else on the machine is.
- 「每小時都被收 API 使用費，把 cron 排在同一小時省錢？」— A machine owner is not charged the data
  fee at all; data is inside the server hour. Spacing or bunching crons changes nothing on the
  bill. (Even under the old per-hour data fee, bunching only mattered because the fee was
  per-active-hour, never per call.) Schedule crons on what the strategy needs, not on billing.
- 「聊天用 Flash、寫 code 才換 Pro 省錢嗎？」— Yes, that is a real saving: flash is roughly a third
  of pro per token, and the switch is per session with no restart. Mention the DeepSeek peak-hour
  ×2 and that Claude models cost more but have no surcharge.
- 「停機會不會扣錢？」— Yes, a stopped machine is still billed the server hour; only deleting it
  stops the meter.
- 「回測／掃參數／看盤板／定期報告會扣錢嗎？」— Nothing beyond the server hour; the only extra is
  the LLM tokens of the chat turn you are in.
- 「Web search 會扣錢嗎？」— 0.4 TWD per search on Claude models; not billed on DeepSeek.
