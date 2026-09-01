# Reports — publishing a rendered report to the workspace

A **report** is a JSON document this machine writes and the platform renders in the
web workspace sidebar: KPI rows, charts, tables and prose, laid out by the web from
structured data — not a screenshot, not a wall of Telegram text. Use it for anything
the user will want to read again later: a performance review, a morning briefing on a
watchlist, an MCPT / research write-up, a post-mortem of a live week.

The platform pushes a short summary notification once the report is stored, so the
report reaches the user even when this machine is asleep — never send your own
Telegram message about a report as well, that duplicates every alert.

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
