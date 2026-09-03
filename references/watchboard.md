# Watchboard — widgets on the user's board

The **watchboard** is the tile grid in the web workspace: live Taiwan quotes (stream
widgets) and small machine-computed panels (machine widgets), laid out by the user with
drag-and-drop. You create and feed widgets; the platform owns the board and the user owns
the layout. It is for **now** — one number, one chart, one small table the user glances at.
Anything they will read again later is a report (`references/reports.md`), not a widget.

Widgets are **declarative JSON, not code**: you say which catalogue type and which
parameters, the browser draws it with its own components. No HTML, no JS, no iframes.

## 1. How it works — the drop directory

The machine only writes files; the runtime's uploader ships them. **The write is the
finish line** — say the widget is created and reply; never wait for the upload.

```
workspace/watch/
  ops/<epoch_ms>-<op>.json     one board operation (add / update / remove); after upload the
                               runtime moves it to ops/sent/ (accepted) or ops/failed/ (refused)
  data/<widget_id>.json        the current content of one machine widget — overwritten on every
                               refresh, uploaded in place; the board keeps no history
  data/<widget_id>.files/      picture sidecar for an `image` block (same rule as reports §5)
  upload_errors.log            one line per refusal, with the api's reason
workspace/report_jobs/<widget_id>/
  job.json                     the refresh schedule for a machine widget, `"kind": "watch"`;
  run.py                       the script the runtime runs on that schedule (report-schedules §8)
```

- Files are written atomically (`.tmp` then `os.replace`); the scan ignores anything not
  ending in `.json`.
- Ops are applied in file-name order. `add` needs the whole widget (position optional —
  the platform appends the tile to the bottom row); `update` carries only `title`,
  `props` and `source.refresh` — **never `grid`**, the layout belongs to the user;
  `remove` also deletes the platform's stored data for that widget.
- A data file is one report block wrapped in a small envelope, **≤ 64 KB**. The api checks
  it with the same validator reports use; a mismatch between the block sent and the
  `block_type` the widget declared is refused and the tile shows a type-mismatch notice.
- The runtime reads `job.json` and installs the crontab line / scheduled task itself, exactly
  as for a scheduled report (`references/reports.md` §8 — including the Windows subset).
  After each run it only looks at whether `watch/data/<widget_id>.json` changed.

`lib/watch.py` writes all of it correctly; the directory is the contract, the module is a
convenience.

```python
from lib.watch import add_widget, update_widget, remove_widget, write_data, status

add_widget(id, type, title, *, symbol=None, symbols=None, interval=None, venue=None,
           block_type=None, refresh_cron=None, refresh_human=None, script=None, w=None, h=None)
update_widget(id, *, title=None, props=None, refresh_cron=None, refresh_human=None, script=None)
remove_widget(id)
write_data(widget_id, block, images=None)     # inside run.py — the only publish call there
status(op_file_or_widget_id)                  # pending | sent | failed: <reason> | unknown
```

Every check is done up front and raises `ValueError` with the rule that was broken —
catalogue type, kind/type binding, symbol shape, venue, watchlist size, interval, block type,
cron grammar, minimum size, title length, id shape, a machine id already taken by a
scheduled report. Fix the call; nothing was written.

### The files themselves

```json
{"schema_version": "1.0", "op": "add",
 "widget": {"id": "risk", "type": "block", "title": "持倉風險",
            "grid": {"w": 6, "h": 3},
            "source": {"kind": "machine", "refresh": {"human": "每 5 分鐘", "cron": "*/5 * * * *"}},
            "props": {"block_type": "kpi_row"}, "created_by": "agent", "updated_at": 1725256325}}
{"schema_version": "1.0", "op": "update", "id": "risk",
 "patch": {"title": "持倉風險(實盤)", "source": {"refresh": {"human": "每 10 分鐘", "cron": "*/10 * * * *"}}}}
{"schema_version": "1.0", "op": "remove", "id": "risk"}
```

```json
{"schema_version": "1.0", "widget_id": "risk", "generated_at": 1725256325,
 "block": {"type": "kpi_row", "items": [{"label": "淨曝險", "value": "+0.62×", "tone": "neutral"}]}}
```

## 2. Catalogue

| type | kind | min w×h | props | what it shows |
|---|---|---|---|---|
| `price` | stream | 2×2 (default 4×2) | none | last price, change vs. previous close / settlement (crypto: previous UTC day's close), day high-low, timestamp; futures show the session. Taiwan symbols, or crypto with `venue="binance"` |
| `kline` | stream | 4×3 (default 6×3) | `interval`: `1m` `5m` `15m` `60m` `1d` | candlestick chart; history from the platform, the forming bar from ticks (Taiwan) or the Binance kline stream (crypto with `venue="binance"`) |
| `book` | stream | 2×3 (default 3×3) | none | 5-level order book — Taiwan futures `TXF` / `MXF`, or a crypto perpetual with `venue="binance"`. Stocks have no book |
| `watchlist` | stream | 3×2 (default 4×3) | none; `symbols` 1–20 | table of symbol, last, change %, time. One watchlist beats three `price` tiles: same information, fewer cells, fewer stream slots |
| `block` | machine | 2×2; chart-like `block_type` 4×3 | `block_type` | one report block, drawn by the report renderer from your `write_data` payload. Chart-like = `line_chart` `drawdown` `heatmap` `bar_chart` `histogram` `box` `scatter` `image` (min 4×3, default 6×3); `kpi_row` and `table` default 6×3, `callout` 4×2, the rest stay 2×2 |

**The minimum is a floor, not a default.** Below it the card cannot draw its content; the
defaults above are the sizes that stay readable on a 1440-wide screen with the chat pane open
(canvas ≈ 750px). Omit `w`/`h` and you get the default — don't pass the minimum by hand.

**`text`, `quote` and `code` are not watchboard cards.** They exist in the block catalogue
because the report renderer is shared, but prose in a tile is a report squeezed into a grid
cell. If the answer is a paragraph, write a report. A tile answers with a number, a table or
a chart.

- **Ids**: `[A-Za-z0-9_-]{1,32}`, unique on the board, yours to pick. A machine widget's id is
  also its `report_jobs/` directory, so it must be a slug `[a-z0-9][a-z0-9-]{0,31}`.
- **Symbols** (stream): a stock id (4–8 letters/digits, e.g. `2330`, `00878`), the index
  `TAIEX`, or the futures `TXF` / `MXF`. `TMF` is not streamed — use `MXF` for the price.
- **Crypto** (`price` / `kline` / `book`): add `venue="binance"` and give `symbol` as a Binance USD-M
  perpetual — `BTC`, `BTCUSDT` or `BTC/USDT` all work; it is written uppercased with `/` removed and
  the api normalises from there. `lib/watch.py` checks the shape only (3–20 letters/digits, one
  optional `/`), never a list of what Binance trades. Each crypto widget is its own connection in
  the browser, so keep to **at most 6 crypto widgets on a board**. `venue` on `watchlist` / `block` is
  refused — a watchlist is Taiwan-only and a block is a machine widget.
- **Sizes**: 12-column grid, `w` 1–12, `h` 1–12, at least the type's minimum (for a `block`, the
  minimum follows its `block_type`); omit `w`/`h` for the minimum. Position is never yours: the
  platform places new tiles, the user moves them.
- **Board limits** (the api refuses beyond them): 24 widgets, 20 distinct stream symbols across
  the board, 12 machine widgets. `title` 1–40 characters.
- **`block_type`**: any content block from `references/reports.md` §3 — `kpi_row`, `line_chart`,
  `drawdown`, `heatmap`, `bar_chart`, `histogram`, `box`, `scatter`, `metric_table`, `table`,
  `text`, `quote`, `code`, `callout`, `image`. Not `meta` / `footnote` / `divider` (report
  structure, not tile content). The block you later `write_data` must be that type.
- **Refresh** (machine): `refresh_cron` is 5-field cron in this machine's local time, **at most
  once a minute** (`*/1 * * * *`), no seconds field, no `@hourly`; `refresh_human` is the
  schedule in words and is the only form the user sees, so make it match exactly. Restate
  the parsed schedule to the user, as for a scheduled report. `lib/watch.py` checks only the
  cron grammar — on a Windows machine keep to the subset in `references/reports.md` §8
  (hourly is `0 */1 * * *`; the bare `0 * * * *` is not in it and is not installed).

Which to pick: a number that should move as the market moves → stream widget, always. A
number that comes from **your** computation (exposure, a signal, a scan) → machine widget on
the slowest cadence that still answers the question.

**For a crypto K-line use the `kline` widget with `venue="binance"`, never a `block` +
`line_chart` drawn by a script.** The live candles come from the platform's Binance stream; a
script cannot refresh faster than once a minute and would draw a line, not candles.

## 3. Machine widget scripts

**`lib` must be importable from `run.py`.** The runtime runs a job as
`python3 report_jobs/<id>/run.py` from the workspace, and Python puts the script's own
directory on `sys.path`, not the workspace — a bare `from lib.watch import write_data` fails
with `ModuleNotFoundError`. `add_widget(script=…)` pins the workspace for you (it prepends
`sys.path.insert(0, os.getcwd())` unless your script already does). If you write `run.py`
by hand, add those two lines yourself — the examples below assume `add_widget`.

`run.py` runs like a scheduled strategy: cwd = workspace, every `BLAVE_*` variable stripped
(no machine token), 600 s timeout, on a Starter machine of 2 vCPU / 4 GB shared with the
user's live strategies. It is **deterministic code — never an LLM call, never a chat turn**,
and the schedule is the only loop: no `while True`, no `sleep`, no polling quotes inside the
script. If something has to move by the second it is a stream widget, not a script.

```python
# report_jobs/<widget_id>/run.py — pass this text as add_widget(script=...)
from lib.data import ...                      # every fetch goes through lib/ (cached, chunked)
from lib.report_templates import headers_from_env
from lib.watch import write_data

hdrs = headers_from_env()                     # reads the workspace .env; no BLAVE_* needed
# 1. fetch the smallest window that answers the question (a few bars, one quote batch)
# 2. compute — pure functions of that data; no randomness, no state kept between runs
# 3. build ONE block of the declared type, display values formatted as strings,
#    chart values as numbers, nothing NaN, well under 64 KB
# 4. publish it — and publish nothing when the data is not there yet (pre-market, a
#    holiday, an empty fetch): the tile keeps showing the previous value with its time
write_data("<widget_id>", block)
```

Rules of thumb: fetch through `lib/data.py` only (`references/lib.md`) — its caches are
what make an every-few-minutes script cheap; keep the fetch window to what the block
needs (a 5-minute KPI does not need 90 days of history every run); build the block with
`lib.report_templates.kpi`, `kpi_row`, `table`, `line_chart` when they fit (shape by
construction); and prefer `*/5` or `*/15` over `*/1` unless the user asked for every
minute. Windows machines run only the cron subset in `references/reports.md` §8.

`write_data` never touches the network. It checks the block's type against what the widget
declared, refuses an obviously wrong shape (missing required prop, NaN, an `image` block
with both `file` and `sha256`, a path instead of a file name — an empty `table.rows` is
fine, "no movers today" is a legal table, but `columns` must be there) and the 64 KB limit, writes
any picture sidecar first, then the JSON atomically. Pictures: `images={"fig.png": bytes}`
with `{"type": "image", "file": "fig.png", "alt": "..."}` — the same sidecar rule as reports.

Changing an existing widget: `update_widget(id, title=..., refresh_cron=..., refresh_human=...)`
sends the op **and** rewrites `job.json` on this machine; `update_widget(id, script=...)`
only rewrites `run.py` (no op — the next run uses it). `update_widget(id, props={"block_type": ...})`
to a chart-like type is not size-checked on this machine (there is no view of the board): if the
tile is smaller than 4×3 the api refuses the op and `status` shows `failed` — ask the user to
resize the tile first, then send the update again. `remove_widget(id)` sends the op and
deletes the widget's job directory, the data file and its sidecar, so nothing is left to upload
for a tile that no longer exists. A widget id and a scheduled report never share
`report_jobs/<id>/`: `add_widget` refuses an id that is already a report, and `remove_widget`
only deletes a job tagged `"kind": "watch"`. Never touch crontab / schtasks yourself.

## 4. When something is wrong

| What you see | What it means |
|---|---|
| `status(id)` → `pending` for a long time | Not an error by itself. On a machine whose runtime predates the watchboard the files are never picked up: the op stays in `ops/`, the tile (if the user added one) shows "waiting for first output". Ops and data files stay `pending` until the runtime updates — nothing to fix or re-send on your side. Say so; the runtime updates itself. |
| `status(id)` → `failed: …` | The api refused the op; the line quoted is from `watch/upload_errors.log` and names the field. Fix and send a fresh op — the failed file is never retried. |
| the tile shows a content-type mismatch | The block written does not match the widget's `block_type`. `write_data` catches this when the job directory is on this machine; otherwise re-add the widget with the right type. |
| the tile shows "stale" | `generated_at` is older than 3× the refresh period: the script is failing or skipping. Read `report_jobs/<id>/run.log` and `runs.jsonl` (the runtime's, read-only) — a non-zero exit or a run over 600 s is `failed`, exit 0 with no data change is `skipped`. |
| data file sits in `watch/data/` | Normal — it is overwritten in place and uploaded as-is. Only ops move to `sent/` / `failed/`. |

`status` is a diagnostic for afterwards; never poll it after a write. `status("<op file>")`
checks one op by the name `add_widget` / `update_widget` / `remove_widget` returned;
`status("<widget id>")` checks the newest op that mentions that widget.

## 5. Worked examples

**Which one to reach for.** A board is read top-down as "what needs me now → what happened to my
money → what is the market doing → detail". When the user has not said what they want, offer them
in this order — and stop at four or five cards. A crowded board is read by nobody.

1. **A price or watchlist tile** — the one big number, zero setup, ticks by the second.
2. **Strategy health** (`table`) — the only card that tells them something *broke*. Reads local
   state, costs nothing.
3. **A scanner** (`table`) — the largest single use we see on real machines, and it replaces the
   hourly push notification people complain about.
4. **Exposure** (`kpi_row`) — reads local state, costs nothing.
5. **A chart** (`kline`) — confirmation, not discovery, so it comes after.
6. **Threshold distance** (`kpi_row`) — needs the user to name a level, so you cannot offer it
   silently, but ask: it is the one thing people hand-roll most and we cover least.

### A TXF price tile and its 5-minute chart (stream — no script)

```python
from lib.watch import add_widget

add_widget("txf", "price", "台指期", symbol="TXF")                      # 4×2 by default, appended to the board
add_widget("txf-k5", "kline", "台指期 5 分 K", symbol="TXF", interval="5m", w=8, h=4)
```

Two ops, no job directory, nothing to schedule — the browser subscribes to the quote stream
itself. Tell the user both tiles are on the board and can be dragged into place.

### BTC hourly candles (stream, crypto — no script)

```python
from lib.watch import add_widget

add_widget("btc-1h", "kline", "BTC 1H", symbol="BTC", venue="binance", interval="60m")   # 6×3 by default
```

One op with `"source": {"kind": "stream", "venue": "binance", "symbol": "BTC"}`; the api resolves
it to the `BTCUSDT` perpetual and the tile draws live candles from the Binance stream, updating by
the second. This is the whole answer to "give me a BTC chart" — do **not** write a script that
fetches klines through `lib.data` and publishes a `line_chart` block: that is a machine widget
refreshing at most once a minute, and it draws a line, not candles. Same for a crypto price:
`add_widget("btc", "price", "BTC", symbol="BTC", venue="binance")`.

### A `kpi_row` exposure widget refreshed every 5 minutes (machine)

```python
from lib.watch import add_widget

script = '''
from lib.portfolio import aggregate_portfolio
from lib.watch import write_data

target = aggregate_portfolio()          # {symbol: {side, size, exchange, ...}}, account currency
signed = {s: (v["size"] if v["side"] == "long" else -v["size"])
          for s, v in target.items() if v["side"]}
net, gross = sum(signed.values()), sum(abs(x) for x in signed.values())
longs = sum(1 for x in signed.values() if x > 0)
shorts = sum(1 for x in signed.values() if x < 0)
write_data("exposure", {"type": "kpi_row", "items": [
    {"label": "淨曝險", "value": f"{net:+,.0f}", "tone": "pos" if net > 0 else "neg" if net < 0 else "neutral"},
    {"label": "總曝險", "value": f"{gross:,.0f}", "tone": "neutral"},
    {"label": "多 / 空 標的", "value": f"{longs} / {shorts}", "tone": "neutral"},
]})
'''
add_widget("exposure", "block", "持倉曝險", block_type="kpi_row",
           refresh_cron="*/5 * * * *", refresh_human="每 5 分鐘", script=script, w=6, h=2)
```

Reads only local strategy state through `lib.portfolio` — no fetch at all, so `*/5` costs
nothing. `net` is signed, so its tone follows the sign; the count is unsigned and stays
`neutral`. Restate "每 5 分鐘" to the user before you move on.

### A `table` scanner widget, hourly (machine)

```python
from lib.watch import add_widget

script = '''
from lib.data import fetch_twstock_market_value_all, fetch_twstock_quote_batch
from lib.report_templates import headers_from_env
from lib.watch import write_data

hdrs = headers_from_env()
pool = fetch_twstock_market_value_all(hdrs, top=60)                 # cached 1 h locally
ids = [s for s in pool["stock_id"] if not s.startswith("00")][:50]  # drop ETFs; batch max 50
names = dict(zip(pool["stock_id"], pool["name"]))
quotes = fetch_twstock_quote_batch(ids, hdrs)                       # one call, ~10 s snapshot
rows = [{"id": s, "name": names.get(s, ""), "last": f"{q['close']:,.1f}",
         "chg": f"{q['change_rate']:+.2f}%", "vol": f"{q['total_volume']:,}"}
        for s, q in quotes.items() if q.get("close") and q.get("change_rate") is not None]
rows.sort(key=lambda r: abs(float(r["chg"][:-1])), reverse=True)
if rows:                                                             # pre-market: keep the old tile
    write_data("movers", {"type": "table", "columns": [
        {"key": "id", "label": "代號", "align": "left"},
        {"key": "name", "label": "名稱", "align": "left"},
        {"key": "last", "label": "最新", "align": "right", "format": "number"},
        {"key": "chg", "label": "漲跌幅", "align": "right", "format": "percent"},
        {"key": "vol", "label": "成交量", "align": "right", "format": "number"}],
        "rows": rows[:10]})
'''
add_widget("movers", "block", "權值股強弱前十", block_type="table",
           refresh_cron="0 */1 * * *", refresh_human="每小時", script=script, w=6, h=4)
```

Two lib calls per run, ten rows out, `percent` format so the sign colours the change column.
Hourly is written `0 */1 * * *`: the plain `0 * * * *` is not in the Windows subset
(`references/reports.md` §8) and would not be installed there. `0 9-14 * * 1-5` would fit
trading hours better on Linux but is outside that subset too — pick the cadence for the
machine you are on and say which you chose.

### A `table` strategy-health widget every 15 minutes (machine)

```python
from lib.watch import add_widget

script = '''
import os, time
from datetime import datetime
from lib.portfolio import load_all_states
from lib.report_templates import TPE          # 台北時區:機器的系統時鐘是 UTC
from lib.watch import write_data

states = load_all_states()                      # {name: state_dict}; only strategies that have run
names = sorted(d for d in os.listdir("strategies")            # skip templates, the runner's own
               if os.path.isdir(os.path.join("strategies", d))  # report-* work dirs, __pycache__
               and not d.startswith(("TEMPLATE", "report-", "_", ".")))
rows = []
for name in names:                              # every strategy, not just the ones with state:
    st = states.get(name) or {}                 # a strategy that never ran shows "—", not nothing
    path = f"strategies/{name}/state.json"
    try:                                         # live runs rewrite state every bar,
        mt = os.path.getmtime(path)              # so mtime = last successful run
        age = time.time() - mt
        seen = datetime.fromtimestamp(mt, TPE).strftime("%m/%d %H:%M")
    except OSError:
        age, seen = None, "—"                    # never fake a time we do not have
    pos = st.get("position")
    rows.append({"s": name, "t": seen,
                 "p": "—" if pos is None else f"{pos:+g}",
                 "st": "—" if age is None else ("停更" if age > 3 * 3600 else "正常")})
if rows:
    write_data("strat-health", {"type": "table", "columns": [
        {"key": "s", "label": "策略", "align": "left"},
        {"key": "t", "label": "最後執行", "align": "left", "format": "date"},
        {"key": "p", "label": "部位", "align": "right", "format": "number"},
        {"key": "st", "label": "狀態", "align": "left"}],
        "rows": rows})
'''
add_widget("strat-health", "block", "策略運行狀況", block_type="table",
           refresh_cron="*/15 * * * *", refresh_human="每 15 分鐘", script=script)
```

Zero fetches — it lists `strategies/*/` and reads each `state.json` relative to the workspace root (the runner
executes jobs with `cwd` = workspace, so the glob resolves) on the machine, so it costs no API quota and
works when the network is down. It is the one card that says something *broke*, which is why it
is worth offering early.

**Where the timestamp comes from, and when it lies:** `lib/runner.py` saves state on every bar in
live mode, so the file's mtime is the last successful run. That holds for strategies driven by
`lib.runner.run`. A hand-written runner that only saves on a position change — or writes state
somewhere else — will look stale when it is fine. If the user's strategies are hand-rolled, say so
and leave the column blank rather than printing a time that means something else.

### A `kpi_row` threshold-distance monitor (machine)

The most hand-rolled thing on real machines, and the one this catalogue covered worst. The user
names the levels; you write them into the script.

```python
from lib.watch import add_widget

script = '''
from lib.data import fetch_twstock_quote_batch
from lib.report_templates import headers_from_env
from lib.watch import write_data

LEVELS = {"2330": ("跌破", 2300.0), "2317": ("突破", 235.0), "2454": ("跌破", 4000.0)}

hdrs = headers_from_env()
quotes = fetch_twstock_quote_batch(list(LEVELS), hdrs)     # one call, ~10 s snapshot
items = []
for sid, (side, level) in LEVELS.items():
    q = quotes.get(sid) or {}
    px = q.get("close")
    if not px:
        continue
    gap = (px - level) / level * 100 if side == "突破" else (level - px) / level * 100
    hit = gap >= 0                                          # +ve = past the level, −ve = short of it
    items.append((not hit, abs(gap),                        # triggered first, then nearest
                  {"label": f"{sid} {side} {level:,.0f}",
                   "value": ("已觸發" if hit else f"{abs(gap):.2f}%"),
                   "tone": "pos" if hit else "neutral"}))
items.sort(key=lambda t: t[:2])                             # first cell renders large
if items:
    write_data("levels", {"type": "kpi_row", "items": [t[2] for t in items[:4]]})
'''
add_widget("levels", "block", "價位監控", block_type="kpi_row",
           refresh_cron="*/5 * * * *", refresh_human="每 5 分鐘", script=script)
```

The sort is what makes the card useful: anything triggered goes first, then the nearest level —
otherwise the big first cell shows whichever level the user happened to type first.
`kpi_row` defaults to 6×3 here, and it must: four cells fold to two rows on a narrow canvas and
`h=2` hides the bottom row's values entirely. Four levels is the cap — the first cell renders
large, so put the one that matters there (the sort does that once something triggers).

**Say what this is, honestly.** A machine widget runs at most once a minute, so the distance can
be up to a minute behind. That is fine for "am I near my level"; it is not a trigger, and it is
not the same as the price tile next to it, which ticks by the second. If the user wants to be
*told* when it fires, that is an alert (Telegram from a strategy or a cron script), not a tile —
a tile only shows.

**Signal variant, same shape.** Watching an indicator instead of a price: fetch bars with
`lib.data.fetch_twstock_ohlcv(sid, "1d", hdrs)` (or `fetch_kline` for crypto), compute the
indicator, and put "指標值 vs 門檻" in `value`. Everything else is identical.
