"""
Report helper — build a report and drop it in `workspace/reports/`.

A report is a JSON document the platform stores and the web workspace renders in
the sidebar (charts, KPI rows, tables, prose). The machine publishes one by
landing a file at `workspace/reports/<id>.json`. **The write is the finish line:**
the runtime's uploader (a 2-minute timer) ships it and moves the file to
`reports/sent/`, or to `reports/failed/` plus a line in
`reports/upload_errors.log`. Do not wait for the upload before saying the report
is produced.

This module is a convenience only — the drop directory is the contract, so a
report written with `json.dump` into that path works exactly the same. What it
saves you: the atomic write the contract requires, the envelope boilerplate, the
mandatory leading `meta` block, the image sidecar (written before the JSON, in
the order the contract requires), and the three-directory status check.

Block types and their fields: `references/reports.md`.

Usage:
    from lib.report import write_report

    path = write_report(
        "mcpt-2317-20260901",
        "2317 MCPT 檢定",
        [
            {"type": "text", "variant": "lead", "markdown": "p = 0.012..."},
            {"type": "kpi_row", "items": [
                {"label": "p-value", "value": "0.012", "tone": "pos"}]},
            {"type": "image", "file": "perm.png", "alt": "permutation histogram"},
        ],
        type="research",
        report_type="一次性",
        images={"perm.png": open("tmp/perm.png", "rb").read()},
    )

Diagnosis only — never the next step after `write_report`. Reach for it when a
report never appeared or you suspect it was refused:

    from lib.report import status
    status("mcpt-2317-20260901")   # 'pending' | 'sent' | 'failed: <reason>' | 'unknown'
"""

import json
import os
import re
import time

# The uploader scans $BLAVE_AGENT_WORKSPACE/reports, so that env var wins when it
# is set. It is often absent though: scheduled strategy subprocesses are started
# with a minimal env that strips every BLAVE_* variable, so fall back to the
# directory this file lives in (lib/ is always inside the workspace).
WORKSPACE = os.environ.get("BLAVE_AGENT_WORKSPACE") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
REPORTS_DIR = os.path.join(WORKSPACE, "reports")
SENT_DIR = os.path.join(REPORTS_DIR, "sent")
FAILED_DIR = os.path.join(REPORTS_DIR, "failed")
ERROR_LOG = os.path.join(REPORTS_DIR, "upload_errors.log")

SCHEMA_VERSION = "1.1"
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# An `image` block's `file`: a plain name inside the sidecar, never a path. This one
# IS checked here — the name is used to open a file for writing, so `../` would put
# bytes outside the drop dir. Everything else about the picture (extension, size) is
# the uploader's call, and it reports through reports/upload_errors.log.
_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
FILES_SUFFIX = ".files"


def _write_bytes(path, data):
    """One sidecar picture, written the way the report itself is: into a `.tmp` the
    uploader's scan ignores, then `os.replace()` so it appears whole or not at all."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_report(report_id, title, blocks, type="research", report_type=None,
                 created_at=None, meta=None, images=None):
    """Write one report into the drop directory. Returns the file path.

    report_id   `[A-Za-z0-9_-]{1,64}`; it is the file name AND the report id, and
                re-using it overwrites that report on the platform — so a
                deterministic id makes a re-run idempotent, and a per-run id
                (a date, a timestamp) keeps every run. Do NOT use the runtime's
                own ids (`daily-YYYY-MM-DD`, `wk-YYYY-MM-DD`).
    title       1–200 chars; shown in the sidebar list and the push notification.
    blocks      the block list (see `references/reports.md`). A `meta` block is
                prepended unless blocks[0] already is one.
    type        `performance` / `morning` / `research` — sidebar grouping.
    report_type display string for the report header ("績效週報", "一次性");
                defaults to `type`.
    created_at  unix seconds, int; defaults to now.
    meta        extra props for the generated meta block (`period`, `account`,
                `benchmark`, `origin`, `machine`, `extra`).
    images      `{file name: bytes}` for the picture sidecar `<id>.files/`, named
                from an `image` block as `{"type": "image", "file": "perm.png",
                "alt": ...}`. The uploader carries the bytes and swaps `file` for
                the `sha256` the platform stores — which is the only way to get a
                figure out of a scheduled run, where the machine token is stripped
                from the environment. png / jpg / jpeg / webp / gif, ≤2MB each.

    Nothing here is validated beyond the report id and the image file names (a name
    becomes a path on this disk, so it may not be one): the api is the only validator,
    and a second copy of the rules on this side would drift and start refusing reports
    the platform accepts. A rejected report lands in `reports/failed/` with the
    api's message (it names the offending field path) in `upload_errors.log`.
    """
    if not isinstance(report_id, str) or not _ID_RE.fullmatch(report_id):
        raise ValueError(f"report id {report_id!r} must match [A-Za-z0-9_-]{{1,64}}")
    # Check every name before writing any of them: a bad one halfway through would
    # otherwise leave a sidecar holding some of the pictures and raise anyway.
    for name in images or {}:
        if not isinstance(name, str) or not _FILE_RE.fullmatch(name):
            raise ValueError(f"image name {name!r} must be a plain file name "
                             "matching [A-Za-z0-9][A-Za-z0-9._-]{0,79}, not a path")
    blocks = list(blocks)
    created_at = int(created_at if created_at is not None else time.time())
    if not blocks or not (isinstance(blocks[0], dict) and blocks[0].get("type") == "meta"):
        head = {"type": "meta", "title": title,
                "report_type": report_type or type, "generated_at": created_at}
        head.update(meta or {})
        blocks.insert(0, head)
    doc = {"schema_version": SCHEMA_VERSION, "id": report_id, "type": type,
           "title": title, "created_at": created_at, "blocks": blocks}

    os.makedirs(REPORTS_DIR, exist_ok=True)
    # Pictures first, JSON last — the report landing is what makes the set visible to
    # the uploader, so everything it references must already be on disk. Raising here
    # leaves a sidecar with no report, which the uploader sweeps after a day.
    for name, data in (images or {}).items():
        _write_bytes(os.path.join(REPORTS_DIR, report_id + FILES_SUFFIX, name), data)
    path = os.path.join(REPORTS_DIR, report_id + ".json")
    # Atomic: the uploader may scan mid-write. The temp name must not end in
    # `.json` or the scan would pick up the half-written file.
    tmp = path + ".tmp"
    # utf-8 explicitly — titles carry Chinese and a Windows machine's locale
    # default (cp950) raises on them. allow_nan=False so a NaN fails here, with
    # a stack trace, instead of being refused by the api hours later.
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, path)
    except Exception:
        # A half-written .tmp is inert (the uploader only scans `.json`), but leaving
        # one behind after every rejected NaN just accumulates confusing litter.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def status(report_id):
    """Where a dropped report got to: 'pending' (still queued), 'sent',
    'failed' (with the reason), or 'unknown'.

    A diagnostic for after the fact, not a step after `write_report` — the
    uploader runs on a 2-minute timer, so never poll this waiting for 'sent'.

    'unknown' is not an error — `reports/sent/` keeps only the most recent ~20
    files, so a report that shipped a while ago reports 'unknown' too.
    """
    name = report_id + ".json"
    if os.path.exists(os.path.join(REPORTS_DIR, name)):
        return "pending"
    if os.path.exists(os.path.join(SENT_DIR, name)):
        return "sent"
    if os.path.exists(os.path.join(FAILED_DIR, name)):
        return f"failed: {_last_error(report_id) or 'see reports/upload_errors.log'}"
    return "unknown"


def _last_error(report_id):
    try:
        with open(ERROR_LOG, encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if f" {report_id}: " in ln]
    except OSError:
        return None
    return lines[-1] if lines else None
