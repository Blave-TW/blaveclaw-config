"""
Static quality analysis for Type A / Type C strategy files — catches a broken or
unfilled compute_signals() contract, backtest fee-drag gaming (FEE=0), a
TAIFEX futures strategy missing the mandatory txf_settlement_mask, and an
indicator-driven Type A strategy without a PLOT_SERIES declaration, before a
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

    findings = (
        _check_fee(tree) + _check_compute_signals(tree)
        + _check_txf_settlement_mask(tree) + _check_plot_series(tree)
    )
    return sorted(findings, key=lambda f: f["line"])


def _parse_for_runner(filepath: str):
    # The backtest runner (lib/runner.py) calls the single-check entry points
    # below on the file that is *executing* — it obviously parses, and the full
    # CLI already reports read/parse problems as CRITICAL, so return None here.
    try:
        return ast.parse(Path(filepath).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None


def txf_settlement_findings(filepath: str) -> list[dict]:
    """TAIFEX settlement-mask check only — the runner's blocking guard."""
    tree = _parse_for_runner(filepath)
    return _check_txf_settlement_mask(tree) if tree is not None else []


def plot_series_findings(filepath: str) -> list[dict]:
    """PLOT_SERIES check only — the runner's non-blocking backtest hint."""
    tree = _parse_for_runner(filepath)
    return _check_plot_series(tree) if tree is not None else []


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


# ── TAIFEX settlement-mask check ────────────────────────────────────────────────

# Only the R1 continuous-series fetchers — the ones whose roll gap fabricates
# PnL. Auxiliary TAIFEX feeds (pcr, bid_ask_vol) and the per-contract-month
# fetch_stock_futures_batch_daily deliberately do NOT trigger: a strategy may
# use them as indicators while trading a non-TAIFEX symbol.
_TWFUTURES_PRICE_FETCHERS = {
    "fetch_twfutures_ohlcv",
    "fetch_twfutures_ohlcv_batch",
}
_TAIFEX_INDEX_SYMBOLS = {"TXF", "MXF", "TMF"}

_MASK_FIX = (
    "Fix in compute_signals: `settle = txf_settlement_mask(df.index); "
    "signal[settle] = 0.0; return signal, settle` (Type C: `weights.loc[settle] = 0.0` "
    "and return settle as exec_at_close). See references/lib.md › txf_settlement_mask "
    "and strategies/txf_composite_60m/strategy.py for a real example."
)


def _call_name(node: ast.Call):
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _check_txf_settlement_mask(tree: ast.AST) -> list[dict]:
    # A strategy is treated as TAIFEX if it fetches a TAIFEX price series, or —
    # covering fetches hidden behind a helper module — declares SYMBOL as an
    # index-futures contract. Stock futures (e.g. 'CDF') can't be enumerated
    # statically, but they necessarily fetch via fetch_twfutures_* so the call
    # trigger covers them.
    trigger = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in _TWFUTURES_PRICE_FETCHERS:
            trigger = (node.lineno, f"calls {_call_name(node)}()")
            break
    if trigger is None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if (
                any(isinstance(t, ast.Name) and t.id == "SYMBOL" for t in targets)
                and isinstance(value, ast.Constant)
                and value.value in _TAIFEX_INDEX_SYMBOLS
            ):
                trigger = (node.lineno, f"SYMBOL = '{value.value}'")
                break
    if trigger is None:
        return []

    mask_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _call_name(n) == "txf_settlement_mask"
    ]
    if not mask_calls:
        return [_c(
            trigger[0],
            f"TAIFEX futures strategy ({trigger[1]}) without txf_settlement_mask — "
            "the data is an unadjusted continuous series; every monthly roll books "
            "the contract-basis gap as fake PnL (~+4%/yr × gross exposure) and the "
            "backtest omits real roll fees. " + _MASK_FIX,
        )]

    # Cargo-cult guard: calling the mask but throwing the result away. Only the
    # bare-expression-statement form is detectable statically; assigning the mask
    # without actually zeroing the signal with it passes this check.
    discarded = {
        id(s.value) for s in ast.walk(tree)
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
    }
    if all(id(c) in discarded for c in mask_calls):
        return [_c(
            mask_calls[0].lineno,
            "txf_settlement_mask() is called but its result is discarded — the "
            "mask must zero the signal and be returned as exec_at_close. " + _MASK_FIX,
        )]
    return []


# ── PLOT_SERIES declaration check (Type A) ──────────────────────────────────────

# Fetchers that return the traded instrument's own price/OHLCV (or pure metadata):
# calling one says nothing about indicators. Any OTHER lib.data fetch_* call
# (alpha feeds, twstock institutional / broker / PER, TAIFEX pcr, a spot index
# used for basis …) pulls an exogenous series the signal is almost certainly
# built on — that is the "external indicator" case the rule targets.
_PRICE_OR_META_FETCHERS = {
    "fetch_data",  # the strategy's own entry point
    "fetch_kline", "fetch_kline_batch", "fetch_bingx_kline", "fetch_db_kline",
    "fetch_twstock_price", "fetch_twstock_price_adj",
    "fetch_twstock_price_batch", "fetch_twstock_price_adj_batch",
    "fetch_twstock_ohlcv", "fetch_twstock_quote", "fetch_twstock_quote_batch",
    "fetch_twfutures_ohlcv", "fetch_twfutures_ohlcv_batch",
    "fetch_stock_futures_batch_daily",
    "fetch_twstock_ohlcv_symbols", "fetch_stock_futures_ohlcv_symbols",
    "fetch_twstock_list", "fetch_twstock_info", "fetch_economic_calendar",
}
# Window computations — the self-computed indicator case. shift/diff/pct_change
# deliberately do NOT count: "Close above yesterday's Close" is a pure price rule.
_INDICATOR_METHODS = {"rolling", "ewm"}

_PLOT_SERIES_FIX = (
    "Declare the 1–2 df columns that explain the entries/exits in the config "
    "section — e.g. PLOT_SERIES = {\"Taker Intensity 24h\": \"TI\"} (oscillator, "
    "sub-pane) or {\"SMA fast\": (\"SMA_F\", {\"overlay\": True})} (price units, "
    "overlay); the column must exist in the df fetch_data returns. See AGENTS.md › "
    "Backtest Output, references/plot-series.md, examples/btc_ti_5min/strategy.py."
)


def _assigns(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return True
    return False


def _check_plot_series(tree: ast.AST) -> list[dict]:
    # Type A = single-symbol config. Type C files declare UNIVERSE instead of
    # SYMBOL and have no single trade chart to overlay, so they are skipped.
    if not _assigns(tree, "SYMBOL") or _assigns(tree, "UNIVERSE"):
        return []
    if _assigns(tree, "PLOT_SERIES"):
        return []

    trigger = next(
        ((n.lineno, "defines _add_indicators()") for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_add_indicators"),
        None,
    )
    if trigger is None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name in _INDICATOR_METHODS or (
                name and name.startswith("fetch_") and name not in _PRICE_OR_META_FETCHERS
            ):
                trigger = (node.lineno, f"calls {name}()")
                break
    if trigger is None:
        return []  # pure price rule — PLOT_SERIES is optional

    return [_w(
        trigger[0],
        f"indicator-driven Type A strategy ({trigger[1]}) without PLOT_SERIES — "
        "the web workspace chart gets no indicator pane, so the user cannot see "
        "why it traded. " + _PLOT_SERIES_FIX,
    )]


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
