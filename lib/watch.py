"""
Watchboard helper — add / update / remove widgets on the user's board and feed
the machine widgets, all by dropping files in `workspace/watch/`.

The board lives on the platform; this machine only writes files. An *op* file in
`watch/ops/` asks the platform to add, update or remove a widget; a *data* file
in `watch/data/<widget_id>.json` carries the current content of one machine
widget (one report block). **The write is the finish line:** the runtime's
uploader ships each file and moves ops to `ops/sent/` or `ops/failed/` (with a
line in `watch/upload_errors.log`); data files are overwritten in place on every
refresh. Never wait for the upload before saying the widget is created.

This module is a convenience only — the drop directory is the contract, so a file
written with `json.dump` into that path works exactly the same. What it saves
you: the atomic writes, the envelope boilerplate, the catalogue / size / cron /
symbol checks (so a bad widget fails here with a message instead of in
`ops/failed/` minutes later), and, for machine widgets, the `report_jobs/<id>/`
registration the runtime schedules from.

Catalogue, limits and the script template: `references/watchboard.md`.

Usage:
    from lib.watch import add_widget, update_widget, remove_widget, write_data, status

    add_widget("txf", "price", "台指期", symbol="TXF")                  # stream widget
    add_widget("tsmc-k", "kline", "2330 1 分 K", symbol="2330", interval="1m")
    add_widget("btc-1h", "kline", "BTC 1H", symbol="BTC", venue="binance", interval="60m")  # crypto
    add_widget("tsmc-k5", "kline", "台積電 5 分 K", symbol="2330", interval="5m",      # price levels
               levels=[{"price": 2300, "side": "below"}, {"price": 2450, "side": "above", "label": "前高"}])
    update_widget("tsmc-k5", levels=[{"price": 2280, "side": "below"}])   # reset the monitor
    add_widget("risk", "block", "持倉風險",                              # machine widget
               block_type="kpi_row", refresh_cron="*/5 * * * *",
               refresh_human="每 5 分鐘", script=script_text)
    update_widget("risk", title="持倉風險(實盤)")
    remove_widget("risk")

Inside a machine widget's `run.py` (scheduled by the runtime, no LLM, no token):
    from lib.watch import write_data
    write_data("risk", {"type": "kpi_row", "items": [...]})

Diagnosis only — never the next step after a write:
    status("risk")                     # newest op for that widget: pending | sent | failed: … | unknown
    status("1725256325123-add.json")   # one op file by name
"""

import json
import os
import re
import shutil
import time

from lib.report import (JOBS_DIR, WORKSPACE, _check_cron, _write_bytes, _write_text_atomic,
                        register_schedule, remove_schedule)

WATCH_DIR = os.path.join(WORKSPACE, "watch")
OPS_DIR = os.path.join(WATCH_DIR, "ops")
OPS_SENT_DIR = os.path.join(OPS_DIR, "sent")
OPS_FAILED_DIR = os.path.join(OPS_DIR, "failed")
DATA_DIR = os.path.join(WATCH_DIR, "data")
ERROR_LOG = os.path.join(WATCH_DIR, "upload_errors.log")

SCHEMA_VERSION = "1.0"
FILES_SUFFIX = ".files"
DATA_MAX_BYTES = 64 * 1024

# Widget id: the board's rule. A machine widget's id is also its report_jobs/ directory
# name, so it must satisfy the job-id rule as well (lowercase slug, no underscore).
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_MACHINE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_OP_FILE_RE = re.compile(r"[0-9]{13}-(add|update|remove)\.json")
_STOCK_RE = re.compile(r"[A-Za-z0-9]{4,8}")
# Crypto (venue="binance"): a Binance USD-M perpetual written as `BTC`, `BTCUSDT`, `BTC/USDT`
# or `BTC-USDT`. Only the shape is checked (after uppercasing); the api normalises the symbol
# and is the one that knows what Binance trades — no list is kept here.
_CRYPTO_RE = re.compile(r"[A-Z0-9]+(?:[/-][A-Z0-9]+)?")
_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")

INDEX_SYMBOLS = ("TAIEX",)
FUTURES_SYMBOLS = ("TXF", "MXF")
INTERVALS = ("1m", "5m", "15m", "60m", "1d")
# kline price levels (contract §3.4): at most 4 per card — more labels than that overlap
# on a 256px-high card; monitor more prices on another card.
LEVELS_MAX = 4
LEVEL_SIDES = ("above", "below")
LEVEL_LABEL_MAX = 12
VENUES = ("binance",)
CRYPTO_TYPES = ("price", "kline", "book")  # the only widgets that take a venue (contract §3.1)
WATCHLIST_MAX = 20

# type -> (kind, min w, min h). A block widget's minimum depends on its block_type:
# chart-like blocks need 4×3 (CHART_BLOCKS), the rest keep the 2×2 here (contract §3).
CATALOGUE = {
    "price": ("stream", 2, 2),
    "kline": ("stream", 4, 3),
    "book": ("stream", 2, 3),
    "watchlist": ("stream", 3, 2),
    "block": ("machine", 2, 2),
}
CHART_BLOCKS = ("line_chart", "drawdown", "heatmap", "bar_chart", "histogram", "box",
                "scatter", "image")
CHART_MIN_W, CHART_MIN_H = 4, 3

# Report block types a machine widget may declare / send. `meta` and `footnote` are
# report structure (head and tail of a document), `divider` draws nothing — none of
# them is content for a single tile, so they are not offered here.
BLOCK_TYPES = ("kpi_row", "line_chart", "drawdown", "heatmap", "bar_chart", "histogram",
               "box", "scatter", "metric_table", "table", "text", "quote", "code",
               "callout", "image")

# Per block type: the props that must be present. Same shape rules the report side
# applies before the api (references/reports.md §3); the api stays the only full
# validator, this just refuses the obviously wrong.
_BLOCK_REQUIRED = {
    "kpi_row": ("items",), "line_chart": ("series",), "drawdown": ("points",),
    "heatmap": ("variant", "values"), "bar_chart": ("variant",), "histogram": ("bins",),
    "box": ("groups",), "scatter": ("points",), "metric_table": ("items",),
    "table": ("columns", "rows"), "text": ("markdown",), "quote": ("text",),
    "code": ("lang", "source"), "callout": ("tone", "text"), "image": ("alt",),
}
# Required props that may still be an empty list: an empty table ("no movers today") is a
# legal tile and the api's validator accepts it; its `columns` stay non-empty.
_EMPTY_OK = {("table", "rows")}


# ─── validation ───────────────────────────────────────────────────────────────

def _check_id(id, machine=False):
    if not isinstance(id, str) or not _ID_RE.fullmatch(id):
        raise ValueError(f"widget id {id!r} must match [A-Za-z0-9_-]{{1,32}}")
    if machine and not _MACHINE_ID_RE.fullmatch(id):
        raise ValueError(f"machine widget id {id!r} must be a slug matching "
                         "[a-z0-9][a-z0-9-]{0,31} (it is also the report_jobs/ directory name)")


def _check_title(title):
    if not isinstance(title, str) or not 1 <= len(title) <= 40:
        raise ValueError("title must be a string of 1–40 characters")


def _check_symbol(symbol, type):
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol must be a non-empty string")
    if symbol in FUTURES_SYMBOLS:
        return symbol
    if type == "book":
        raise ValueError(f"book widget only supports {' / '.join(FUTURES_SYMBOLS)} "
                         f"(or a crypto perpetual with venue=\"binance\"), got {symbol!r}")
    if symbol in INDEX_SYMBOLS or _STOCK_RE.fullmatch(symbol):
        return symbol
    raise ValueError(f"symbol {symbol!r} must be a stock id (4–8 letters/digits), "
                     f"{' / '.join(INDEX_SYMBOLS)} or {' / '.join(FUTURES_SYMBOLS)}")


def _check_venue(venue, type):
    if venue not in VENUES:
        raise ValueError(f"venue {venue!r} must be one of {', '.join(VENUES)}")
    if type not in CRYPTO_TYPES:
        raise ValueError(f"venue is only for {' / '.join(CRYPTO_TYPES)} widgets (crypto, "
                         f"contract §3.1); {type} takes Taiwan symbols only")
    return venue


def _check_crypto_symbol(symbol):
    """Shape only: uppercased, letters/digits with one optional `/` or `-`, 3–20 characters
    once the separator is gone. Returns the symbol as given, uppercased, separator removed
    (`btc/usdt` -> `BTCUSDT`, `BTC` stays `BTC`); the api normalises from there."""
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol must be a non-empty string")
    up = symbol.upper()
    bare = up.replace("/", "").replace("-", "")
    if not _CRYPTO_RE.fullmatch(up) or not 3 <= len(bare) <= 20:
        raise ValueError(f"crypto symbol {symbol!r} must be 3–20 letters/digits with an optional "
                         "'/' (BTC, BTCUSDT, BTC/USDT)")
    return bare


# Default sizes when the caller gives none (contract §3). The minimum is a floor —
# below it the card cannot draw its content — and is NOT the default: measured at
# 1440 wide with the chat pane open the canvas is only ~750px (12 cols ≈ 62px), and
# every one of these defaults is the size at which the card stays readable there.
# The user can still drag anything down to its minimum.
_DEFAULT_WH = {
    "price": (4, 2),      # a 3-col card truncates its title
    "kline": (6, 3),      # candles need room to read
    "book": (3, 3),       # 2×3 squeezes the volume bars to a slit and clips the spread
    "watchlist": (4, 3),  # fits 5 rows; more than that scrolls
}
# block widgets default by block_type; anything not listed falls back to its minimum
_DEFAULT_BLOCK_WH = {
    "kpi_row": (6, 3),    # four cells fold to 2+2 on a narrow canvas and h=2 hides the values
    "table": (6, 3),      # 5 rows is what h=3 holds
    "callout": (4, 2),    # two lines of body text
}


def _check_grid(type, w, h, block_type=None):
    kind, min_w, min_h = CATALOGUE[type]
    label = type
    if block_type in CHART_BLOCKS:
        min_w, min_h, label = CHART_MIN_W, CHART_MIN_H, f"{block_type} block"
    dw, dh = _DEFAULT_WH.get(type, (min_w, min_h))
    if type == "block":
        dw, dh = _DEFAULT_BLOCK_WH.get(block_type, (min_w, min_h))
        if block_type in CHART_BLOCKS:
            dw, dh = 6, 3
    w = dw if w is None else w
    h = dh if h is None else h
    for name, v, lo in (("w", w, min_w), ("h", h, min_h)):
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= 12:
            raise ValueError(f"{name} for a {label} widget must be an int in {lo}–12, got {v!r}")
    return {"w": w, "h": h}


def _check_refresh(cron, human):
    if cron is None or human is None:
        raise ValueError("refresh_cron and refresh_human go together")
    fields = _check_cron(cron)          # 5 fields, so once a minute is the densest possible
    if not isinstance(human, str) or not 1 <= len(human) <= 60:
        raise ValueError("refresh_human must be a string of 1–60 characters")
    return {"human": human, "cron": " ".join(fields)}


def _check_block_type(block_type):
    if block_type not in BLOCK_TYPES:
        raise ValueError(f"block_type {block_type!r} is not one of {', '.join(BLOCK_TYPES)}")
    return block_type


def _check_interval(interval):
    if interval not in INTERVALS:
        raise ValueError(f"interval {interval!r} must be one of {', '.join(INTERVALS)}")
    return interval


def _check_levels(levels):
    """kline price levels (contract §3.4): a list of 0–4 `{price, side, label?}` dicts.
    Returns a fresh list with `since` stamped now on every entry — the moment the monitor
    starts; the web decides "triggered" from the bars after it. The caller never sends
    `since`: a hand-picked time would re-trigger on old bars."""
    if not isinstance(levels, (list, tuple)):
        raise ValueError("levels must be a list of {'price': ..., 'side': 'above'|'below', 'label'?: ...}")
    if len(levels) > LEVELS_MAX:
        raise ValueError(f"at most {LEVELS_MAX} levels per kline card (got {len(levels)}) — "
                         "labels overlap beyond that; monitor more prices on another card")
    now = int(time.time())
    out = []
    for i, lv in enumerate(levels):
        p = f"levels[{i}]"
        if not isinstance(lv, dict):
            raise ValueError(f"{p} must be a dict with price and side")
        if "since" in lv:
            raise ValueError(f"{p}.since is filled in by lib/watch.py (the moment the monitor "
                             "starts) — never pass it")
        extra = set(lv) - {"price", "side", "label"}
        if extra:
            raise ValueError(f"{p} has unknown keys {sorted(extra)}; only price, side, label")
        price = lv.get("price")
        if (isinstance(price, bool) or not isinstance(price, (int, float))
                or price != price or price in (float("inf"), float("-inf")) or price <= 0):
            raise ValueError(f"{p}.price must be a finite positive number, got {price!r}")
        if lv.get("side") not in LEVEL_SIDES:
            raise ValueError(f"{p}.side must be one of {', '.join(LEVEL_SIDES)} "
                             f"(above = breakout, below = breakdown), got {lv.get('side')!r}")
        clean = {"price": price, "side": lv["side"], "since": now}
        if "label" in lv:
            label = lv["label"]
            label = label.strip() if isinstance(label, str) else label  # api strips too; agree on what counts
            if not isinstance(label, str) or not 1 <= len(label) <= LEVEL_LABEL_MAX:
                raise ValueError(f"{p}.label must be 1–{LEVEL_LABEL_MAX} characters")
            clean["label"] = label
        out.append(clean)
    return out


def _check_block(block):
    """Reject an obviously wrong block: not an object, unknown type, a required prop
    missing or empty, NaN anywhere (json.dumps with allow_nan=False does that last one
    when the file is written)."""
    if not isinstance(block, dict):
        raise ValueError("block must be a dict like {'type': 'kpi_row', 'items': [...]}")
    btype = block.get("type")
    if btype not in BLOCK_TYPES:
        raise ValueError(f"block type {btype!r} is not one of {', '.join(BLOCK_TYPES)}")
    for key in _BLOCK_REQUIRED[btype]:
        v = block.get(key)
        if v == [] and (btype, key) in _EMPTY_OK:
            continue
        if v is None or (isinstance(v, (str, list, dict)) and not v):
            raise ValueError(f"{btype} block needs a non-empty {key!r}")
    if btype == "image":
        has_file, has_sha = "file" in block, "sha256" in block
        if has_file == has_sha:
            raise ValueError("image block needs exactly one of 'file' (sidecar) or 'sha256'")
        if has_file and not (isinstance(block["file"], str) and _FILE_RE.fullmatch(block["file"])):
            raise ValueError(f"image file {block.get('file')!r} must be a plain file name, not a path")
        if has_sha and not (isinstance(block["sha256"], str) and _SHA_RE.fullmatch(block["sha256"])):
            raise ValueError("image sha256 must be 64 lowercase hex characters")
    return btype


# ─── files ────────────────────────────────────────────────────────────────────

def _write_json_atomic(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"          # never ends in .json, so the scan ignores it
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def _write_op(op, body):
    """One op file `ops/<epoch_ms>-<op>.json`; the ms is bumped if that name exists so
    two ops in the same millisecond keep their order."""
    os.makedirs(OPS_DIR, exist_ok=True)
    ms = int(time.time() * 1000)
    while os.path.exists(os.path.join(OPS_DIR, f"{ms}-{op}.json")):
        ms += 1
    doc = {"schema_version": SCHEMA_VERSION, "op": op}
    doc.update(body)
    return _write_json_atomic(os.path.join(OPS_DIR, f"{ms}-{op}.json"), doc)


def _job_path(id):
    return os.path.join(JOBS_DIR, id, "job.json")


def _read_job(id):
    try:
        with open(_job_path(id), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def _read_watch_job(id):
    """The `report_jobs/<id>/job.json` of a machine *widget* — only a registration tagged
    `kind: "watch"` counts. A scheduled report (no `kind`, or another one) under the same id
    is the user's and is never read as, rewritten as or removed with a widget."""
    job = _read_job(id)
    return job if job is not None and job.get("kind") == "watch" else None


# The runtime runs a job as `python3 report_jobs/<id>/run.py` from the workspace, and
# Python puts the *script's* directory on sys.path — not the cwd — so a bare
# `from lib…` raises ModuleNotFoundError (seen on 29026: every §5 example failed on
# first run). Pin the workspace before the user's script; a script that already does
# it is left alone.
_PATH_BOOTSTRAP = ("import os as _os, sys as _sys\n"
                   "_sys.path.insert(0, _os.getcwd())  # lib/ lives in the workspace, not next to run.py\n")


def _with_workspace_on_path(script):
    # "already pinned" means a real statement, not a comment mentioning sys.path
    # (§3 of the reference says add_widget does this — an agent will write that down)
    if _PATH_BOOTSTRAP in script or re.search(r"^\s*sys\.path\.(insert|append)\(", script, re.M):
        return script
    # `from __future__` must stay the first statement — slot the bootstrap after it
    lines = script.splitlines(keepends=True)
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")
                              or lines[i].startswith("from __future__")):
        i += 1
    if i and any(l.startswith("from __future__") for l in lines[:i]):
        return "".join(lines[:i]) + _PATH_BOOTSTRAP + "".join(lines[i:])
    return _PATH_BOOTSTRAP + script


def _register_watch_job(id, title, refresh, block_type, script):
    """`report_jobs/<id>/` through lib.report.register_schedule (same cron grammar,
    same created_at / updated_at / pending handling), then tag the registration with
    `kind: "watch"` and the widget's block type. The runtime ignores fields it does
    not know, so a runtime without watch support installs it as a plain job."""
    job_dir = register_schedule(id, title, title, refresh["cron"], refresh["human"],
                                _with_workspace_on_path(script))
    doc = _read_job(id) or {}
    doc.update({"kind": "watch", "widget_id": id, "block_type": block_type})
    _write_text_atomic(_job_path(id), json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return job_dir


# ─── public api ───────────────────────────────────────────────────────────────

def add_widget(id, type, title, *, symbol=None, symbols=None, interval=None, venue=None,
               levels=None, block_type=None, refresh_cron=None, refresh_human=None,
               script=None, w=None, h=None):
    """Add one widget to the board. Returns the op file path.

    id            `[A-Za-z0-9_-]{1,32}`, unique on the board — yours to pick. A machine
                  widget's id is also its `report_jobs/<id>/` directory, so it must be a
                  slug `[a-z0-9][a-z0-9-]{0,31}`.
    type          `price` / `kline` / `book` / `watchlist` (stream) or `block` (machine).
    title         1–40 chars, the tile header.
    symbol        stream, all but watchlist: a stock id (4–8 letters/digits), `TAIEX`,
                  `TXF` or `MXF`; `book` accepts only `TXF` / `MXF` (or a crypto
                  perpetual with venue="binance"). With venue="binance"
                  a Binance USD-M perpetual — `BTC`, `BTCUSDT` or `BTC/USDT` — written
                  as given, uppercased, `/` and `-` removed; the api normalises the rest.
    symbols       watchlist: 1–20 symbols (same shapes as `symbol`; Taiwan only).
    interval      kline: `1m` / `5m` / `15m` / `60m` / `1d`.
    levels        kline only (Taiwan or crypto): price monitor lines drawn on the chart,
                  0–4 of `{"price": 2300, "side": "below"}` / `{"price": 2450, "side":
                  "above", "label": "前高"}`. `side` is the crossing direction (above =
                  breakout, below = breakdown); `label` ≤ 12 chars replaces the direction
                  word. Stamped with `since` = now here — never pass it. The web derives
                  "triggered" from the bars after `since` (one-shot: stays triggered until
                  you `update_widget(levels=...)`). No script, second-level, zero cost.
    venue         price / kline / book only: `"binance"` makes `symbol` a crypto symbol (contract
                  §3.1; refused on book / watchlist / block). A crypto K-line is always
                  this widget, never a block + line_chart drawn by a script — the live
                  candles come from the platform stream.
    block_type    machine: the report block the script will send (see BLOCK_TYPES).
    refresh_cron  machine: 5-field cron, this machine's local time; `*/1 * * * *` is
                  the densest schedule there is — no seconds field. Only the grammar is
                  checked here; a Windows machine runs the subset in
                  references/reports.md §8 (hourly is `0 */1 * * *`, not `0 * * * *`),
                  and a form outside it is not installed and shows as an error in the
                  user's list.
    refresh_human machine: the schedule in words (「每 5 分鐘」), the only form the
                  user sees, so it must match the cron.
    script        machine: the full text of `report_jobs/<id>/run.py`. It runs like a
                  scheduled strategy (cwd = workspace, no `BLAVE_*`, no token, no LLM)
                  and publishes by calling `write_data(id, block)`.
    w, h          initial size in grid units; default = the type's minimum (price: 4×2,
                  kline: 6×3; a block is 2×2, or 4×3 when its block_type is chart-like —
                  see CHART_BLOCKS). Position is the user's — the platform appends the
                  tile to the bottom row.
    """
    if type not in CATALOGUE:
        raise ValueError(f"type {type!r} is not one of {', '.join(CATALOGUE)}")
    kind = CATALOGUE[type][0]
    _check_id(id, machine=(kind == "machine"))
    _check_title(title)
    props = {}

    if kind == "stream":
        for name, v in (("block_type", block_type), ("refresh_cron", refresh_cron),
                        ("refresh_human", refresh_human), ("script", script)):
            if v is not None:
                raise ValueError(f"{name} is for machine widgets; {type} is a stream widget")
        if venue is not None:
            _check_venue(venue, type)
        if type == "watchlist":
            if symbol is not None:
                raise ValueError("watchlist takes symbols=[...], not symbol=")
            if not isinstance(symbols, (list, tuple)) or not 1 <= len(symbols) <= WATCHLIST_MAX:
                raise ValueError(f"watchlist needs symbols=[...] with 1–{WATCHLIST_MAX} entries")
            syms = [_check_symbol(s, type) for s in symbols]
            if len(set(syms)) != len(syms):
                raise ValueError("watchlist symbols must be distinct")
            source = {"kind": "stream", "symbols": syms}
        else:
            if symbols is not None:
                raise ValueError(f"{type} takes symbol=..., not symbols=")
            if venue is None:
                source = {"kind": "stream", "symbol": _check_symbol(symbol, type)}
            else:
                source = {"kind": "stream", "venue": venue, "symbol": _check_crypto_symbol(symbol)}
        if type == "kline":
            props["interval"] = _check_interval(interval)
            if levels is not None:
                checked = _check_levels(levels)
                if checked:                      # [] on add = no monitor, same as omitting
                    props["levels"] = checked
        else:
            for name, v in (("interval", interval), ("levels", levels)):
                if v is not None:
                    raise ValueError(f"{name} is only for kline widgets, not {type}")
    else:
        for name, v in (("symbol", symbol), ("symbols", symbols), ("interval", interval),
                        ("venue", venue), ("levels", levels)):
            if v is not None:
                raise ValueError(f"{name} is for stream widgets; block is a machine widget")
        props["block_type"] = _check_block_type(block_type)
        refresh = _check_refresh(refresh_cron, refresh_human)
        if not isinstance(script, str) or not script.strip():
            raise ValueError("a machine widget needs script= (the full text of run.py)")
        if os.path.exists(_job_path(id)) and _read_watch_job(id) is None:
            # Same id as a scheduled report: registering would overwrite it in place.
            raise ValueError(f"report_jobs/{id}/ is a scheduled report, not a widget — "
                             "pick another id (or remove_schedule it first if the user asks)")
        source = {"kind": "machine", "refresh": refresh}
    grid = _check_grid(type, w, h, props.get("block_type"))    # block minimum depends on block_type

    widget = {"id": id, "type": type, "title": title, "grid": grid, "source": source,
              "props": props, "created_by": "agent", "updated_at": int(time.time())}
    if kind == "machine":
        # Registration first: the runtime installs the schedule from it, and an op that
        # lands without a job would leave a tile that never fills.
        _register_watch_job(id, title, refresh, props["block_type"], script)
    return _write_op("add", {"widget": widget})


def update_widget(id, *, title=None, props=None, levels=None, refresh_cron=None,
                  refresh_human=None, script=None):
    """Change a widget's title, props, price levels (kline), refresh schedule or
    (machine only) script. Returns the op file path, or None when only the script
    changed (that is local — the runtime picks the new run.py up on its next run, no
    op needed).

    Never the position or size: `grid` belongs to the user's drag-and-drop and an
    update op does not carry it. `props` is a dict of the type's props (`interval` for
    kline, `block_type` for block); for a machine widget the local `report_jobs/<id>/`
    registration is rewritten to match, through the same `register_schedule` path.

    `levels` (kline only, contract §3.4): `None` leaves the monitor alone, `[]` clears
    it, a new list **resets** it — every entry gets a fresh `since`, so the web's
    triggered state goes back to "watching". Same shape as in `add_widget`; `since` is
    never yours to pass.

    `props` and `levels` are a shallow merge on the api (contract §4.1): only the keys
    sent change, the rest of the widget's props stay. Whether the widget is a kline is
    not checked here — a stream widget leaves no record on this machine — so `levels`
    on any other type is refused by the api (400, `status` shows `failed` naming it).

    Changing a block's `block_type` to a chart-like one (CHART_BLOCKS, minimum 4×3) is
    not size-checked here — this machine has no view of the board. If the tile is
    currently smaller the api refuses the op (400, shows as `failed`); ask the user to
    resize the tile first, then send the update again.
    """
    _check_id(id)
    job = _read_watch_job(id)                 # present = machine widget on this machine
    patch = {}
    if title is not None:
        _check_title(title)
        patch["title"] = title
    if props is not None:
        if not isinstance(props, dict):
            raise ValueError("props must be a dict")
        for k, v in props.items():
            if k == "interval":
                _check_interval(v)
            elif k == "block_type":
                _check_block_type(v)
            elif k == "levels":
                raise ValueError("pass levels=[...] as its own argument, not inside props= "
                                 "(lib stamps each level's since)")
            else:
                raise ValueError(f"unknown prop {k!r} (interval for kline, block_type for block)")
        if job is not None and set(props) - {"block_type"}:
            raise ValueError("a machine widget's props are only block_type")
        patch["props"] = dict(props)
    if levels is not None:
        if job is not None:
            raise ValueError(f"levels is only for kline widgets; {id!r} is a machine widget")
        patch.setdefault("props", {})["levels"] = _check_levels(levels)   # [] clears
    if refresh_cron is not None or refresh_human is not None:
        patch["source"] = {"refresh": _check_refresh(refresh_cron, refresh_human)}
    if script is not None and (not isinstance(script, str) or not script.strip()):
        raise ValueError("script must be the full text of run.py")
    if not patch and script is None:
        raise ValueError("nothing to update: give title, props, refresh_cron+refresh_human or script")

    if job is not None and ("title" in patch or "props" in patch or "source" in patch or script):
        sched = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        refresh = patch.get("source", {}).get("refresh") or {"cron": sched.get("cron"),
                                                              "human": sched.get("human")}
        if script is None:
            with open(os.path.join(JOBS_DIR, id, "run.py"), encoding="utf-8") as f:
                script = f.read()
        _register_watch_job(id, patch.get("title", job.get("title", id)), _check_refresh(
            refresh["cron"], refresh["human"]),
            patch.get("props", {}).get("block_type", job.get("block_type")), script)
    elif script is not None:
        raise ValueError(f"{id!r} has no report_jobs/ registration on this machine — "
                         "only machine widgets have a script")
    if not patch:
        return None
    return _write_op("update", {"id": id, "patch": patch})


def remove_widget(id):
    """Remove a widget: the op for the platform, plus — for a machine widget — its
    `report_jobs/<id>/` registration (the runtime drops the schedule), its pending
    data file and picture sidecar (nothing left for the uploader to ship to a tile
    that no longer exists). A scheduled report under the same id is left alone.
    Returns the op file path."""
    _check_id(id)
    # Only a widget's own registration goes: a stream id has none, and a scheduled
    # report that happens to share the id is the user's — never rmtree it.
    if _MACHINE_ID_RE.fullmatch(id) and _read_watch_job(id) is not None:
        remove_schedule(id)
    for path in (os.path.join(DATA_DIR, id + ".json"), os.path.join(DATA_DIR, id + FILES_SUFFIX)):
        try:
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        except OSError:
            pass
    return _write_op("remove", {"id": id})


def write_data(widget_id, block, images=None):
    """Publish the current content of a machine widget: `watch/data/<widget_id>.json`,
    overwritten on every call (the board keeps only "now"). Returns the file path.

    block   one report block, `{"type": "kpi_row", ...}` — shapes and limits in
            `references/reports.md` §3. Display values are formatted strings, chart
            values are numbers, nothing may be NaN. Its type must be the one the
            widget declared in `add_widget(block_type=...)`.
    images  `{file name: bytes}` for an `image` block's `{"file": ...}`, written to the
            sidecar `watch/data/<widget_id>.files/` before the JSON (same rule as
            reports: pictures first, JSON last).

    The whole file must stay under 64 KB — trim rows / points before calling, the
    uploader refuses anything larger. No network is touched here; this is what a
    scheduled `run.py` calls, and that process holds no token anyway.
    """
    _check_id(widget_id)
    btype = _check_block(block)
    job = _read_job(widget_id)
    declared = job.get("block_type") if job else None
    if declared and declared != btype:
        raise ValueError(f"widget {widget_id!r} declared block_type {declared!r}, got {btype!r}")
    for name in images or {}:
        if not isinstance(name, str) or not _FILE_RE.fullmatch(name):
            raise ValueError(f"image name {name!r} must be a plain file name, not a path")
    if btype == "image" and "file" in block and block["file"] not in (images or {}):
        raise ValueError(f"image block references {block['file']!r} but images= does not carry it")

    doc = {"schema_version": SCHEMA_VERSION, "widget_id": widget_id,
           "generated_at": int(time.time()), "block": block}
    body = json.dumps(doc, ensure_ascii=False, allow_nan=False)
    size = len(body.encode("utf-8"))
    if size > DATA_MAX_BYTES:
        raise ValueError(f"data for {widget_id!r} is {size} bytes, limit {DATA_MAX_BYTES} — "
                         "send fewer rows / points")
    for name, data in (images or {}).items():
        _write_bytes(os.path.join(DATA_DIR, widget_id + FILES_SUFFIX, name), data)
    return _write_json_atomic(os.path.join(DATA_DIR, widget_id + ".json"), doc)


def status(op_file_or_widget_id):
    """Where an op got to: 'pending' (still in `ops/`), 'sent', 'failed: <reason>'
    (last matching line of `watch/upload_errors.log`) or 'unknown'.

    Pass an op file name (`1725256325123-add.json`, as returned by add / update /
    remove) to check that op, or a widget id to check the newest op that mentions
    it. A diagnostic for after the fact — the uploader runs on its own timer, so
    never poll this waiting for 'sent'. 'unknown' is not an error: `ops/sent/` keeps
    only recent files, and a machine on an older runtime never moves anything, so
    its ops simply stay 'pending'.
    """
    key = os.path.basename(op_file_or_widget_id)
    if _OP_FILE_RE.fullmatch(key):
        for state, d in (("pending", OPS_DIR), ("sent", OPS_SENT_DIR), ("failed", OPS_FAILED_DIR)):
            if os.path.exists(os.path.join(d, key)):
                return _describe(state, key, key)
        return "unknown"
    _check_id(key)
    newest = None
    for state, d in (("pending", OPS_DIR), ("sent", OPS_SENT_DIR), ("failed", OPS_FAILED_DIR)):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not _OP_FILE_RE.fullmatch(name):
                continue
            try:
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    doc = json.load(f)
                wid = doc.get("id") or (doc.get("widget") or {}).get("id")
            except (OSError, ValueError, AttributeError):
                continue
            if wid == key and (newest is None or name > newest[1]):
                newest = (state, name)
    if newest is None:
        return "unknown"
    return _describe(newest[0], newest[1], key)


def _describe(state, name, key):
    if state != "failed":
        return state
    return f"failed: {_last_error(name) or _last_error(key) or 'see watch/upload_errors.log'}"


def _last_error(token):
    try:
        with open(ERROR_LOG, encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if token in ln]
    except OSError:
        return None
    return lines[-1] if lines else None
