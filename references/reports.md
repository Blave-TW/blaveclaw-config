# Reports — publishing a rendered report to the workspace

A **report** is a JSON document this machine writes and the platform renders in the
web workspace sidebar: KPI rows, charts, tables and prose, laid out by the web from
structured data — not a screenshot, not a wall of Telegram text. Use it for anything
the user will want to read again later: a performance review, a morning briefing on a
watchlist, an MCPT / research write-up, a post-mortem of a live week.

The platform pushes a short summary notification once the report is stored, so the
report reaches the user even when this machine is asleep — never send your own
Telegram message about a report as well, that duplicates every alert.

§1–§6 are the **format** contract; **§7 is the content bar** — what a report has to
actually say to be worth reading. A report can satisfy every rule in §1–§6 and still
be worthless, so read §7 before you write the prose.

## 1. How to publish — the drop directory

Write the report to `workspace/reports/<id>.json`. That is the whole contract: no
token, no API call, no library needed. The runtime's uploader watches the directory
and ships whatever lands there.

- **`<id>` is the file name stem and the report id**: `[A-Za-z0-9_-]{1,64}`. Sending
  the same id again **overwrites** that report on the platform — deterministic ids
  make a re-run idempotent; date-stamped ids keep every run.
  **Reserved:** `daily-YYYY-MM-DD` and `wk-YYYY-MM-DD` belong to the runtime's own
  performance reports — reusing them overwrites the official report.
- **Write atomically**: write `<id>.json.tmp` (any name not ending in `.json` is
  ignored by the scan) and `os.replace()` it into place. Belt and braces on top of
  that: the uploader leaves any report whose own mtime — **or that of any picture in
  its sidecar** — is younger than 2 seconds for the next tick, so a half-written file
  is never parsed.
- **Figures ride in a sidecar directory, `reports/<id>.files/`** — see §5. **Write the
  pictures first and the report JSON last**: the JSON landing is what makes the whole
  set visible, and by then everything it references is already on disk.
- The envelope `id` is filled in from the file name when absent; when present and
  **different**, the report is refused rather than guessed at.
- After upload the file moves to `reports/sent/` (last ~20 kept). A report refused
  for good moves to `reports/failed/`, with the reason appended to
  `reports/upload_errors.log` — the api's message names the offending field path
  (`blocks[3].items[1].value`), so read that file before rewriting anything.
  **The sidecar travels with its report** into either directory; a `<id>.files/` left
  behind with no report is swept a day later.

`lib/report.py` does the above for you:

```python
from lib.report import write_report, status

write_report(
    "mcpt-2317-20260901",           # id == file name; [A-Za-z0-9_-]{1,64}
    "2317 MCPT 檢定",                # 1–200 chars, sidebar title
    [                               # blocks — a meta block is prepended for you
        {"type": "text", "variant": "lead", "markdown": "p = 0.012, ..."},
        {"type": "kpi_row", "items": [
            {"label": "p-value", "value": "0.012", "tone": "pos"},
            {"label": "Permutations", "value": "1,000", "tone": "neutral"}]},
    ],
    type="research",                # performance | morning | research
    report_type="一次性",            # header display string; defaults to `type`
    meta={"machine": "blave-agent-01"},   # optional meta props, see §3
    images={"perm.png": open("tmp/perm.png", "rb").read()},   # → <id>.files/, see §5
)

# Diagnosis only — never a step after write_report; see "The write is the finish line".
status("mcpt-2317-20260901")   # 'pending' | 'sent' | 'failed: <reason>' | 'unknown'
```

`write_report` checks only the report id and the image file names (a name is used to
write a file, so it must not be a path) — every other rule is enforced downstream,
where the error message is more precise than anything this side could reproduce. It
writes the pictures before the JSON, in the order the drop dir requires.

**The write is the finish line.** Once the JSON is in the drop dir the report is
produced and you are done — tell the user it has been produced and will show up in the
workspace sidebar shortly, then move on. Shipping it is the runtime's job: a 2-minute
timer picks the file up, so in the normal case the report appears within about two
minutes. **Do not poll `status()`, and do not wait for `pending` to turn into `sent`
before replying** — every extra tool call there is the user paying to watch a timer that
has not fired yet.

`status()` is a diagnostic for afterwards — the user says the report never showed up,
or you have reason to think it was refused. That is the failure worth knowing about: a
report whose format the api rejects moves to `reports/failed/` with the reason appended
to `reports/upload_errors.log` (the message names the offending field path), and it will
never arrive on its own. `'unknown'` is not a failure — it also means "sent a while ago
and already pruned from `sent/`".

Old BlaveClaw machines (pre-Blave-Agent runtime) have no uploader; files just
accumulate in `reports/`. If `reports/sent/` does not exist on this machine, do not tell
the user the report will appear in the sidebar.
The sidecar is newer than the rest of this page: a runtime that predates it passes a
`file` field straight through to the api, which refuses it as an unknown prop. If a
report lands in `failed/` for that reason, this machine's runtime is too old — upload
the picture yourself and reference it by `sha256` (§5), or leave it out.

## 1b. Templates — the data half is already written

For the three morning-brief types the deterministic half lives in `lib/report_templates.py`.
A template fetches every series through `lib.data`, builds the KPI row, charts, tables and
the footnote in contract shape, and hands back a `Pack` with the figures it used
(`pack.context`) and the narrative slots left for you (`pack.slots`). You add the
judgement; you do not touch the blocks.

```python
from lib.report_templates import tw_market_brief, crypto_market_brief, symbol_brief, publish

pack = tw_market_brief()                 # today (Taipei); headers come from the workspace .env
print(pack.describe())                   # every figure the pack carries, one line each — cite these
#   [tw-market-20260902] 台股大盤晨報          ← title has no date: the sidebar row shows when it was made
#     加權指數: 46,948.72(+1.78%),20 日高 46,948.72
#     三大法人: 外資 +267.0 億(昨 -144.0 億)、投信 +131.0 億、自營 +163.0 億、合計 +561.0 億
#     外資期貨淨多單: +12,300 口(+2,500 口,09-01)
#     缺少:  - 台指期 2026-09-01 無夜盤 bar(…)      ← a missing series is a missing block, never a guess
#     narrative slots: lead≤600, read≤2400, action≤1500, risk≤900

publish(pack, narrative={
    "lead":   "外資現貨與期貨同日轉多,量能放大六成——這是資金回補,不是空窗反彈。",
    "read":   "…what the numbers say and why (markdown, §4 subset)…",
    "action": "…what to do about it, with levels…",
    "risk":   "外資連兩日淨賣超逾 150 億,或淨多單回落到 1 萬口以下,這份解讀作廢。",
})
```

- `crypto_market_brief(symbols=("BTC", "ETH", "SOL"))` — price / returns table, rebased
  performance, BTC funding, the market-wide Blave indicators, today's macro events.
- `symbol_brief("2330")` — Taiwan stock: close / volume / 外資買賣超 (張), key levels (20 日高低,
  5/20/60 日均); `symbol_brief("BTC")` — crypto perp: price, funding, 爆倉 / 巨鯨 / 多空力道.
- Slots: `lead` becomes the opening card (one falsifiable claim), `read` / `action` become
  sections after the data, `risk` a warning callout before the footnote. Each has a character
  cap (`pack.slots`); `publish` raises past it — cut, do not summarise.
- **Scheduled run = `publish(pack)` with no narrative** (data-only, `origin: scheduled`). The
  runtime has no timer that wakes the agent, so a cron job cannot carry a judgement; a canned
  sentence in a script is a view nobody formed. Ids are date-stamped and the data-only form
  gets an `-auto` suffix (`tw-market-20260902-auto`), so a scheduled run never overwrites the
  narrated report you produced in chat the same day; re-running the same form the same day
  overwrites itself.
- `pack.notes` lists what the source did not have (e.g. 期貨法人 not published yet, no night
  bars); the corresponding block is simply absent. Say so in the narrative if it matters;
  never fill the gap with a number.

Run it from the workspace root so `lib` imports: `python3 -c '…'` from `/opt/blave-agent/workspace`, or
`PYTHONPATH=/opt/blave-agent/workspace python3 tmp/make_brief.py` — `python3 tmp/x.py` alone puts `tmp/` on
`sys.path`, not the workspace, and `from lib.report_templates import …` fails (seen on 29026, three retries).

Headers, if you need `lib.data` outside a template: `headers_from_env()` in the same module
reads `blave_api_key` / `blave_secret_key` from the workspace `.env` (see `references/lib.md`).

## 2. Envelope

```json
{
  "schema_version": "1.1",
  "id": "wk-2026-08-31",
  "type": "performance",
  "title": "績效週報 08/25–08/31",
  "created_at": 1756684800,
  "blocks": []
}
```

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | `"1.1"` — the only accepted value today. |
| `id` | string | `[A-Za-z0-9_-]{1,64}`, equal to the file name stem. |
| `type` | string | `performance` / `morning` / `research` — sidebar grouping. |
| `title` | string | 1–200 chars. |
| `created_at` | int | **unix seconds, UTC** — never milliseconds, never a string. |
| `blocks` | array | 1–120 blocks. |

Whole document ≤ **2 MB** (bigger is refused, not truncated).

## 3. Blocks

A block is `{"type": "<key>", ...props}`. The array is flat — blocks never nest.
**An unknown type or an unknown prop is refused (400), not ignored**: a typo in a
field name loses the report, so copy names from this page rather than inventing them.
Strings are ≤200 chars unless stated. `?` marks optional.

Most visual blocks (`kpi_row`, all charts, `metric_table`, `table`, `code`, `image`)
also accept `title?` (≤80, the section heading above the block) and `caption?` (≤300,
the small print below it — put the measurement basis there).

| Block | Required props | Limits / notes |
|---|---|---|
| `meta` | `title`, `report_type`, `generated_at` | **Exactly one, always first.** Optional: `period` `{from, to}` display strings ≤32 (`"08/25"`), `account` `{aum: number, currency}`, `benchmark`, `origin` (`scheduled`/`chat`), `machine`, `extra` (≤3 `{label, value}`). `period` + `account` + `benchmark` + `extra` ≤4 header cells in total. |
| `kpi_row` | `items[{label, value, tone}]` | 1–6 items; **the first is the focus** and renders largest. `label` ≤40, `value` a formatted string, `tone` = `pos`/`neg`/`neutral` (unsigned numbers such as Sharpe or win-rate are `neutral` — a wall of green means nothing). Optional `unit` ≤16, `delta`. |
| `line_chart` | `series[{name, role, points}]` | 1–4 series; `role` = `primary` (solid, **at most one**) or `benchmark` (dashed); `points` = 1–5000 `[t, v]`, `t` unix seconds int, `v` finite number. Optional `y_unit` (≤8, see *Axis units* below), `bands` (≤2 `{from, to, label}`, unix seconds, label ≤32) and `reflines` (≤4 `{y, label, emphasis}`, `emphasis: true` = red loss level). |
| `drawdown` | `points` | 1–5000 `[t, v]`, `v` a **negative percent** (−9.84 = −9.84%). Optional `maxdd` `{value, from, to}` (unix seconds). No unit field — the contract pins this chart to negative percent. |
| `heatmap` | `variant`, `values` (+ `rows`,`cols` or `labels`) | `variant` = `calendar` (needs `rows` ≤40 years, `cols` ≤20 months — an annual / total column goes in `cols` too) or `matrix` (needs `labels` ≤40, values −1…1). `values` is 2-D, shaped rows×cols / labels×labels; `null` renders as an em-dash (future months, the diagonal). Optional **`emphasis_cols`** (**calendar only**): unique integer indices into `cols` marking the columns to render with added weight — that annual / total column. The web cannot tell which column is the total (`cols` is plain strings and not every calendar has one), so say it here. On a `matrix` heatmap `emphasis_cols` is an unknown prop → refused. |
| `bar_chart` | `variant` + `items` or `segments` | `variant` = `bars` (`items` ≤60 `{label, value}`, signed, zero axis) or `stacked` (`segments` **2–4** `{label, value}`, value ≥0, normalised into widths). Only four category colours exist, so a 5th segment would repeat one. **Merging the tail into an "Other" segment is your decision, not the web's** — it cannot know which segments to fold or how to say so; fold them here and explain the fold in `caption`. No unit field on either variant. |
| `histogram` | `bins[{x0, x1, count}]` | 1–200 bins, `x0 < x1`, `count` a non-negative int. Optional `x_unit` / `y_unit` (≤8, see *Axis units*) and `reflines` ≤4 `{x, label, emphasis}` (vertical). |
| `box` | `groups[{label, min, q1, median, q3, max}]` | 1–40 groups, label ≤40, the five numbers monotonically non-decreasing. Optional `outliers` ≤50 numbers and `y_unit` (≤8, see *Axis units*) — there is **no `x_unit`**: the x-axis is the group labels, not a numeric scale. State the whisker basis (P5–P95 or true extremes) in `caption` — no field carries it. |
| `scatter` | `points[{x, y}]` | 1–2000 points; optional per-point `label` and `role` = `focus`/`context` (omit `role` everywhere for a single population). Optional `x_unit` / `y_unit` (≤8, see *Axis units*) and `regression` `{slope, intercept}` — put slope / R² in the `caption`, they are not drawn. |
| `metric_table` | `items[{label, value}]` | 1–60 pairs; `label` and `value` are both strings ≤60, `value` already formatted. Label-value grid, no header row. Optional per-item `format` = `text` (default) / `number` / `percent` / `date` — the same field name and the same four values as a `table` column, and **the same colour gate, see below the table**. The grid has no columns, so it hangs off the item instead. |
| `table` | `columns[{key, label, align}]`, `rows` | ≤20 columns; `key` = `[A-Za-z0-9_]{1,40}`, unique within the table; `label` ≤40; `align` = `left`/`right`/`center` (numeric columns are always `right`); optional `format` = `text` (default) / `number` / `percent` / `date` — **it gates the up/down colouring, see below the table**. ≤500 rows, values string / number / `null` (→ em-dash). **A row key not declared in `columns` is refused.** |
| `text` | `markdown` | ≤20000 chars, subset in §4. Optional `variant: "lead"` — the opening conclusion card: **at most one, and it must be the block right after `meta`**. |
| `quote` | `text` | ≤500; optional `cite` ≤120. Pull quote — only a sentence already made in the body, ≤2 per report. |
| `footnote` | `items[{id, text}]` | 1–30 items; `id` = `[A-Za-z0-9_-]{1,32}`, unique in the report; `text` ≤1000. **At most one footnote block, and it must be the last block.** |
| `code` | `lang`, `source` | `lang` = `[A-Za-z0-9+#_.-]{1,20}` (`text` when there is no language); `source` ≤20000. |
| `divider` | — | No props. **Neither de-duplicate them nor judge whether one belongs**: the web omits a divider whenever the next thing already opens itself (a block `title`, a markdown H2/H3, the head or foot of the report, a `footnote`, a second adjacent divider). Drop one wherever a break reads right; **a divider you inserted that does not appear is the expected outcome, not a bug** — do not go hunting for it. |
| `callout` | `tone`, `text` | `tone` = `warning`/`info`; `text` ≤2000; optional `title` ≤120. |
| `image` | `file` **or** `sha256`, plus `alt` | **Exactly one of the two references, never both** (both = refused here on the machine). `file` = a plain file name in `reports/<id>.files/` — `[A-Za-z0-9][A-Za-z0-9._-]{0,79}`, never a path; the extension picks the MIME type (`png`/`jpg`/`jpeg`/`webp`/`gif`) and each picture is 1 byte–2 MB. `sha256` = `[0-9a-f]{64}` of an image already on the platform (§5). One name referenced by several blocks uploads once. `alt` ≤200 is **required** (accessibility, no default). Optional `caption` ≤300. The platform adds `url` and the pixel `w`/`h` when the report is read back — **never send `w`/`h` yourself**, they are unknown props and the report is refused. |

**Numbers vs display strings — the mistake to check for first.** Chart data
(`line_chart` / `drawdown` / `heatmap` / `bar_chart` / `histogram` / `box` / `scatter`
coordinates and values) must be **real numbers** — the web computes scales from them.
Display fields (`kpi_row.items[].value`, `metric_table.items[].value`, `table` cells)
must be **already-formatted strings** (`"+1.82%"`, `"24,318.77 USDT"`): thousands
separators, sign and decimals are decided here and printed verbatim. Every number must
be finite — `NaN`/`Infinity` is refused (`lib/report.py` raises on them locally).

**Axis units.** `line_chart` and `box` take `y_unit`; `histogram` and `scatter` take `x_unit`
and `y_unit`. Each is a display suffix of ≤8 chars (`"%"`, `"USDT"`, `"bp"`) printed on the axis
labels — it never converts or scales the numbers in `points` / `bins`. Nothing else carries
the unit: the web cannot tell a percent series from an equity-in-USDT one, and guessing `%`
would turn your data into a false statement. Omit it and the axis prints bare numbers.
`bar_chart` and `drawdown` have **no** unit fields at all (`drawdown` is fixed to negative
percent by the contract), and **`box` has a `y_unit` but no `x_unit`** — its x-axis is the
group labels, a category axis with nothing to suffix. A unit field on a block that does not
declare one is an unknown prop and loses the report.

**Colour is gated by `format` — in `table` columns and `metric_table` items alike — and that
makes the sign your job.** Only a column or an item declared `number` or `percent` gets
up/down colour, and the judgement is purely the **first character of the displayed string**:
`+` renders green, `−`/`-` renders red, anything else stays neutral. `text` (the default) and
`date` are never coloured, in either block. The web deliberately does not read the `label` to
infer meaning — a keyword rule would break across languages and custom names. The gate cuts
both ways, and the two halves are complementary:

- **A signed value that is not a profit or a loss stays neutral by staying `text`.** `Net
  Exposure` shown as `+0.62×` is a direction, not money made or lost; leave it at the default
  and the `+` prints with no green tint. Reach for `number` / `percent` only where the sign
  really does mean gain or loss.
- **A figure whose value is positive but whose meaning is negative — VaR, Max DD, worst loss,
  largest adverse excursion — is written with a negative sign here** (`"−1.12%"`, not
  `"1.12%"`). That is not cosmetic; P&L-facing figures are stated as their effect on equity,
  so a loss carries a minus.

There is no per-cell or per-item `tone` field and none is coming.

**Spacing and signs in display strings — a house style, not a validated one.** The web picks
which fragments of a string to set in the monospace face from the shape of the string itself, so
how you write it changes how it reads. This applies to `kpi_row`'s `value` / `unit` / `delta`,
`metric_table`'s `value`, `table` cells, and the prose in the narrative fields.

- **A word unit takes one space between it and the number**: `+0.88 pp`, `2 bp`,
  `24,318.77 USDT`, `120 次`.
- **A symbol unit takes none and stays glued to the number**: `+1.82%`, `+0.62×`, `±20`.
- **U+2212 (`−`) is the preferred minus — a typographic preference, not a requirement.** In the
  monospace face U+2212 is the same width as `+`, so the positive and negative values in one
  column line up. An ASCII hyphen (`-`) behaves **identically** in every other way: the same run
  goes mono, and it takes the same semantic colour (the colour gate above already reads `−` and
  `-` alike). Emit whatever your program prints by default — **do not add a character-replacement
  pass for this**.
- **A numeric range reads best with an en dash**: `5–10` is treated as one numeric fragment,
  where `5-10` sets only the first half in mono.

**Ignoring this costs the typeface and nothing else.** A missing space leaves that run in the
regular face — the value is still correct, the colour is still correct, and the report is not
refused. `validate_report()` does not look at spacing, minus signs or dashes, and no api error
will ever name them. Write to it as a convention, not as a gate to clear.

## 4. Markdown subset (`text.markdown`)

Only these render; anything else shows up as plain text.

| Syntax | Renders as |
|---|---|
| `## Heading` | section heading with a hairline rule |
| `### Heading` | sub-heading |
| `**bold**` | bold (no colour change) — see the note below |
| `*italic*` | italic — for Chinese emphasis use bold instead |
| `- item` | bullet list |
| `1. item` | numbered list |
| backtick-wrapped text | inline code chip |
| `[^id]` | footnote reference — `id` **must** match an `items[].id` in the report's `footnote` block, or the report is refused |

Not supported: tables (use a `table` block), images (use `image`), links, H1, H4+,
block quotes (use `quote`), raw HTML.

**Bold: a matched pair of `**` is always bold.** CommonMark's flanking rules are *not*
applied — under those rules a closing `**` followed by fullwidth punctuation
(`**先觀察一週**。`) is not a closing delimiter and the asterisks print literally, which
would penalise ordinary Chinese sentences. The web pairs them up before rendering
(asterisks inside inline code and `code` blocks are left alone). **Do not reword a
sentence or move punctuation to make bold work** — write it the natural way.

## 5. Images

Charts belong in chart blocks — the web draws them from the data series, so they stay
readable and themed. The `image` block is for a figure that cannot be expressed as
data (a matplotlib research plot, an annotated diagram).

The report JSON never carries image bytes. There are two ways to point at them.

**The sidecar — the default, and the only one that works from a scheduled script.**
Put the file in `reports/<id>.files/` and name it from the block:

```
workspace/reports/
  mcpt-2317-20260901.json          {"type": "image", "file": "perm.png", "alt": "..."}
  mcpt-2317-20260901.files/
    perm.png
```

`lib/report.py` does this for you — pass `images={"perm.png": <bytes>}` to
`write_report` and it writes the sidecar before the JSON, which is the order the drop
dir requires (§1). The uploader then sends the bytes, hashes them and rewrites `file`
into the `sha256` the platform stores. **Use this whenever you can**: a strategy
subprocess is started with every `BLAVE_*` variable stripped, so a scheduled script
holds no machine token and cannot upload anything itself — and the long-tail research
figure produced on a schedule is exactly what this block exists for. The producer
needs files, nothing else.

**Uploading yourself.** From a context that does hold the machine token — or for a
picture already on the platform, such as a backtest chart — PUT the bytes and
reference the hash:

```
PUT https://api.blave.org/openclaw/agent/strategy_image/<sha256>
    Content-Type: image/png            # png / jpeg / webp / gif
    x-api-key: proxy-<machine token>
    body: the raw bytes
```

The path `<sha256>` must be the sha256 of exactly those bytes (a mismatch is refused),
which is what makes re-uploading an unchanged image a no-op. Then use
`{"type": "image", "sha256": "<same hash>", "alt": "..."}`. Never put `file` and
`sha256` on the same block — two references cannot both be the picture, and the
machine refuses the report rather than choose.

### When an image fails

Three outcomes, and which one you get depends on who made the mistake and whether
retrying would help:

| What happened | What the machine does |
|---|---|
| **You wrote it wrong** — no such file in `<id>.files/`, an extension that is not an image, empty or over 2 MB | The **whole report** goes to `failed/` with the reason in `upload_errors.log`. Nothing is degraded and nothing is guessed at: a report referencing a picture that does not exist is broken the same way an `[^id]` pointing at no footnote is, and you should hear about it now rather than ship a document with a hole in it. Fix the file, write the report again. |
| **Something transient** — the file cannot be read this tick (a lock, a virus scanner), the upload connection fails, the tick's time budget runs out | The report **stays in the drop dir** and is retried with backoff (60 s up to an hour). The image is **not** dropped: it will go up on a later tick, and losing a figure permanently to save a few minutes is a bad trade. Nothing for you to do. |
| **The image storage quota is full** (the api answers `507`) | That one `image` block is **removed and the report ships without it**, with a line in `upload_errors.log`. This is the only case that neither clears by retrying nor is your fault — holding the report back would mean it never arrives at all. Do not leave the user staring at a gap: the 507 is recorded on the machine and you should say in chat that a figure was left out because their image storage is full. |

**A `507` on the report channel is a different thing — do not treat the two alike.**
On the image channel it means the user's image storage is full and re-sending changes
nothing. On the report channel it means the old report that should have been evicted
could not be deleted, which does clear by itself, so it is retried like any other
transient failure. Same status code, different channel, opposite handling.

## 6. Structural rules worth re-reading before you write

1. `meta` exactly once, first block.
2. `text` with `variant: "lead"` at most once, immediately after `meta`.
3. `footnote` at most once, last block; every `[^id]` resolves to one of its items.
4. `line_chart` carries at most one `primary` series.
5. Every `table` row key is declared in `columns`.
6. `created_at` / `generated_at` / all chart `t` values: unix **seconds**, int, UTC.
7. An unknown block type or an unknown prop refuses the whole report — including a unit
   field on `bar_chart`/`drawdown`, `x_unit` on a `box`, `emphasis_cols` on a `matrix`
   heatmap, and `w`/`h` on an `image`.
8. In a `number`/`percent` `table` column or `metric_table` item, a loss-shaped figure
   (VaR, Max DD, worst loss) is written with a minus sign — the sign is the only thing the
   colour follows. Conversely, a signed value that is not P&L (`Net Exposure` `+0.62×`)
   is left as `text` so it stays neutral.
9. Every `image` block carries `file` **or** `sha256`, never both; a `file` exists in
   `reports/<id>.files/` and was written before the report JSON.

## 7. Content standards — the report has to say something

Everything above is format. A report can pass all of it — valid blocks, honest numbers,
every measurement basis footnoted — and still be a dashboard printed as prose: each
bullet reading "indicator X moved from A to B, which means C", where C is the same
number said again in words. This section is the bar for what goes **inside** the blocks.

**Scope — read off the envelope `type`.** Not every report carries a view, and forcing
one into a report that shouldn't have it is its own failure.

| `type` | What applies |
|---|---|
| `research`, `morning` | **All six rules.** These exist to answer "what do you think, and why". |
| `performance` | **Rules 5 and 6 only** (plus rule 2 on any sentence that explains *why* a number moved — stating the number itself is the point of the report and needs no thesis). A performance report is a state snapshot: numbers, attribution, what changed since last time. Do **not** invent an investment view to fill a section; the clean snapshot is the correct output. (The runtime writes the official daily/weekly ones itself, §1 — yours are ad-hoc extras.) |

### 1. One falsifiable claim, carried by the `lead`

The `text` block with `variant: "lead"` (§3) states **one claim that could have turned out
wrong** — a sentence that would read differently on a different day.

- **Filler:** "Sentiment is neutral-to-bullish; be careful chasing the move." True on almost
  any day. It describes the dashboard instead of reading it.
- **A claim:** "The bid is rotating from spot into leverage, and leverage is not crowded yet —
  so right now the risk of a pullback is smaller than the risk of missing the move."

The test is whether **a competent reader could disagree with the sentence**. If nobody could,
it is not a claim. Everything else in the report then supports it, qualifies it, or attacks it.

### 2. Every number is a cause or a comparison — the swap test

A figure earns its sentence only by driving a conclusion or by standing against something
(a prior period, a peer, a threshold, an expectation). To check a sentence you just wrote:
**swap the number for a plausibly different value. If the conclusion still stands, the number
was decoration and the sentence is restatement.**

- **Fails:** "Directional alpha is 0.18 against a 7-day mean of 0.12 — neutral-to-bullish, not
  yet euphoric." Put 0.05 in and the same words still get written.
- **Passes:** "Funding is positive across the board but tiny (BTC +0.0085%) — long positioning
  without crowded leverage." At +0.09% the sentence has to say the opposite.

### 3. Answer "so what"

Every section closes on the consequence for the reader: what it means for exposure, for
timing, or for which decision changes. A paragraph that ends on the observation is half a
paragraph. If you cannot name a consequence, the section is probably not worth a section.

### 4. Write the other side — mandatory

State **what would break the claim**: which indicator, in which direction, past roughly what
level, means the view in the `lead` is wrong and should be dropped. Name the level, not the
mood ("if funding goes above ~+0.05% per 8h the crowded-leverage read replaces this one", not
"if leverage gets extreme"). A `callout` with `tone: "warning"` is a good home for it.

This is the rule that adds the most depth, and it only works if you go looking for hostile
evidence **before** you write, not after. **If every figure in the report supports the thesis,
you selected the figures** — go back and pull the ones that argue against it.

### 5. No sentence that is true on any day

"Watch out for a pullback", "keep monitoring", "stay cautious", "the outlook remains
uncertain", "pay close attention to" — these carry no information and cost the reader's trust
in the sentences around them. Delete each one, or replace it with the threshold that would
make it actionable (rule 4).

### 6. Insufficient data is an answer — never manufacture conviction

Rules 1–5 raise the bar for the **argument**, never for how sure you sound. When the data on
hand does not support a judgment, the correct output is to say so — "the data is not sufficient
to judge X" — plus what would be needed to judge it. That is a complete answer and a report may
contain several.

Never invent a mechanism to explain a number you have not verified, never present an inference
as an observation, and never firm up a hedge to make the report read stronger. A confident
sentence with nothing under it is a worse failure than a shallow one: the shallow report wastes
the reader's time, the fabricated one loses them money. Every figure stays real or labelled.

## 8. Scheduled reports — a job directory, not a cron line

A recurring report is **one directory plus one registration file**. You write the script and
the registration; the runtime owns the schedule (it installs, pauses and removes the crontab
line / scheduled task itself), runs the script, records every run and reports the list to the
web, where the user can pause, resume, run now and delete without you. **Never touch crontab
or schtasks for a report.** `lib.report.register_schedule` writes both files correctly:

```python
from lib.report import register_schedule, list_schedules, remove_schedule

register_schedule(
    "perf-4h",                                   # id: [a-z0-9][a-z0-9-]{0,39}, a slug
    "每 4 小時運行狀況",                          # title, 1–80
    "每 4 小時給我一份各策略運行狀況：倉位、當日損益、最近訊號、有沒有錯誤。",  # the user's words, verbatim
    "0 */4 * * *",                               # cron, 5 fields, this machine's local time
    "每 4 小時",                                  # the schedule in words — the only form the user sees
    script,                                      # full text of run.py
)
list_schedules()        # every job + its last run — for 「我有哪些定期報告」
remove_schedule("perf-4h")   # when the user asks you in chat to delete one
```

```
workspace/report_jobs/<id>/
  job.json      the registration — exists = registered, deleted = cancelled
  run.py        your script
  runs.jsonl    written by the runtime: one line per run, read-only for you
  run.log       stdout + stderr of the last run, read-only for you
```

`job.json` (what `register_schedule` writes; the file is the contract, the helper is a
convenience):

```json
{"id": "perf-4h", "title": "每 4 小時運行狀況",
 "prompt": "每 4 小時給我一份各策略運行狀況：倉位、當日損益、最近訊號、有沒有錯誤。",
 "schedule": {"human": "每 4 小時", "cron": "0 */4 * * *"},
 "enabled": true, "created_at": 1756800000, "updated_at": 1756800000, "pending": null}
```

- Same id again = update. `register_schedule` keeps `created_at`, bumps `updated_at` and
  sets `pending` back to `null` — that is how the web learns an edit has landed, so always
  re-register through it rather than editing the file by hand. At most 20 jobs per machine.
- `prompt` is the user's own request, not your rewrite; the web shows it back as the
  report's description and hands it to you again when they edit it.
- `schedule.cron` is standard 5-field cron in the machine's local time — no `@daily`,
  no seconds field, no month/weekday names. The web never displays the cron; it displays
  `schedule.human` and the next run time the runtime computes from the cron, which is how a
  mis-parse becomes visible — so restate the schedule when you register it (AGENTS.md).
- **Windows machines** only run this subset: `*/N * * * *` with N in
  1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30; `M */N * * *` with N in 1, 2, 3, 4, 6, 8, 12 (steps
  that divide the hour / day — for any other N the task scheduler counts from creation time
  and the next-run time shown to the user would be wrong); `M H * * *`; `M H * * D` (one
  weekday digit); `M H D * *`. Anything else (`1-5` weekday ranges, `9,18` lists, `*/7`) is
  not installed and shows as an error in the user's list — split it into several jobs or
  pick the nearest expressible schedule and say so.

`run.py` constraints — it runs exactly like a scheduled strategy:

- cwd is the workspace; `lib/` imports work as usual.
- Every `BLAVE_*` environment variable is stripped: no machine token, no direct API
  call to the platform. A report reaches the platform only by landing in `reports/` —
  `write_report(...)` or a template `publish(pack)` (§1b), with pictures in the sidecar (§5).
- Write nothing when there is nothing to report. Exit 0 with no new `reports/*.json` is
  recorded as `skipped`, which is the correct outcome for a signal-only job; a non-zero
  exit or a run over 600 s is `failed` (the tail of `run.log` shows in the web, and the
  usual failure alert fires). Do not script a fixed judgement into it — a scheduled run
  is data-only (§1b).
- Use date-stamped report ids (`perf-20260902-0800`) unless the re-run really should
  overwrite the previous report.

When the user edits a job from the web you receive 「請修改定期報告「{title}」（id：{id}）。
新的描述：「…」。新的週期：「…」…」: change `run.py` and/or the schedule accordingly, call
`register_schedule` again with the same id, and restate the parsed schedule. Finish it in
that turn — the web shows a waiting state until the re-registration lands.
