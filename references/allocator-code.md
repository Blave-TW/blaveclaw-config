# Allocator Code Structure

An **allocator** decides how much of the portfolio each strategy gets. The
slope/std optimiser inside `manager/manager.py` is the *default* one, not the
only one — a user with their own weighting idea writes it as a file and both
`manager.py` and `management_backtest.py` use it via `--allocator <name>`.

Strategies answer *when to be in*. Allocators answer *how much*. Same shape:
one directory per name, user-owned code, its own backtest output.

## Layout

```
allocators/
  TEMPLATE.py                    # tracked; copy this
  my_method/
    allocator.py                 # the method
    stats.json                   # written by management_backtest.py
    pnl.png                      # written by management_backtest.py
```

`allocators/*` is gitignored except `TEMPLATE.py`, same as `strategies/`.
Never put an allocator in `manager/` — the only new file that directory takes
is a custom executor (see `manager.md`).

**The directory name must match `[A-Za-z0-9_-]{1,64}`** — ASCII letters,
digits, `_` and `-`, nothing else. A name with a space or a Chinese character
still reports and still runs from the command line, but the workspace page
filters it out of the method dropdown, so the user can never select it. Put
the human-readable name in `DISPLAY_NAME`, which has no such restriction.

## Contract

```python
DISPLAY_NAME = "動能加權"                 # human-facing name
DESCRIPTION  = "用指數衰減的近期報酬當權重"   # one sentence
PARAMS       = {"half_life": 60}          # this method's own knobs

def allocate(returns, lookback):
    ...
    return {"btc_ti_24h": 0.6, "eth_ma_cross": 0.4}
```

- `returns` — DataFrame of daily strategy returns, one column per strategy.
  From `management_backtest.py` it is exactly `lookback` rows; from `manager.py`
  it is the full history, so slice `returns[-lookback:]` if the method cares.
- `lookback` — the walk-forward window, **supplied by the caller**. It is not
  one of the method's parameters; it defines the out-of-sample protocol both
  callers share.
- `PARAMS` — everything else the method exposes. Edit the values **in the
  file**, exactly like a strategy's top-of-file constants. There is
  deliberately no `--param` CLI flag: params are code, versioned with the
  method that uses them. **Values must be plain scalars** (int / float / bool /
  str): a list or dict is silently dropped from the workspace page's parameter
  form, so the user sees a method whose knob has vanished. A knob that is
  naturally a pair goes in as two scalar keys. And **validate the value inside
  `allocate()`** — the page lets the user type any number into the field, so
  the file's own default is no guarantee of what arrives.

### What the loader enforces (`lib/allocator.py`)

Return value goes through `clean()` before anything uses it:

| Input | Result |
|---|---|
| raw scores (any positive scale) | normalised to sum 1 — returning unnormalised is fine |
| a strategy omitted | weight 0 |
| a negative weight | **ValueError** |
| a name that is not a live strategy | **ValueError** |
| NaN, all-zero, non-dict | **ValueError** |

Negatives are rejected rather than clipped because they fail silently
downstream, not loudly: `manager.py` derives leverage from the volatility of
`weights > 0` only, and `lib/portfolio.py` computes
`contribution = account_value × leverage × weight × position` — a negative
weight flips that strategy's live position direction. **Shorting is expressed
by the strategy signal, never by the portfolio weight.**

A missing or malformed allocator raises too. There is no fallback to the
built-in optimiser — a typo'd `--allocator` name must not quietly trade on
different weights than the user asked for.

## Workflow

Validate before going live. A method that cannot beat randomly-drawn weights
out of sample does not get applied.

**Never run the walk-forward gate on your own.** It takes minutes — the user
sits watching a turn that looks stuck — and it runs on a member set they have
not chosen yet. It happens on their word, or when they press 跑回測 on the page
themselves. Step 4, `--apply`, needs their explicit confirmation as always.

**The dry-run in step 3 is yours to run**, and you should: it is seconds, it
writes nothing, and it is the honest way to satisfy `AGENTS.md` › *Verify, Then
Report* — it loads your file through the real path and prints the weights it
produces. Say which members it ran on (no `--members` means all of them).

Unlike a strategy, an allocator needs no run to become usable: the page lists
it from the file itself, so it is selectable the moment you save it. Its
`stats.json` is a report card, not an entry ticket.

Hand back with what you ran and what is still unrun, e.g. "「動能加權」寫好
了,乾跑一次(8 支全員)權重是 BTC 34% / ETH 31% / SOL 35%,看起來合理;走
查回測還沒跑。" — then tell them the two ways to run the gate: say so here, or
on the 自動下單 › 策略管理 page **pick the method from the 管理方法 dropdown
first** (yours appears under the custom group as its `DISPLAY_NAME`), then
choose members and press 跑回測. That first step matters: the page does not
switch to a new allocator by itself, so a user who skips it backtests the
built-in and reads the result as your method's.

```bash
# 1. create
cp allocators/TEMPLATE.py allocators/my_method/allocator.py   # mkdir first

# 2. gate — walk-forward vs 1,000 random portfolios. ONLY on the user's word.
#    --members is not optional: without it EVERY strategy with a stats.json
#    joins the portfolio, which is rarely the set the user means. (The page's
#    跑回測 button runs this same script and always sends --members, plus
#    --params-json when the method has PARAMS.)
python3 manager/management_backtest.py --members a,b,c --allocator my_method
#    → allocators/my_method/stats.json + pnl.png
#    read "Managed beats N% of ... random portfolios on Sharpe"

# 3. propose — dry-run, portfolio_config.json untouched. Run this yourself
#    right after step 1; it is the check that your file actually works.
python3 manager/manager.py --members a,b,c --allocator my_method

# 4. apply — ONLY after the user confirms the weights (same rule as always)
python3 manager/manager.py --members a,b,c --allocator my_method --apply
```

`--apply` writes `portfolio_config.json["allocator"] = "my_method"` alongside
the weights, so it is always visible which method produced the live weights.
Nothing downstream reads that field; it is there for the user and the workspace.

`management_backtest.py` needs more than `lookback` days of overlapping
strategy history (365 by default) — with less, it prints how many days it has
and stops. That is the usual blocker for a new portfolio, not a bug.

Self-check the contract logic any time with `python3 lib/allocator.py`.

## Writing one

Keep it deterministic and cheap. The walk-forward loop calls `allocate()` once
per out-of-sample day — a few thousand calls for a year — so anything that
fetches data or fits a heavy model per call turns a one-minute backtest into an
hour. Everything it needs is already in `returns`.

## Never edit the built-in

**`manager/manager.py` and `manager/management_backtest.py` are off limits.**
A new weighting idea — including "the built-in, but with X" — is always a new
`allocators/<name>/allocator.py`. No exceptions.

Editing the built-in looks cheaper and is not:

- **The workspace page can't see it.** Its method dropdown lists exactly the
  `allocators/<name>/` directories the machine reports. A new flag on
  `manager.py` is invisible there, and the page also refuses to pass extra
  params to the built-in — so the user can only reach it by typing a command.
- **It gets merged away.** Both scripts are tracked files; the next
  `blaveclaw-config` update compares them file by file against the reference
  clone, and a local edit is at the mercy of that merge.
- **It changes the default for everything.** The built-in is the one method
  every portfolio falls back to; a variant nobody asked for should not live
  inside it.

An allocator can reuse the built-in's parts instead of copying them. Both
callers put `manager/` on `sys.path` before loading your file, so
`from manager import portfolio_slope_std` (the objective) or `optimize_weights`
(the whole optimiser) imports — the same line `management_backtest.py` uses.
It is `from manager import ...`, never `from manager.manager import ...`:
`manager/` has no `__init__.py`, so the name resolves to the *module*
`manager/manager.py`, not to a package.

Worked example — the built-in slope/std with a ceiling on any one strategy:

```python
"""Built-in slope/std, with a ceiling on any single strategy's weight."""
import math

import numpy as np
from scipy.optimize import minimize

from manager import portfolio_slope_std  # manager/ is on sys.path — see above

DISPLAY_NAME = "斜率/波動(有上限)"
DESCRIPTION  = "內建 slope/std 最佳化,但單一策略權重不超過上限"
PARAMS       = {"max_weight": 0.30}   # 0.30 = 30%, a fraction — not 30


def allocate(returns, lookback):
    names = list(returns.columns)
    n     = len(names)
    cap   = PARAMS["max_weight"]

    # Validate the knob before using it. The page lets the user type any
    # number into a PARAMS field, and `30` (meaning 30%) would pass every
    # check below while making the bounds so wide the cap does nothing —
    # a method named "with a ceiling" silently running without one.
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 0 < cap <= 1:
        raise ValueError(f"max_weight must be a fraction in (0, 1], got {cap!r} "
                         f"(30% is 0.3, not 30)")
    cap = float(cap)
    # A cap below 1/n makes sum(w) = 1 infeasible, so every SLSQP start fails
    # and the fallback below hands back equal weight — 1/n each, every one of
    # them above the cap. Raise instead: a method named "with a ceiling" must
    # not quietly run without one.
    # Round the suggested floor UP: 1/3 printed as 0.3333 fails this same check.
    # With one strategy the only feasible cap is 1.0 — a ceiling is meaningless
    # there, so a single-member portfolio should use the built-in instead.
    if cap * n < 1.0:
        floor = math.ceil(1e4 / n) / 1e4
        raise ValueError(f"max_weight {cap} × {n} strategies < 1 "
                         f"(cap must be >= {floor})")

    matrix      = returns.values      # portfolio_slope_std slices to `lookback`
    objective   = lambda w: -portfolio_slope_std(w, matrix, lookback)
    constraints = [{"type": "eq", "fun": lambda w: w.sum() - 1}]
    bounds      = [(0.0, cap)] * n

    best, best_score = None, np.inf
    starts = [np.ones(n) / n] + [np.random.dirichlet(np.ones(n)) for _ in range(10)]
    for w0 in starts:
        res = minimize(objective, w0, method="SLSQP", bounds=bounds,
                       constraints=constraints, options={"ftol": 1e-9, "maxiter": 1000})
        if res.success and res.fun < best_score:
            best, best_score = res, res.fun

    w = np.ones(n) / n if best is None else np.clip(best.x, 0.0, cap)
    return {names[i]: float(w[i]) for i in range(n)}
```

Because the cap is a `PARAMS` key, the workspace page shows it as an editable
parameter — which a flag on `manager.py` could never be.
