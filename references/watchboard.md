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
  `props` (a shallow merge: only the keys sent change) and `source.refresh` — **never `grid`**,
  the layout belongs to the user;
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

add_widget(id, type, title, *, symbol=None, symbols=None, interval=None, venue=None, levels=None,
           panes=None, block_type=None, refresh_cron=None, refresh_human=None, script=None,
           w=None, h=None)
update_widget(id, *, title=None, props=None, levels=None, panes=None, refresh_cron=None,
              refresh_human=None, script=None)
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
| `kline` | stream | 4×3 (default 6×3, 6×4 with one indicator pane, 6×5 with two) | `interval`: `1m` `5m` `15m` `60m` `1d`; `levels`: 0–4 `{price, side, label?}` — the price monitor, drawn as lines on the chart (§5); `panes`: 0–2 `{id}` — indicator sub-panes under the candles, **crypto only**, `id` is the indicator's slug (`holder_concentration`), not a number (§5) | candlestick chart; history from the platform, the forming bar from ticks (Taiwan) or the Binance kline stream (crypto with `venue="binance"`) |
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
  minimum follows its `block_type`); omit `w`/`h` for the default. Position is never yours: the
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
script cannot refresh faster than once a minute and would draw a line, not candles. The same
goes for a **crypto indicator on that chart**: it is `panes` on the `kline` widget (§5), not a
scheduled script publishing a `line_chart` block.

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
2. **Price levels on a K-line** (`kline` + `levels`) — needs the user to name a level, so you
   cannot offer it silently, but ask the user for the price: it is the one
   thing people hand-roll most, and it is now zero-script, second-level and free.
3. **Strategy health** (`table`) — the only card that tells them something *broke*. Reads local
   state, costs nothing.
4. **A scanner** (`table`) — the largest single use we see on real machines, and it replaces the
   hourly push notification people complain about.
5. **Funds and exposure** (`kpi_row`) — reads local state; one account read on top when a venue
   is bound, and three honest cells when it is not.
6. **Positions** (`table`) — the same band one level down: what the account actually holds, read
   from the reconcile snapshot. Zero fetch, no keys.
7. **Taiwan market temperature** (`kpi_row`) — one run a day, after-hours numbers; the intraday
   index is a `price` tile, not this card.
8. **A chart** (`kline`) without levels — confirmation, not discovery, so it comes after. For
   crypto it can carry one or two indicator sub-panes (`panes`), also script-free.

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
    {"label": "淨曝險", "value": f"{net:+,.0f}", "tone": "neutral"},   # direction, not profit
    {"label": "總曝險", "value": f"{gross:,.0f}", "tone": "neutral"},
    {"label": "多 / 空 標的", "value": f"{longs} / {shorts}", "tone": "neutral"},
]})
'''
add_widget("exposure", "block", "持倉曝險", block_type="kpi_row",
           refresh_cron="*/5 * * * *", refresh_human="每 5 分鐘", script=script, w=6, h=2)
```

Reads only local strategy state through `lib.portfolio` — no fetch at all, so `*/5` costs
nothing. `net` carries a sign but is **direction, not profit** — it stays `neutral`, the same rule
the report catalogue applies to `Net Exposure` in a table (a green +0.62× reads as "up 0.62%").
Only realised or unrealised money takes `pos`/`neg`; the count is unsigned and stays
`neutral`. Restate "每 5 分鐘" to the user before you move on.

### The same card with the account's equity — 資金與曝險 (machine)

```python
from lib.watch import add_widget

script = '''
import importlib
from lib.portfolio import aggregate_portfolio
from lib.report_templates import kpi, kpi_row
from lib.venue_wiring import read_env, detect_venue
from lib.watch import write_data

target = aggregate_portfolio()
signed = {s: (v["size"] if v["side"] == "long" else -v["size"])
          for s, v in target.items() if v["side"]}
net, gross = sum(signed.values()), sum(abs(x) for x in signed.values())
longs = sum(1 for x in signed.values() if x > 0)
shorts = sum(1 for x in signed.values() if x < 0)

eq = None                              # 沒綁 venue、金鑰失效、交易所不通 → 少一格,不是壞掉
env = read_env()
vid = detect_venue(env)                # 官方 lib 齊全的交易所才認得;台灣券商一律 None
if vid:
    try:
        eq = importlib.import_module("lib.account_" + vid).get_equity(env)
    except Exception:
        eq = None

items = []
if eq:
    items.append(kpi("帳戶權益", f"{eq['equity']:,.2f}", "neutral", unit=eq.get("currency")))
items.append(kpi("淨曝險", f"{net:+,.0f}", "neutral"))   # direction, not profit
items.append(kpi("總曝險", f"{gross:,.0f}", "neutral",
                 delta=(f"{gross / eq['equity']:.2f}× 權益"
                        if eq and eq["equity"] > 0 else None)))
items.append(kpi("多 / 空 標的", f"{longs} / {shorts}", "neutral"))
write_data("funds", kpi_row(items))
'''
add_widget("funds", "block", "資金與曝險", block_type="kpi_row",
           refresh_cron="*/5 * * * *", refresh_human="每 5 分鐘", script=script)
```

The exposure card above with the money in front of it. **This one or that one, not both** — same
numbers, one extra cell. No `w`/`h` here: four cells take the `kpi_row` default 6×3, and the 6×2
above is sized for three (at four the row folds to 2+2 on a narrow canvas and `h=2` clips the
values).

Equity is the only line that leaves the machine: one signed account read per run through
`lib/account_<venue>.py`, which `lib.venue_wiring.detect_venue` picks from the keys in `.env`.
**When it is not there the card is three cells, never a 0 and never an error string** — the
exposure half is pure local arithmetic and is not broken by a missing key. `detect_venue` routes
only venues that ship both `lib/account_<id>.py` and `lib/order_<id>.py`, and it deliberately
skips the Taiwan brokers (`sinopac`, `president`, `capital`), so a 群益 machine gets the three-cell
form even though `lib/account_capital.py` does have `get_equity` (it reads the worker's snapshot,
no network) — call that module by name if the user asks for equity there, and say why it is wired
by hand.

**今日損益 is not a cell, and do not invent one.** It needs yesterday's equity; the machine keeps
none. `get_flows(env, since)` is on-chain deposits and withdrawals, not PnL — the platform is what
nets flows out of its own equity-snapshot series — and a widget script must not carry state between
runs (§3). Offer 權益 / 淨曝險 / 總曝險 and say the day's PnL needs an equity history the machine
does not have.

### A `table` positions widget every 5 minutes (machine)

```python
from lib.watch import add_widget

script = '''
import json
from datetime import datetime, timezone
from lib.portfolio import aggregate_portfolio, split_key
from lib.report_templates import TPE
from lib.watch import write_data

try:                                    # 對帳過才有;讀不到就整欄「—」,不拿目標冒充實際
    with open("manager/last_reconcile.json") as f:
        snap = json.load(f)
except (OSError, ValueError):
    snap = {}
actual = snap.get("actual") or {}       # {key: {"side", "size"}},那一輪交易所真的回報的部位
# 同一輪的目標;沒快照(或機器上還是舊格式的快照)才自己算
target = snap.get("target") or aggregate_portfolio()

def cell(key, row):
    if not row or not row.get("side"):
        return "—"                      # 方向要進每一格:兩側方向可能相反(策略剛翻倉、
                                        # HALT 開著、下單一直失敗),只印一個方向會把
                                        # 「多 1000 / 空 1000」畫成已經對齊的樣子
    t = target.get(key) or {}           # 口數 vs 金額:compute_diff 的 is_lot_based 同一條判斷
    lots = ((t.get("asset_spec") or {}).get("type") == "futures_contracts"
            or t.get("exchange") == "capital")
    size = abs(float(row.get("size") or 0))
    sign = "空 " if row.get("side") == "short" else "多 "
    return sign + (f"{size:,.0f} 口" if lots else f"{size:,.0f}")

rows = []
for key in sorted(set(target) | set(actual)):
    a, t = actual.get(key) or {}, target.get(key) or {}
    if not a.get("side") and not t.get("side"):           # 兩邊都平的殘留 key 不佔一列
        continue
    sym, market = split_key(key)                          # BTCUSDT@spot → ("BTCUSDT", "spot")
    rows.append({"s": sym + ("(現貨)" if market == "spot" else ""),
                 "a": cell(key, a), "t": cell(key, t)})

ts = snap.get("ts")                     # 現在是 utcnow().isoformat();老機器上還有 epoch 秒的舊檔
try:                                    # 讀不懂就當沒有時間,不要讓整張卡掛掉
    dt = (datetime.fromtimestamp(float(ts), timezone.utc) if isinstance(ts, (int, float))
          else datetime.fromisoformat(ts).replace(tzinfo=timezone.utc))
    when = dt.astimezone(TPE).strftime("%m/%d %H:%M")
except (TypeError, ValueError):
    when = None
if "actual" not in snap:                # 老機器的快照沒有這個欄位——不能因為讀得到時間
    cap = "這台機器的對帳快照沒有實際部位(舊格式),實際欄全是「—」 · "   # 就說「剛對過帳」
elif not actual:
    cap = "上次對帳時交易所沒有部位 · " if when else "這台機器還沒對過帳 · "
else:
    cap = "對帳快照 " + when + " · " if when else "對帳快照 · "
cap += "數量為部位金額(帳戶計價幣別),台股/期貨標的為口數;進場均價與未實現損益機器上沒有來源"
if rows:
    write_data("positions", {"type": "table", "columns": [
        {"key": "s", "label": "標的", "align": "left"},
        {"key": "a", "label": "實際", "align": "right"},      # 方向在格子裡,不上色:
        {"key": "t", "label": "目標", "align": "right"}],     # 部位方向不是賺賠
        "rows": rows, "caption": cap})
'''
add_widget("positions", "block", "持倉明細", block_type="table",
           refresh_cron="*/5 * * * *", refresh_human="每 5 分鐘", script=script, w=6, h=4)
```

Zero fetch and no keys: `manager/last_reconcile.json` is written by `lib.portfolio.reconcile()`
on every round, and `manager/reconciler.py` runs a heartbeat round every 5 minutes even when no
strategy moved (`RECONCILE_EVERY_S = 300`), so `*/5` and the snapshot's own cadence line up —
faster would redraw the same file. `w=6, h=4` because the `table` default 6×3 holds five rows.

**What the snapshot actually holds**, and what follows from it: `{"ts", "target", "actual",
"orders"}` (plus `"ledger"` when `self_ledger` is on). `actual` is what `get_positions()` returned
that round *after* `lib/venue_wiring.auto_get_positions()` mapped it — `{key: {"side", "size"}}`,
where `size` is the **account-currency notional** (`size × mark_price`) for a crypto swap, **lots**
for a capital account, and the inventory value for a `SYMBOL@spot` row. The base-currency quantity
and the mark price are gone by then, so the 數量 column is money, not coins — label it that way.

**進場均價 and 未實現損益 are not columns, because nothing on this machine knows them.** A
`lib/account_*.py` position row is `{symbol, side, size, mark_price}` (capital's is
`{symbol: {side, size}}`, lots) — no entry price at any venue — and no other file stores one: `manager/orders.jsonl` keeps each leg's `fill_price`, but averaging
those into a cost basis is position accounting the machine does not do, on a log that
`references/manager.md` § self_ledger already documents as drift-prone. So the card shows **實際 vs
目標** instead, which is the honest superset: what is held, and whether the bot still wants it.
If the user asks for entry price and unrealised PnL, say the machine has no source for them today
rather than printing a number derived from a guess.

Two limits worth saying out loud. **One card, one account**: a machine trading both TW and crypto
would mix TWD and USDT in one money column, and there is no FX rate on the machine to bridge them
— filter on the target row's `exchange` and give each account its own card. And a machine that has
never reconciled has no snapshot: the card then lists targets with 實際 = 「—」 and the caption says
so. Never relabel a target as a position. `report_templates.table` is not used here because it
forces a block `title`, and the tile header is already the title.

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

### A `kpi_row` Taiwan market-temperature widget, once a day (machine)

```python
from lib.watch import add_widget

script = '''
from datetime import datetime, timedelta
from lib.data import (fetch_twmarket_index, fetch_twmarket_institutional,
                      fetch_twmarket_margin, fetch_twmarket_turnover)
from lib.report_templates import TPE, headers_from_env, kpi, kpi_row
from lib.watch import write_data

hdrs = headers_from_env()
today = datetime.now(TPE)
start, end = (today - timedelta(days=30)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")

def series(df, col):                     # 有值的那一欄;空的、缺欄的都回 None
    if df is None or col not in getattr(df, "columns", []):
        return None
    s = df[col].dropna()
    return s if len(s) else None

items, days = [], []
s = series(fetch_twmarket_index(start, end, hdrs), "Close")          # 大盤指數只有 OHLC
if s is not None and len(s) > 1:
    close = float(s.iloc[-1])
    chg = (close / float(s.iloc[-2]) - 1) * 100
    items.append(kpi("加權指數", f"{close:,.2f}",
                     "pos" if chg > 0 else "neg" if chg < 0 else "neutral",
                     delta=f"{chg:+.2f}% · {s.index[-1]:%m/%d} 收"))
    days.append(s.index[-1])

s = series(fetch_twmarket_turnover(start, end, hdrs), "value")       # 成交金額,元
if s is not None:
    val, avg5 = float(s.iloc[-1]), float(s.tail(5).mean())
    items.append(kpi("成交值", f"{val / 1e12:.2f}", "neutral", unit="兆",
                     delta=f"{(val / avg5 - 1) * 100:+.1f}% vs 5 日均 · {s.index[-1]:%m/%d}"))
    days.append(s.index[-1])

s = series(fetch_twmarket_institutional(start, end, hdrs), "foreign")  # 淨買賣超金額,元
if s is not None:
    v = float(s.iloc[-1])
    items.append(kpi("外資買賣超", f"{v / 1e8:+,.1f}",
                     "pos" if v > 0 else "neg" if v < 0 else "neutral",
                     unit="億", delta=f"{s.index[-1]:%m/%d}"))
    days.append(s.index[-1])

s = series(fetch_twmarket_margin(start, end, hdrs), "margin_balance")  # 融資餘額,張
if s is not None and len(s) > 1:
    v, prev = float(s.iloc[-1]), float(s.iloc[-2])
    items.append(kpi("融資餘額", f"{v / 1e4:,.1f}", "neutral", unit="萬張",
                     delta=f"{(v - prev) / 1e4:+,.1f} 萬張 · {s.index[-1]:%m/%d}"))
    days.append(s.index[-1])

if len(items) == 4:                      # 缺一格就不發,卡片留上一次的數字——三格的溫度列
                                         # 會被讀成「那一項沒有」,不是「沒抓到」
    ds = sorted({d.strftime("%Y-%m-%d") for d in days})   # 各欄資料日不一定同一天
    block = kpi_row(items)
    # caption 是純文字(渲染器不吃 markdown),不要放 ** 之類的記號
    block["caption"] = ("TWSE 盤後統計,非即時;各格資料日可能不同——融資餘額當日約 21:00 才公布,"
                        "傍晚跑的話那一格是前一個交易日,所以每格都帶自己的日期。資料日 "
                        + (ds[-1] if len(ds) == 1 else ds[0] + "~" + ds[-1] + ",見各格日期"))
    write_data("tw-temp", block)
'''
add_widget("tw-temp", "block", "台股大盤溫度 · 盤後", block_type="kpi_row",
           refresh_cron="10 8 * * *", refresh_human="每天 16:10(台北)", script=script)
```

Four `lib.data` calls, all month-file cached, on a series that changes once a day — so the card
runs once a day and nothing is gained by running it more often. `report_templates.tw_market_brief`
computes the same four numbers for the morning report; this is that row, on the board.

**The cron is the machine's local clock, and the Linux fleet runs on UTC** (that is why the
strategy-health example below imports `TPE`), so 16:10 Taipei is `10 8 * * *` there and
`10 16 * * *` on a box whose clock is Taipei — check `date` before you pick one, and restate
「每天 16:10(台北)」 to the user, which is the only form they see. `M H * * *` is inside the
Windows subset (`references/reports.md` §8).

**Two of the four numbers are a day behind when you run this at 16:10.** Margin balance is
published around 21:00 Taipei, so an afternoon run gets the previous session's figure; that is why
every cell carries its own date in `delta` and the caption says the dates can differ. If you would
rather have all four on the same day, schedule it the next morning instead (`0 22 * * *` UTC =
06:00 Taipei) and title it 昨日盤後 — say which one you chose. Never let a stale cell sit under a
fresh-looking timestamp without its date.

**Say the day, in a place that cannot be cut off.** The tile's own timestamp is the *run* time, so
on a holiday this card republishes yesterday's close with a fresh "updated at" — the title carries
`盤後` and the index cell's `delta` carries the trading day for exactly that reason; the caption
holds the 口徑 line but is the first thing a short tile clips. A card that reads "13:10 更新" over a
09/03 close is the misreading this one must not create. For the intraday number use the stream
tile instead — `add_widget("taiex", "price", "加權指數", symbol="TAIEX")` — the two complement each
other: live index above, after-hours context below.

**漲跌家數 is not a cell.** There is no market-wide breadth function on the machine;
`fetch_twstock_quote_batch` takes 50 ids per call, so the whole market is ~48 calls per run and
anything cheaper is an approximation. If the user wants it, offer the Top-200 version and put the
pool in the label (「漲跌家數(權值 200)」) — never a number that looks like the whole market.
**If one fetch fails, publish nothing.** `series()` turns a failed or empty fetch into `None` and
the cell is skipped, so `if items:` alone would still publish a half row — a three-cell 大盤溫度
reads as "外資沒買賣超" rather than "那一格沒抓到". Require all four (`if len(items) == 4:`) and
let the tile keep its previous numbers, marking itself stale after 3× the period.

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

### Price levels on a K-line (stream — no script)

The most hand-rolled thing on real machines. The user names the levels; you put them on the
chart:

```python
from lib.watch import add_widget

add_widget("tsmc-k5", "kline", "台積電 5 分 K", symbol="2330", interval="5m",
           levels=[{"price": 2300, "side": "below"}, {"price": 2450, "side": "above", "label": "前高"}])
```

One op, no job directory, nothing to schedule, and it costs nothing: the lines are drawn on the
candles and the browser moves the picture by the second from the same stream the chart uses. A
level is `price` + `side` — `below` fires when a bar's low reaches the price (跌破), `above` when
a high reaches it (突破) — plus an optional `label` (≤ 12 characters; it replaces the direction
word on the line, 「前高 2,450」 instead of 「突破 2,450」). `lib/watch.py` stamps every level
with `since` = now; passing `since` yourself is refused.

**Triggered is derived, not stored.** The web looks at the bars after `since`: the first one that
crosses the line gets the marker, the line turns solid in the direction's colour and the card
header says 「已跌破 2,300 · 13:25」. Reload, another device — the same answer, with no machine
script and no state on the platform. **One-shot**, like TradingView's "Stopped — Triggered": once
fired it stays fired until you change the levels. To arm it again, or watch a new price, send the
whole list once more:

```python
from lib.watch import update_widget

update_widget("tsmc-k5", levels=[{"price": 2280, "side": "below"},
                                 {"price": 2450, "side": "above", "label": "前高"}])
update_widget("tsmc-k5", levels=[])          # clear the monitor, keep the chart
```

`levels=None` (the default) leaves the monitor alone; a new list resets every level's `since`,
so the triggered state goes back to watching. The op carries only `{"levels": [...]}` — props
are a shallow merge on the api, so `interval` and everything else stay as they are. Four levels
per card is the cap (the labels overlap beyond that) — more prices, another card; one symbol
per card is the `kline` rule anyway. Crypto takes the same argument:

```python
from lib.watch import add_widget

add_widget("btc-1h", "kline", "BTC 1H", symbol="BTC", venue="binance", interval="60m",
           levels=[{"price": 100000, "side": "above", "label": "十萬"}])
```

Distance to the level is read by eye — the gap between the candles and the line, and on the right
axis the last-price label sitting next to the level's label. Do not print a percentage beside it,
and do not build this as a `kpi_row` script: a script refreshes at most once a minute and shows a
number where the chart shows the thing itself. A tile only shows; if the user wants to be *told*
when it fires, that is an alert (Telegram from a strategy or a cron script), not a widget.

### Indicator sub-panes on a K-line (stream, crypto — no script)

The user asks for 「BTC 的加上籌碼集中度」. That is one argument on the chart they already have,
not a second card:

```python
from lib.watch import add_widget

add_widget("btc-1h", "kline", "BTC 1 小時 K", symbol="BTC", venue="binance", interval="60m",
           panes=[{"id": "holder_concentration"}])
```

The indicator is drawn in its own pane under the candles, 6×4 by default (6×5 with two panes,
6×3 with none). To put one on a chart that is already on the board, or to take it off:

```python
from lib.watch import update_widget

update_widget("btc-1h", panes=[{"id": "holder_concentration"}])          # put one on
update_widget("btc-1h", panes=[{"id": "holder_concentration"},          # and a second
                               {"id": "funding_rate"}])
update_widget("btc-1h", panes=[])                                       # take them off
```

The list you send **replaces** the panes on the card, so adding a second one means sending both.
Everything else in `props` stays — `interval`, `levels` — because props are a shallow merge on
the api. `update_widget` never resizes the card (`grid` is the user's), so a chart the user
sized 6×3 will be tight with a pane on it: tell them to drag it a row taller rather than trying
to do it for them.

**`id` is the indicator's slug — the same word `lib/data.py` fetches it with.** You already
know these: `fetch_holder_concentration` calls `holder_concentration/get_alpha`, so the slug is
`holder_concentration`. The ten alpha fetchers in `lib/data.py` are the list to pick from:

`holder_concentration` 籌碼集中度 · `funding_rate` 資金費率 · `taker_intensity` 多空力道 ·
`whale_hunter` 巨鯨警報 · `unusual_movement` 異常漲跌 · `squeeze_momentum` 擠壓動能 ·
`liquidation` 爆倉指標 · `market_sentiment` 市場情緒 · `capital_shortage` 資金稀缺

(`market_direction` has a fetcher in `lib/data.py` but is **not** in the platform catalogue —
it cannot be a sub-pane; asking for it comes back as an unknown-slug 400. Verified on prod.)

The platform's catalogue is larger than this list (21 indicators) — prod also serves slugs that
have no fetcher here, such as `gtrade_holder_concentration`, `hyperliquid_top_trader_exposure`
and `main_alt` — but only indicators that draw as a line can be a sub-pane; a heat map or a
single-value indicator is refused. `lib/watch.py` checks the shape of the slug, never that it
exists, so anything wrong comes back as an api 400 and the op ends in `ops/failed/`. If the
user names an indicator that is not in the list above, say so rather than inventing a slug.

**Three things to check before you pick one.** All three are measured on prod, and all three are
things to tell the user about rather than work around:

- **Five of them are market-level, not per-symbol**: `blave_top_trader_exposure`,
  `capital_shortage`, `gtrade_holder_concentration`, `hyperliquid_top_trader_exposure` and
  `main_alt`. Put one on a BTC card and on an ETH card and you get two identical lines. Using
  them is fine; telling the user it is *his symbol's* data is not. The api response carries a
  `scope` field and that is the authority — verified on prod: those five rows really are
  `scope = market`, so the card labels them for you.
- **Some indicators are coarser than the candles and degrade to a few points.**
  `squeeze_momentum`'s finest period is 1d: measured against a 1m chart it comes back with
  **one point**, 5m three, 15m seven, 60m fourteen. Nothing errors — there is simply no line to
  look at. Match the indicator's resolution to the interval; a 1d indicator belongs under 60m
  or 1d candles.
- **Parameters are the platform's defaults, and v1 cannot change them.** `funding_rate`
  (exchange), `liquidation` / `taker_intensity` / `unusual_movement` (time frame) and
  `whale_hunter` (time frame and score type) all take parameters on the platform, but `panes`
  has no way to pass one — you get the default set (`24h`, `score_oi`). If the user wants a
  particular parameter, say so: Studio for now, or v2.

**A 1d card starts blank on the left.** The 1d indicator window is 180 days while the chart
loads 300 bars, so the oldest ~120 candles have no indicator line under them. That is the
contract's "align by time, never fill in" rule working, not a broken pane.

What this is and is not:

- **No machine, no job, no cron.** The platform fetches the indicator and the browser draws it.
  This is not a `block` widget, so there is no script to write and nothing to schedule —
  nothing on this machine runs for it.
- **Crypto only.** `panes` needs `venue="binance"`; the indicator library is a crypto one, so
  `add_widget` refuses a Taiwan `kline` before anything is written. `update_widget` cannot tell
  (a stream widget leaves no record here) — there the api refuses it and `status` shows `failed`.
- **At most two per card.** Past that the candles are squeezed out of the main pane. Two
  indicators that answer different questions belong on two charts.
- **The two run at different speeds, and the card says so.** Candles stream by the second;
  indicators are polled on their own cadence, so the pane carries its own data time. That gap
  is honest — do not describe the indicator as live.
- **Points stop where the indicator's data stops** — nothing is extended forward, and the
  platform may round an indicator up to its own minimum period (see the resolution note above).

**Do not build this as a script.** The 籌碼集中度 card an agent once shipped as a `block` +
`line_chart` on a `09:20` cron is exactly the wrong shape: it refreshes once a day, and it puts
the price and the indicator on two separate cards that the user has to line up by eye. A crypto
indicator that belongs under a price chart is `panes` on that chart's `kline` widget.
