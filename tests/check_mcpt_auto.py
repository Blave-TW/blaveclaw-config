"""Minimal check for the automatic MCPT in lib/runner.py — no network, no api.
A synthetic 1h OHLCV frame + a trivial SMA-cross strategy goes through runner.run() in
backtest mode; asserts stats.json carries the three MCPT keys with the Distribution shape
the web reads (edges = counts+1, counts sum = n, everything finite), that MCPT=False writes
none, that MCPT_N is honoured, that a live tick carries all three over, and that scan_grid
never touches lib.validation.mcpt (monkeypatched counter).
Run: cd blaveclaw-config && MPLBACKEND=Agg .venv/bin/python tests/check_mcpt_auto.py
"""
import json, math, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MPLBACKEND", "Agg")
import numpy as np
import pandas as pd
import lib.runner as R
import lib.validation as V
from lib.param_scan import scan_grid

fails = 0
def check(cond, msg):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + msg); fails += (not cond)

tmp = tempfile.mkdtemp(prefix="mcpt-", dir=os.environ.get("SCRATCHPAD") or None)
R._REPO_ROOT = type(R._REPO_ROOT)(tmp)
V._REPO_ROOT = tmp
os.chdir(tmp)  # lib.execute save_state uses a relative strategies/ path

# ── synthetic data + trivial strategy ─────────────────────────────────────────
rng   = np.random.default_rng(7)
N     = 3000
idx   = pd.date_range("2025-01-01", periods=N, freq="1h")
close = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, N)))
DF    = pd.DataFrame({"Open": close * (1 + rng.normal(0, 0.001, N)), "High": close * 1.01,
                      "Low": close * 0.99, "Close": close, "Volume": 1.0}, index=idx)

def fetch(hdrs):
    return DF.copy()

def compute(df, fast=10, slow=50):
    f = df["Close"].rolling(fast).mean(); s = df["Close"].rolling(slow).mean()
    return (f > s).astype(float)

def cfg(name, **extra):
    c = {"MODE": "backtest", "STRATEGY_NAME": name, "SYMBOL": "SYN", "INTERVAL": "1h",
         "START": "2025-01-01", "FEE": 0.0005, "WARMUP": 50}
    c.update(extra); return c

def stats_of(name):
    return json.load(open(os.path.join(tmp, "strategies", name, "stats.json")))

def dist_ok(d, n):
    return (isinstance(d, dict) and set(d) == {"edges", "counts", "actual"}
            and len(d["edges"]) == len(d["counts"]) + 1 == V.MCPT_DIST_BINS + 1
            and sum(d["counts"]) == n and all(isinstance(c, int) and c >= 0 for c in d["counts"])
            and all(isinstance(e, float) and math.isfinite(e) for e in d["edges"])
            and all(d["edges"][i] < d["edges"][i + 1] for i in range(len(d["edges"]) - 1))
            and isinstance(d["actual"], float) and math.isfinite(d["actual"]))

# ── 1. backtest writes the three keys ─────────────────────────────────────────
os.environ.pop("BLAVE_MODE", None)
R.run(cfg("auto"), fetch, compute)
st = stats_of("auto")
check(all(k in st for k in R.MCPT_KEYS), f"backtest 寫出三個 MCPT key → {[k for k in R.MCPT_KEYS if k in st]}")
check(isinstance(st["MCPT p-value"], float) and 0 <= st["MCPT p-value"] <= 1, f"p 值 float ∈ [0,1] → {st.get('MCPT p-value')}")
check(st["MCPT Permutations"] == R.MCPT_N_DEFAULT == 2000, f"預設 n = MCPT_N_DEFAULT = 2000 → {st.get('MCPT Permutations')}")
check(dist_ok(st["MCPT Distribution"], 2000), "Distribution 形狀對:edges=counts+1=31、counts 總和=n、全 finite、edges 遞增")
raw = open(os.path.join(tmp, "strategies", "auto", "stats.json")).read()
check("NaN" not in raw and "Infinity" not in raw, "檔內無 NaN / Infinity 字面值")
check(len(json.dumps(st["MCPT Distribution"])) < 1024, f"Distribution ≲ 1KB → {len(json.dumps(st['MCPT Distribution']))} bytes")
check(st["Sharpe Ratio"] is not None and "Generated At" in st and "trades" in st, "其他統計照寫")

# ── 2. MCPT_N override / MCPT=False ───────────────────────────────────────────
R.run(cfg("n300", MCPT_N=300), fetch, compute)
st = stats_of("n300")
check(st["MCPT Permutations"] == 300 and dist_ok(st["MCPT Distribution"], 300), "MCPT_N=300 覆寫 → Permutations=300、counts 總和=300")
R.run(cfg("off", MCPT=False), fetch, compute)
st = stats_of("off")
check(not any(k in st for k in R.MCPT_KEYS), "MCPT=False → 三個 key 都不寫")
check(st["Sharpe Ratio"] is not None and st["Trades"] > 0, "MCPT=False 其他統計照寫")

# ── 3. failure is a warning, not a failed backtest ────────────────────────────
R.run(cfg("flat"), fetch, lambda df: pd.Series(0.0, index=df.index))  # 0 trades
st = stats_of("flat")
check(not any(k in st for k in R.MCPT_KEYS) and st["Trades"] == 0, "0 筆交易 → 不寫 MCPT、回測照完成")
orig = V.mcpt
V.mcpt = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    R.run(cfg("boom"), fetch, compute); st = stats_of("boom")
    check(not any(k in st for k in R.MCPT_KEYS) and st["Sharpe Ratio"] is not None, "mcpt 例外 → 只 warning、其他統計照寫")
finally:
    V.mcpt = orig
V.mcpt = lambda *a, **k: (float("nan"), float("nan"), np.full(int(k.get("n", 5)), np.nan))
try:
    R.run(cfg("nan"), fetch, compute); st = stats_of("nan")
    check(not any(k in st for k in R.MCPT_KEYS), "mcpt 回 NaN → 不寫任何 MCPT key(api allow_nan=False)")
finally:
    V.mcpt = orig

# ── 4. live tick carries all three over ───────────────────────────────────────
before = stats_of("auto")
os.environ["BLAVE_MODE"] = "live"
try:
    R.run({**cfg("auto"), "MODE": "live"}, fetch, compute)
finally:
    os.environ.pop("BLAVE_MODE", None)
after = stats_of("auto")
check({k: after.get(k) for k in R.MCPT_KEYS} == {k: before[k] for k in R.MCPT_KEYS}, "live tick → 三個 MCPT key(含 Distribution)原樣帶過")
check(after["Generated At"] == before["Generated At"], "live tick → Generated At 帶過不重蓋")
sdir = os.path.join(tmp, "strategies", "auto")
check(R._carry_over(sdir, "live") == {k: before[k] for k in R.MCPT_KEYS + (R.GENERATED_AT_KEY,)}, "_carry_over(live) 回三 key + Generated At")
check(R._carry_over(sdir, "backtest") == {}, "_carry_over(backtest) 空 → 由 _auto_mcpt 重算")

# ── 5. scan_grid never calls mcpt ─────────────────────────────────────────────
calls = {"n": 0}
def counting(*a, **k):
    calls["n"] += 1; return orig(*a, **k)
V.mcpt = counting
try:
    grid = scan_grid(DF, compute, row_vals=[5, 10, 20], col_vals=[30, 50, 80],
                     row_param="fast", col_param="slow", fee=0.0005, warmup=80,
                     valid_fn=lambda r, c: r < c)
finally:
    V.mcpt = orig
check(grid.shape == (3, 3) and np.isfinite(grid).sum() >= 6, f"scan_grid 跑完 3×3 → {int(np.isfinite(grid).sum())} 格有值")
check(calls["n"] == 0, f"scan_grid 路徑完全不觸發 mcpt → 呼叫次數 {calls['n']}")
src = open(os.path.join(os.path.dirname(R.__file__), "param_scan.py")).read()
check("mcpt" not in src.lower(), "lib/param_scan.py 原始碼不引用 mcpt")

# ── 6. manual write_mcpt_to_stats with / without distribution ─────────────────
a, p, d = orig(DF["Close"].values[50:], compute(DF).iloc[50:].values, n=200, fee=0.0005, periods_per_year=8760)
V.write_mcpt_to_stats("off", p, len(d), d, a)
st = stats_of("off")
check(st["MCPT Permutations"] == 200 and dist_ok(st["MCPT Distribution"], 200), "手動 write_mcpt_to_stats(含 dist, actual) → 三 key、形狀對")
V.write_mcpt_to_stats("off", 0.5, 77)
st = stats_of("off")
check(st["MCPT Permutations"] == 77 and "MCPT Distribution" not in st, "手動不帶 dist → 只寫 p+n,舊 Distribution 移除(不留別次的直方圖)")
for bad in ((d, None), (None, a)):
    try:
        V.write_mcpt_to_stats("off", 0.5, 200, *bad); check(False, "dist/actual 只給一個要 raise")
    except ValueError:
        check(True, "dist/actual 只給一個要 raise")
try:
    V.write_mcpt_to_stats("off", p, 999, d, a); check(False, "n ≠ len(dist) 要 raise")
except ValueError:
    check(True, "n ≠ len(dist) 要 raise")
try:
    V.mcpt_stats_fields(1.0, 0.5, np.array([0.1, np.nan])); check(False, "dist 含 NaN 要 raise")
except ValueError:
    check(True, "dist 含 NaN 要 raise")
f = V.mcpt_stats_fields(0.7, 0.3, np.full(50, 1.234))
check(dist_ok(f["MCPT Distribution"], 50), "全同值 dist(退化)→ histogram 仍合法")
check(set(f) == set(R.MCPT_KEYS) == {V.MCPT_P_KEY, V.MCPT_N_KEY, V.MCPT_DIST_KEY}, "runner MCPT_KEYS 與 validation key 常數一致")

print("all checks passed" if not fails else f"FAILED: {fails}"); sys.exit(1 if fails else 0)
