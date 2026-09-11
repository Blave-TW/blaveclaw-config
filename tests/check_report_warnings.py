"""Minimal check for the research-skeleton warnings in lib/report.write_report
(references/reports.md §7b): they fire on a long title / missing kpi_row, stay quiet on a
compliant research report and on non-research types, and never stop the write.
Run: cd blaveclaw-config && .venv/bin/python tests/check_report_warnings.py
"""
import contextlib, io, os, shutil, sys, tempfile
WS = tempfile.mkdtemp(prefix="rpt-")
os.environ["BLAVE_AGENT_WORKSPACE"] = WS
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.report import write_report

LEAD = {"type": "text", "variant": "lead", "markdown": "claim"}
KPI = {"type": "kpi_row", "items": [{"label": "x", "value": "1", "tone": "neutral"}]}
fails = []

def run(rid, title, blocks, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        path = write_report(rid, title, blocks, **kw)
    if not os.path.exists(path):
        fails.append(f"{rid}: report not written")
    return buf.getvalue()

def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)

try:
    out = run("ok", "日圓干預只延後了貶值，沒有扭轉它", [LEAD, KPI], type="research")
    check("WARNING" not in out, "compliant research report: no warning")
    out = run("long", "字" * 41, [LEAD, KPI], type="research")
    check("title is 82 wide" in out and "kpi_row" not in out, "41 CJK chars: title warning only")
    check(out.isascii(), "warning text is ASCII (Windows run.log is cp950)")
    check("WARNING" not in run("latin", "a" * 80, [LEAD, KPI], type="research"), "80 Latin chars: no warning")
    meta = {"type": "meta", "title": "字" * 41, "report_type": "一次性", "generated_at": 1756684800}
    check("title is 82 wide" in run("metattl", "short", [meta, LEAD, KPI], type="research"),
          "caller's meta.title (the rendered one) is what gets measured")
    check("no kpi_row" in run("nokpi", "t", [LEAD, {"type": "text", "markdown": "x"}, KPI], type="research"),
          "kpi_row not right after lead: warns")
    check("no kpi_row" not in run("nolead", "t", [KPI], type="research"), "no lead, kpi_row right after meta: quiet")
    check("WARNING" not in run("morning", "字" * 60, [LEAD], type="morning"), "morning report: never warns")
finally:
    shutil.rmtree(WS, ignore_errors=True)

print("\nALL OK" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)
