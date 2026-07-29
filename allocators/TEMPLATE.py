"""Allocator template — copy to allocators/<name>/allocator.py and edit.

An allocator decides HOW MUCH of the portfolio each strategy gets. It is the
pluggable alternative to the built-in slope/std optimiser in manager/manager.py.

    cp allocators/TEMPLATE.py allocators/my_method/allocator.py

Then validate and use it:

    python3 manager/management_backtest.py --allocator my_method   # walk-forward vs random
    python3 manager/manager.py --allocator my_method               # dry-run weights
    python3 manager/manager.py --allocator my_method --apply       # write portfolio_config.json

As shipped this template is equal weight — it runs as-is, so a copy is a
working allocator before you change anything.

Full contract: references/allocator-code.md
"""

# Human-facing name + one line you could read months later and still remember
# what this does. Same convention as a strategy's DISPLAY_NAME / DESCRIPTION.
DISPLAY_NAME = "等權"
DESCRIPTION = "每檔策略給一樣的權重"

# This method's own knobs. Edit the values here — they are NOT command-line
# flags, same as a strategy's top-of-file constants. `lookback` is not one of
# them: it is the walk-forward window and is passed in by the caller.
PARAMS = {}


def allocate(returns, lookback):
    """
    returns  DataFrame of daily strategy returns, one column per strategy.
             In the walk-forward backtest this is exactly `lookback` rows; from
             manager.py it is the full history, so slice with returns[-lookback:]
             if your method cares.
    lookback Walk-forward window in days.

    Return   {strategy_name: weight}. Weights must be >= 0; they are normalised
             to sum 1 for you, so returning raw scores is fine. Omitting a
             strategy means zero weight. Raising is better than returning
             something you are unsure of — a wrong weight sizes a live position.
    """
    names = list(returns.columns)
    return {name: 1.0 for name in names}
