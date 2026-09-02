"""Minimal check for lib/param_scan.write_scan + lib/validation.write_mcpt_to_stats —
no network, no api. A fake 5×9 Sharpe grid (NaN cells, a sharp isolated peak that is NOT
the plateau) goes through find_plateau → write_scan; asserts the scan.json contract the
web 穩健參數 tab reads (keys, shapes, NaN → null, peak ≠ plateau, current on/off grid).
Then a stub stats.json takes the MCPT merge and must keep every other field.
Run: cd blaveclaw-config && .venv/bin/python tests/check_param_scan.py
"""
import json, math, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from lib.param_scan import find_plateau, write_scan, _locate
import lib.validation as V

fails = 0
def check(cond, msg):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + msg); fails += (not cond)

tmp = tempfile.mkdtemp(prefix="scan-", dir=os.environ.get("SCRATCHPAD") or None)
row_vals = [0.5, 0.6, 0.7, 0.8, 0.9]                  # ENTRY_TH
col_vals = [round(0.1 * k, 1) for k in range(1, 10)]  # EXIT_TH 0.1..0.9
grid = np.full((5, 9), 0.4)
grid[3, 1] = 3.0                                      # isolated spike, neighbours stay 0.4
grid[1:4, 5:9] = 1.2                                  # broad 3×4 hill → the plateau
grid[0, 0] = np.nan; grid[4, 8] = np.nan; grid[2, 4] = np.nan   # invalid / never-traded

best_idx, nbr_mean, best_row, best_col, best_sharpe = find_plateau(grid, row_vals, col_vals)
check(tuple(best_idx) != (3, 1), "find_plateau 不選孤立尖峰 (3,1)")
check(1 <= best_idx[0] <= 3 and 5 <= best_idx[1] <= 8, f"plateau 落在平台區 → {tuple(int(x) for x in best_idx)}")

path = write_scan(grid, row_vals, col_vals, nbr_mean, best_idx, tmp,
                  row_param="ENTRY_TH", col_param="EXIT_TH", fee=0.0005,
                  start="2024-01-01", end="2026-08-31", current=(0.8, 0.2))
doc = json.load(open(path))
KEYS = {"row_param", "col_param", "row_vals", "col_vals", "grid", "nbr_mean", "peak", "plateau",
        "current", "fee", "start", "end", "window", "generated_at"}
check(os.path.basename(path) == "scan.json" and not os.path.exists(path + ".tmp"), "寫到 scan.json,無 .tmp 殘留")
check(set(doc) == KEYS, f"欄位名照契約 → 多/少: {set(doc) ^ KEYS or '無'}")
check(doc["row_param"] == "ENTRY_TH" and doc["col_param"] == "EXIT_TH", "row_param / col_param")
check(doc["row_vals"] == row_vals and doc["col_vals"] == col_vals, "row_vals / col_vals 原序")
check(len(doc["grid"]) == 5 and all(len(r) == 9 for r in doc["grid"]), "grid 5×9")
check(len(doc["nbr_mean"]) == 5 and all(len(r) == 9 for r in doc["nbr_mean"]), "nbr_mean 5×9")
check(doc["grid"][0][0] is None and doc["grid"][4][8] is None and doc["grid"][2][4] is None, "grid NaN → null")
check(doc["nbr_mean"][0][0] is None and doc["nbr_mean"][2][4] is None, "nbr_mean NaN → null(NaN 格自身無鄰域均值)")
check(all(v is None or isinstance(v, float) for r in doc["grid"] + doc["nbr_mean"] for v in r), "數值格皆 float 或 null")
check(doc["peak"] == {"i": 3, "j": 1, "sharpe": 3.0}, f"peak = 全域最高格 (3,1,3.0) → {doc['peak']}")
check((doc["plateau"]["i"], doc["plateau"]["j"]) == tuple(int(x) for x in best_idx)
      and math.isclose(doc["plateau"]["sharpe"], round(best_sharpe, 4)), "plateau = find_plateau best_idx,sharpe 為鄰域平均")
check(doc["peak"]["i"] != doc["plateau"]["i"] or doc["peak"]["j"] != doc["plateau"]["j"], "尖峰 ≠ 穩健點")
check(doc["current"] == {"i": 3, "j": 1, "vals": [0.8, 0.2]}, f"current 在網格上 → i/j 對應 → {doc['current']}")
check(doc["fee"] == 0.0005 and doc["start"] == "2024-01-01" and doc["end"] == "2026-08-31" and doc["window"] == 1, "fee / start / end / window")
check(isinstance(doc["generated_at"], int) and doc["generated_at"] > 1_700_000_000, "generated_at epoch 秒")
check("NaN" not in open(path).read(), "檔內沒有 NaN 字面值")

write_scan(grid, row_vals, col_vals, nbr_mean, best_idx, tmp, "ENTRY_TH", "EXIT_TH", 0.0005, None, None,
           current=(0.83, 0.2))
doc2 = json.load(open(path))
check(doc2["current"] == {"i": None, "j": None, "vals": [0.83, 0.2]}, f"current 不在網格 → i/j null、vals 保留 → {doc2['current']}")
check(doc2["start"] is None and doc2["end"] is None, "start/end 可為 null")
write_scan(grid, np.array(row_vals), np.array(col_vals), nbr_mean, best_idx, tmp, "ENTRY_TH", "EXIT_TH", 0.0005, None, None)
check(json.load(open(path))["current"] is None and json.load(open(path))["row_vals"] == row_vals, "沒傳 current → null;numpy vals 轉原生 list")
try:
    write_scan(grid[:4], row_vals, col_vals, nbr_mean, best_idx, tmp, "A", "B", 0.0, None, None); check(False, "形狀不合要 raise")
except ValueError:
    check(True, "形狀不合要 raise")
big_r = list(range(41)); big_g = np.full((41, 9), 0.5); d3 = tempfile.mkdtemp(prefix="scan-big-", dir=os.environ.get("SCRATCHPAD") or None)
try:
    write_scan(big_g, big_r, col_vals, big_g, (0, 0), d3, "A", "B", 0.0, None, None); check(False, "軸超過 40 要 raise(api 上限)")
except ValueError as e:
    check("40" in str(e), "軸超過 40 要 raise(api 上限)")
check(not os.path.exists(os.path.join(d3, "scan.json")), "超限時不寫 scan.json")
g40 = np.full((40, 40), 0.5); v40 = list(range(40))
b40, n40, *_ = find_plateau(g40, v40, v40)
write_scan(g40, v40, v40, n40, b40, d3, "A", "B", 0.0, None, None)
check(len(json.load(open(os.path.join(d3, "scan.json")))["grid"]) == 40, "剛好 40×40 可寫")

check(_locate(200.0, [199.998, 199.999, 200.0, 200.001]) == 2, "_locate 大數值細步長取最近格,不取第一個 isclose")
check(_locate(0.2, col_vals) == 1 and _locate(0.83, row_vals) is None, "_locate 命中 / 不在格上")
for bad_cur, why in (((True, 0.3), "current 含 bool"), ((float("nan"), 0.3), "current 含 NaN")):
    try:
        write_scan(grid, row_vals, col_vals, nbr_mean, best_idx, d3, "A", "B", 0.0, None, None, current=bad_cur); check(False, f"{why} 要 raise")
    except ValueError:
        check(True, f"{why} 要 raise")
try:
    write_scan(grid, [0.5, float("nan"), 0.7, 0.8, 0.9], col_vals, nbr_mean, best_idx, d3, "A", "B", 0.0, None, None); check(False, "row_vals 含 NaN 要 raise")
except ValueError:
    check(True, "row_vals 含 NaN 要 raise")
write_scan(grid, [0.1 * k for k in (5, 6, 7, 8, 9)], col_vals, nbr_mean, best_idx, d3, "A", "B", 0.0, None, None)
check(json.load(open(os.path.join(d3, "scan.json")))["row_vals"] == [0.5, 0.6, 0.7, 0.8, 0.9], "軸值 round(6) 去掉浮點雜訊")

# ── MCPT merge into stats.json ────────────────────────────────────────────────
V._REPO_ROOT = tmp
sdir = os.path.join(tmp, "strategies", "demo"); os.makedirs(sdir)
before = {"strategy": "demo", "Sharpe Ratio": 1.23, "Trades": 42, "daily_returns": [0.1, -0.2], "panes": [{"x": 1}]}
json.dump(before, open(os.path.join(sdir, "stats.json"), "w"), indent=2)
V.write_mcpt_to_stats("demo", 0.0345, 2000)
after = json.load(open(os.path.join(sdir, "stats.json")))
check(after["MCPT p-value"] == 0.0345 and after["MCPT Permutations"] == 2000, "MCPT 兩個 key 寫入")
check({k: after[k] for k in before} == before, "其他欄位原樣不動")
check(set(after) - set(before) == {"MCPT p-value", "MCPT Permutations"}, "只多這兩個 key")
check(not os.path.exists(os.path.join(sdir, "stats.json.tmp")), "stats.json 無 .tmp 殘留")
try:
    V.write_mcpt_to_stats("nope", 0.5, 10); check(False, "stats.json 不存在要 raise,不憑空造檔")
except FileNotFoundError:
    check(True, "stats.json 不存在要 raise,不憑空造檔")
check(not os.path.exists(os.path.join(tmp, "strategies", "nope", "stats.json")), "raise 後確實沒造檔")
for bad in (1.5, float("nan"), float("inf")):
    try:
        V.write_mcpt_to_stats("demo", bad, 10); check(False, f"p 值 {bad} 要 raise")
    except ValueError:
        check(True, f"p 值 {bad} 要 raise")
check(json.load(open(os.path.join(sdir, "stats.json")))["MCPT p-value"] == 0.0345, "raise 後 stats.json 不動")
check(len(doc["current"]["vals"]) == 2 and len(doc2["current"]["vals"]) == 2, "current.vals 固定長度 2")
check(all(math.isfinite(doc[k]["sharpe"]) and 0 <= doc[k]["i"] < 5 and 0 <= doc[k]["j"] < 9 for k in ("peak", "plateau")), "peak/plateau 完整物件、i/j 在範圍內、sharpe finite")
allnan = np.full((5, 9), np.nan); d2 = tempfile.mkdtemp(prefix="scan-nan-", dir=os.environ.get("SCRATCHPAD") or None)
try:
    find_plateau(allnan, row_vals, col_vals); check(False, "全 NaN grid find_plateau 要 raise")
except ValueError:
    check(True, "全 NaN grid find_plateau 要 raise")
try:
    write_scan(allnan, row_vals, col_vals, allnan, (0, 0), d2, "A", "B", 0.0, None, None); check(False, "全 NaN grid write_scan 要 raise")
except ValueError:
    check(True, "全 NaN grid write_scan 要 raise")
check(not os.path.exists(os.path.join(d2, "scan.json")), "全 NaN 時不寫 scan.json")
g_inf = grid.copy(); g_inf[0, 1] = np.inf
write_scan(g_inf, row_vals, col_vals, nbr_mean, best_idx, tmp, "ENTRY_TH", "EXIT_TH", 0.0005, None, None)
check(json.load(open(path))["grid"][0][1] is None and "Infinity" not in open(path).read(), "inf 格 → null,檔內無 Infinity")

# ── runner carry-over: live tick keeps MCPT + Generated At, backtest drops / restamps ──
import lib.runner as R
json.dump({**after, "Generated At": 1_700_000_000}, open(os.path.join(sdir, "stats.json"), "w"))
check(R._carry_over(sdir, "live") == {"MCPT p-value": 0.0345, "MCPT Permutations": 2000, "Generated At": 1_700_000_000}, "live 重寫 → 帶過 MCPT 兩 key + Generated At")
check(R._carry_over(sdir, "backtest") == {}, "backtest 重寫 → 全丟(舊 p 值對新參數無效,Generated At 重蓋)")
check(R._carry_over(os.path.join(tmp, "strategies", "nope"), "live") == {}, "舊檔不存在 → 當缺席、不致命")
bad = os.path.join(tmp, "strategies", "broken"); os.makedirs(bad); open(os.path.join(bad, "stats.json"), "w").write("{half-written")
check(R._carry_over(bad, "live") == {}, "舊檔壞掉 → 當缺席、不致命")
json.dump({"Sharpe Ratio": 1.0}, open(os.path.join(bad, "stats.json"), "w"))
check(R._carry_over(bad, "live") == {}, "舊檔沒有可帶 key → 空 dict,不帶其他欄位")
check(R.GENERATED_AT_KEY == "Generated At", "Generated At key 名固定")
src = open(os.path.join(os.path.dirname(R.__file__), "runner.py")).read()
check(src.count("stats.update(_carry_over(out_dir, mode))") == 1 and src.count("carried = _carry_over(out_dir, mode)") == 1
      and src.count("setdefault(GENERATED_AT_KEY, int(time.time()))") == 2, "Type A 與 Type C 兩處寫出都接了 _carry_over + Generated At 補戳")
print("all checks passed" if not fails else f"FAILED: {fails}"); sys.exit(1 if fails else 0)
