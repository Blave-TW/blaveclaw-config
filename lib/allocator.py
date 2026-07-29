"""Allocator loading — the pluggable half of portfolio weighting.

An allocator is one file, `allocators/<name>/allocator.py`, exposing:

    def allocate(returns, lookback) -> {strategy_name: weight}

`returns` is a DataFrame of daily strategy returns (one column per strategy);
`lookback` is the walk-forward window in days, supplied by the caller — it is
NOT the method's own parameter. The method's own knobs live in a module-level
`PARAMS` dict, edited in the file, exactly like a strategy's top-of-file
constants (see `references/allocator-code.md`).

The built-in slope/std optimiser is not a file — `manager.py` uses its own
`optimize_weights` when no allocator is named, so this module deliberately has
no knowledge of it (importing `manager/manager.py` from `lib/` would need the
same sys.path hack the scripts do, for no benefit).
"""
import importlib.util
import os

ALLOCATORS_DIR = 'allocators'


def allocator_path(name):
    return os.path.join(ALLOCATORS_DIR, name, 'allocator.py')


def load(name):
    """Import `allocators/<name>/allocator.py` and return the module.

    Raises rather than falling back: a typo'd allocator name must not silently
    trade on the built-in weights.
    """
    path = allocator_path(name)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"{path} not found — copy allocators/TEMPLATE.py to create it"
        )
    spec = importlib.util.spec_from_file_location(f'allocator_{name}', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, 'allocate', None)):
        raise AttributeError(f"{path} defines no allocate(returns, lookback)")
    return mod


def clean(raw, names):
    """Validate an allocate() return value and normalise it to sum 1.

    Returns {name: weight} covering every strategy in `names` (absent = 0.0).

    Rejects loudly instead of coercing, because both failure modes produce
    silently wrong live position sizes rather than an error:
      - a negative weight survives into `manager.py`'s volatility calc, which
        only sums `weights > 0` — the leverage it derives would be wrong, and
        `lib/portfolio.py` would flip that strategy's position direction.
      - an unknown strategy name means the method is weighting something that
        does not exist; the remaining weights would silently not sum to 1.
    Unnormalised input IS accepted (raw scores are the natural output of most
    weighting schemes) — only the sum is fixed up, never the sign.
    """
    if not isinstance(raw, dict):
        raise TypeError(f'allocate() must return a dict, got {type(raw).__name__}')

    unknown = set(raw) - set(names)
    if unknown:
        raise ValueError(f'allocate() returned unknown strategies: {sorted(unknown)}')

    weights = {}
    for name in names:
        w = float(raw.get(name, 0.0))
        if w != w:  # NaN
            raise ValueError(f'allocate() returned NaN for {name}')
        if w < 0:
            raise ValueError(
                f'allocate() returned a negative weight for {name}: {w}. '
                f'Weights must be >= 0 — shorting is expressed by the strategy '
                f'signal, not by the portfolio weight.'
            )
        weights[name] = w

    total = sum(weights.values())
    if total <= 0:
        raise ValueError('allocate() returned all-zero weights')

    return {k: round(v / total, 4) for k, v in weights.items()}


if __name__ == '__main__':
    # Self-check for the contract above — `python3 lib/allocator.py`.
    # Every rejection here is a silently-wrong live position size if it stops
    # working, so this stays runnable rather than living in a scratch file.
    names = ['a', 'b', 'c']

    assert clean({'a': 1, 'b': 1, 'c': 1}, names) == {'a': 0.3333, 'b': 0.3333, 'c': 0.3333}
    assert clean({'a': 3, 'b': 1}, names) == {'a': 0.75, 'b': 0.25, 'c': 0.0}, 'raw scores normalise; absent = 0'

    for bad, why in [
        ({'a': -1, 'b': 2}, 'negative weight'),
        ({'a': 1, 'zz': 1}, 'unknown strategy'),
        ({'a': 0, 'b': 0}, 'all zero'),
        ({'a': float('nan')}, 'NaN'),
        ([('a', 1)], 'not a dict'),
    ]:
        try:
            clean(bad, names)
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f'clean() accepted {why}: {bad}')

    try:
        load('__no_such_allocator__')
    except FileNotFoundError:
        pass
    else:
        raise AssertionError('load() did not raise on a missing allocator')

    print('lib/allocator.py self-check OK')
