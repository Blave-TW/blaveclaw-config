"""
Static security analysis for marketplace strategies.

Usage:
    python3 lib/security_check.py strategies/xyz.py

Exit codes:
    0 — clean
    1 — warnings only (review before running)
    2 — critical issues (do NOT run)
"""

import ast
import re
import sys
from pathlib import Path
from typing import Optional

ALLOWED_DOMAINS = {
    "api.blave.org",
    "api.binance.com",
    "fapi.binance.com",
    "dapi.binance.com",
    "api.bybit.com",
    "open-api.bingx.com",
    "api-cloud.bitmart.com",
    "api.bitfinex.com",
    "api.telegram.org",
}

# Prompt injection keywords in comments
_INJECTION_RE = re.compile(
    r"#.*\b(ignore|override|forget|disregard|system prompt|new instruction)\b",
    re.IGNORECASE,
)

# Obfuscation: decode-then-exec pattern within a 3-line window
_DECODE_RE = re.compile(r"(base64\.b64decode|bytes\.fromhex|codecs\.decode|\.decode\(['\"]utf)")
_EXEC_RE = re.compile(r"\b(eval|exec|compile)\s*\(")


def check(filepath: str) -> list[dict]:
    """Return list of findings: {level: 'CRITICAL'|'WARNING', line: int, msg: str}"""
    source = Path(filepath).read_text(encoding="utf-8")
    findings = []

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [{"level": "CRITICAL", "line": 0, "msg": f"Cannot parse file: {e}"}]

    findings += _ast_checks(tree)
    findings += _regex_checks(source)
    return sorted(findings, key=lambda f: f["line"])


# ── AST checks ────────────────────────────────────────────────────────────────

def _ast_checks(tree: ast.AST) -> list[dict]:
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in ("eval", "exec", "compile", "__import__"):
                findings.append(_c(node.lineno, f"{name}() — arbitrary code execution"))
            elif name in ("os.system", "os.popen"):
                findings.append(_c(node.lineno, f"{name}() — shell execution"))
            elif name and name.startswith("subprocess."):
                findings.append(_c(node.lineno, f"{name}() — shell execution"))
            elif name == "open":
                _check_open(node, findings)

        elif isinstance(node, ast.Attribute):
            # os.environ used as a whole object (not subscript, not .get/.setdefault)
            if (
                node.attr == "environ"
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                parent = getattr(node, "_parent", None)
                if not isinstance(parent, (ast.Subscript, ast.Attribute)):
                    findings.append(_w(node.lineno, "os.environ referenced — verify only specific keys are read"))

        # writing to lib/ via string literal path
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if re.search(r"\blib/", node.value):
                findings.append(_w(node.lineno, f"string path contains 'lib/' — possible write to shared lib: {node.value!r}"))

    # attach parent refs so the environ check above can inspect context
    _attach_parents(tree)
    return findings


def _check_open(node: ast.Call, findings: list) -> None:
    mode = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    else:
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value
    if mode and any(c in mode for c in "wxa"):
        findings.append(_w(node.lineno, f"open(..., {mode!r}) — verify write target is within workspace"))


def _attach_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node  # type: ignore[attr-defined]


def _call_name(node: ast.Call) -> Optional[str]:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts, cur = [], func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


# ── Regex checks ──────────────────────────────────────────────────────────────

def _regex_checks(source: str) -> list[dict]:
    findings = []
    lines = source.splitlines()

    for i, line in enumerate(lines, 1):
        # Obfuscation: decode + exec within 3 lines
        if _DECODE_RE.search(line):
            window = "\n".join(lines[i - 1 : min(len(lines), i + 2)])
            if _EXEC_RE.search(window):
                findings.append(_c(i, "obfuscated execution: decode + exec/eval pattern"))

        # Non-whitelisted external URLs
        for domain in re.findall(r"https?://([^/\s'\"]+)", line):
            base = domain.split(":")[0].lstrip("www.")
            if base not in ALLOWED_DOMAINS:
                findings.append(_w(i, f"external URL to non-whitelisted domain: {domain}"))

        # Prompt injection in comments
        if _INJECTION_RE.search(line):
            findings.append(_w(i, f"possible prompt injection in comment: {line.strip()!r}"))

        # os.environ without key access (whole dict)
        if re.search(r"os\.environ(?!\s*[\[.])", line):
            findings.append(_w(i, "os.environ used without key — may expose all credentials"))

    return findings


# ── Helpers ───────────────────────────────────────────────────────────────────

def _c(line: int, msg: str) -> dict:
    return {"level": "CRITICAL", "line": line, "msg": msg}

def _w(line: int, msg: str) -> dict:
    return {"level": "WARNING", "line": line, "msg": msg}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 lib/security_check.py <strategy_file.py>")
        sys.exit(0)

    results = check(sys.argv[1])

    if not results:
        print("✅ No issues found.")
        sys.exit(0)

    criticals = [r for r in results if r["level"] == "CRITICAL"]
    warnings  = [r for r in results if r["level"] == "WARNING"]

    print(f"{'❌' if criticals else '⚠️ '} {len(results)} issue(s) found in {sys.argv[1]}:\n")
    for r in results:
        icon = "❌" if r["level"] == "CRITICAL" else "⚠️ "
        print(f"  {icon} Line {r['line']}: {r['msg']}")

    print()
    if criticals:
        print("❌ CRITICAL issues — do NOT run this strategy without manual review.")
        sys.exit(2)
    else:
        print("⚠️  Warnings only — confirm with user before running.")
        sys.exit(1)
