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
Never put an allocator in `manager/` — that directory takes no new files
(see `manager.md`).

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
  method that uses them.

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

```bash
# 1. create
cp allocators/TEMPLATE.py allocators/my_method/allocator.py   # mkdir first

# 2. gate — walk-forward vs 1,000 random portfolios
python3 manager/management_backtest.py --allocator my_method
#    → allocators/my_method/stats.json + pnl.png
#    read "Managed beats N% of ... random portfolios on Sharpe"

# 3. propose — dry-run, portfolio_config.json untouched
python3 manager/manager.py --allocator my_method

# 4. apply — ONLY after the user confirms the weights (same rule as always)
python3 manager/manager.py --allocator my_method --apply
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
import numpy as np
from scipy.optimize import minimize

from manager import portfolio_slope_std  # manager/ is on sys.path — see above

DISPLAY_NAME = "斜率/波動(有上限)"
DESCRIPTION  = "內建 slope/std 最佳化,但單一策略權重不超過上限"
PARAMS       = {"max_weight": 0.30}


def allocate(returns, lookback):
    names = list(returns.columns)
    n     = len(names)
    cap   = float(PARAMS["max_weight"])
    # A cap below 1/n makes sum(w) = 1 impossible — raise instead of letting
    # SLSQP return weights that do not sum to 1 (a wrong live position size).
    if cap * n < 1.0:
        raise ValueError(f"max_weight {cap} × {n} strategies < 1 "
                         f"(cap must be >= {1.0 / n:.4f})")

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
