"""
Static quality analysis for Type A / Type C strategy files — catches a broken or
unfilled compute_signals() contract and backtest fee-drag gaming (FEE=0) before a
strategy is submitted to the marketplace or run after being purchased.

Usage:
    python3 lib/quality_check.py strategies/xyz.py

Exit codes:
    0 — clean
    1 — warnings only (review before running/submitting)
    2 — critical issues, file unreadable, or no file argument (do NOT run/submit)
"""

import ast
import sys
from pathlib import Path


def check(filepath: str) -> list[dict]:
    """Return list of findings: {level: 'CRITICAL'|'WARNING', line: int, msg: str}"""
    try:
        source = Path(filepath).read_text(encoding="utf-8")
    except OSError as e:
        return [{"level": "CRITICAL", "line": 0, "msg": f"Cannot read file: {e}"}]

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [{"level": "CRITICAL", "line": 0, "msg": f"Cannot parse file: {e}"}]

    findings = _check_fee(tree) + _check_compute_signals(tree)
    return sorted(findings, key=lambda f: f["line"])


# ── FEE=0 gaming check ──────────────────────────────────────────────────────────

def _check_fee(tree: ast.AST) -> list[dict]:
    findings = []
    for node in ast.walk(tree):
        # cover both `FEE = 0` (Assign) and `FEE: float = 0` (AnnAssign)
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue

        for target in targets:
            if not (isinstance(target, ast.Name) and target.id == "FEE"):
                continue
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, (int, float))
                and value.value == 0
            ):
                # WARNING, not CRITICAL — zero-fee venues exist and the user may
                # deliberately test without fee drag; surface it, don't block.
                findings.append(_w(
                    node.lineno,
                    "FEE=0 — backtest reports zero transaction cost, inflating "
                    "apparent Sharpe/return. Confirm the venue genuinely charges "
                    "none; otherwise use a realistic fee (e.g. 0.0005).",
                ))
            elif not isinstance(value, ast.Constant):
                # FEE = 0.0*1, FEE = X if Y else Z, … — can't statically verify;
                # an expression here is itself suspicious for a config constant
                findings.append(_w(
                    node.lineno,
                    "FEE is not a plain numeric constant — verify it evaluates to "
                    "a realistic nonzero fee (a computed FEE can hide FEE=0).",
                ))
    return findings


# ── compute_signals contract check ──────────────────────────────────────────────

def _check_compute_signals(tree: ast.AST) -> list[dict]:
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "compute_signals"),
        None,
    )
    if fn is None:
        return [_c(0, "compute_signals() not found — Type A/C strategies must define it "
                      "(see TEMPLATE_A.py / TEMPLATE_C.py)")]

    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    if not returns or all(r.value is None for r in returns):
        return [_c(fn.lineno, "compute_signals() has no return value — must return a pd.Series "
                              "(or a (pd.Series, exec_at_close) tuple, see references/strategy-code.md)")]

    # Unfilled-template heuristic: real signal logic virtually always contains a
    # comparison (threshold, crossover, rank filter) or a stateful loop somewhere
    # in the strategy's functions. The TEMPLATE stubs have neither (commented-out
    # logic / NotImplementedError). Checked across all function bodies — not just
    # compute_signals, because Type A/C conventionally split logic into helpers
    # (_add_indicators, _compute_weights) — but NOT at module level, where the
    # boilerplate `if __name__ == '__main__'` is itself a Compare node. WARNING
    # only — a strategy whose comparisons all live in numpy calls could trip this.
    has_logic = any(
        isinstance(n, (ast.Compare, ast.For, ast.While))
        for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
        for n in ast.walk(f)
    )
    if not has_logic:
        return [_w(fn.lineno, "no comparison or loop found anywhere in the file — looks like "
                              "the TEMPLATE stub logic was left unfilled")]

    # TEMPLATE_C's stub helpers are explicit: `raise NotImplementedError`. Any
    # reachable one left in a strategy file means an unfilled template section
    # (the shipped _rebalance_mask helper is real logic, so the Compare
    # heuristic above alone can't see this).
    findings = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Raise) and n.exc is not None:
            exc = n.exc.func if isinstance(n.exc, ast.Call) else n.exc
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                findings.append(_w(n.lineno, "raise NotImplementedError — a TEMPLATE stub "
                                             "section was left unfilled"))
    return findings


# ── Helpers ───────────────────────────────────────────────────────────────────

def _c(line: int, msg: str) -> dict:
    return {"level": "CRITICAL", "line": line, "msg": msg}

def _w(line: int, msg: str) -> dict:
    return {"level": "WARNING", "line": line, "msg": msg}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        # exit 2, not 0 — a missing argument must never read as a passing scan
        print("Usage: python3 lib/quality_check.py <strategy_file.py>")
        sys.exit(2)

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
        print("❌ CRITICAL issues — do NOT run/submit this strategy without fixing them.")
        sys.exit(2)
    else:
        print("⚠️  Warnings only — confirm with user before running/submitting.")
        sys.exit(1)
