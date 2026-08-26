"""Allocator template — copy to allocators/<name>/allocator.py and edit.

An allocator decides HOW MUCH of the portfolio each strategy gets. It is the
pluggable alternative to the built-in methods in manager/manager.py (`equal`
and `slope`) — **those two names are reserved**, so never call your directory
`allocators/equal/` or `allocators/slope/`; it would never be reached.

    cp allocators/TEMPLATE.py allocators/my_method/allocator.py

Then validate and use it:

    python3 manager/management_backtest.py --allocator my_method   # walk-forward vs random
    python3 manager/manager.py --allocator my_method               # dry-run weights
    python3 manager/manager.py --allocator my_method --apply       # write portfolio_config.json

As shipped this template is inverse-volatility weighting — it runs as-is, so a
copy is a working allocator before you change anything. It is deliberately NOT
equal weight: that is already the built-in `equal`, and a copy of it would just
put a second 等權 in the method picker.

Full contract: references/allocator-code.md
"""

# Human-facing name + one line you could read months later and still remember
# what this does. Same convention as a strategy's DISPLAY_NAME / DESCRIPTION.
DISPLAY_NAME = "反波動加權"
DESCRIPTION = "波動越大的策略給越小的權重"

# This method's own knobs. Edit the values here — they are NOT command-line
# flags, same as a strategy's top-of-file constants. The rule for `lookback`:
# if the method looks at history, it declares the window it looks at — that
# is what puts the field on the workspace page, and what the walk-forward
# holds out. A method that declares none is read as needing none (equal weight
# is the built-in example), and the backtest then treats every day as out of
# sample. `target_vol` is the one name that must never appear here (loading
# raises): it is the account's leverage target, not a weighting input.
PARAMS = {"lookback": 365, "floor_vol": 0.001}


def allocate(returns, lookback):
    """
    returns  DataFrame of daily strategy returns, one column per strategy.
             In the walk-forward backtest this is exactly `lookback` rows; from
             manager.py it is the full history, so slice with returns[-lookback:]
             if your method cares.
    lookback Walk-forward window in days — the same number as PARAMS["lookback"]
             (the scripts keep the two in sync), so read whichever is handier.

    Return   {strategy_name: weight}. Weights must be >= 0; they are normalised
             to sum 1 for you, so returning raw scores is fine. Omitting a
             strategy means zero weight. Raising is better than returning
             something you are unsure of — a wrong weight sizes a live position.
    """
    window = returns[-lookback:]
    # Raw scores are fine — clean() normalises them to sum 1 for you. The floor
    # keeps a flat stretch of returns from dividing by zero.
    return {name: 1.0 / max(float(window[name].std()), PARAMS["floor_vol"])
            for name in window.columns}
