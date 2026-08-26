"""Allocator loading — the pluggable half of portfolio weighting.

An allocator is one file, `allocators/<name>/allocator.py`, exposing:

    def allocate(returns, lookback) -> {strategy_name: weight}

`returns` is a DataFrame of daily strategy returns (one column per strategy);
`lookback` is the walk-forward window in days, always passed as `allocate()`'s
second argument. A method that FITS on that window owns it and declares it in
`PARAMS` like any other knob, which is what puts it in the page's parameter
form — the built-in `slope` does, `equal` does not. A method that declares no
window leaves the choice to the caller. `target_vol` is the one name a file
must never declare (see RESERVED_PARAM_KEYS): it is the account's leverage
target, not a weighting input. The method's own knobs live in a module-level
`PARAMS` dict, edited in the file, exactly like a strategy's top-of-file
constants (see `references/allocator-code.md`).

The two built-in methods are not files — `manager.py` holds them as functions
and picks one by name (`equal`, the default for a new portfolio, or `slope`;
omitting the flag keeps a live portfolio's own method), so this module
deliberately has no knowledge of them beyond refusing to load a file under
those names (importing `manager/manager.py` from `lib/` would need the same
sys.path hack the scripts do, for no benefit).
"""
import ast
import importlib.util
import os

ALLOCATORS_DIR = 'allocators'

# Reserved: these name the built-in methods on `--allocator`, so a directory
# with either name could never be reached — refuse it loudly at load time
# rather than let the user believe their file is the one being traded.
BUILTIN_ALLOCATORS = ('equal', 'slope')

# `target_vol` scales portfolio leverage and is not a weighting input at all —
# a file declaring it would have it read as the caller's flag and never see the
# page's value in its own PARAMS, so refuse the name.
#
# `lookback` is NOT reserved: a method that fits on a window owns that window,
# declares it like any other knob, and the page offers it. The scripts pass the
# resolved value to allocate() as well, so a method can read it either way.
RESERVED_PARAM_KEYS = ('target_vol',)


def allocator_path(name):
    return os.path.join(ALLOCATORS_DIR, name, 'allocator.py')


def load(name):
    """Import `allocators/<name>/allocator.py` and return the module.

    Raises rather than falling back: a typo'd allocator name must not silently
    trade on the built-in weights.
    """
    if name in BUILTIN_ALLOCATORS:
        raise ValueError(
            f"'{name}' is a built-in method, not a file — the manager scripts "
            f"handle it themselves. Rename allocators/{name}/ to something else."
        )
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
    declared = getattr(mod, 'PARAMS', None)
    if isinstance(declared, dict):
        clash = [k for k in RESERVED_PARAM_KEYS if k in declared]
        if clash:
            raise ValueError(
                f"{path} declares reserved PARAMS {clash} — target_vol is the "
                f"portfolio's leverage target, set once for the account, not a "
                f"weighting input a method gets to choose."
            )
    if uses_window(path) and not (isinstance(declared, dict) and 'lookback' in declared):
        raise ValueError(
            f"{path} reads the window argument but declares no PARAMS['lookback']. "
            f"A method that looks at history has to say how far: without the "
            f"declaration the scripts hand it no window at all (every day out of "
            f"sample), and it would be fitting on data nobody can see or set. "
            f"Add \"lookback\": <days> to PARAMS."
        )
    return mod


def uses_window(path):
    """Whether allocate() refers to its second argument anywhere in its body —
    the syntactic tell for "this method looks at history". Pure ast, never an
    import, so the reporter can apply the same rule to a file it will not run."""
    # Bytes, so ast honours a BOM or a coding cookie the way import does — a
    # text-mode read would raise on those, be swallowed, and let the file
    # through unguarded.
    try:
        with open(path, 'rb') as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError):
        return False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == 'allocate':
            params = [a.arg for a in node.args.posonlyargs + node.args.args]
            if len(params) < 2:
                return False
            window = params[1]
            return any(isinstance(n, ast.Name) and n.id == window
                       for n in ast.walk(node) if n is not node)
    return False


def apply_params(mod, overrides):
    """Override the module's PARAMS from the caller (manager scripts'
    --params-json, fed by the workspace page). Only keys the file already
    declares are accepted — an unknown key is a typo that would otherwise run
    the method with its defaults while the caller believes it changed them.
    """
    if not overrides:
        return
    if not isinstance(overrides, dict):
        raise TypeError('params override must be a dict')
    params = getattr(mod, 'PARAMS', None)
    if not isinstance(params, dict):
        raise ValueError('allocator declares no PARAMS dict to override')
    unknown = set(overrides) - set(params)
    if unknown:
        raise ValueError(f'unknown PARAMS keys: {sorted(unknown)} (declared: {sorted(params)})')
    params.update(overrides)


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

    # Quantise to whole basis points that add up to exactly 10000, rather than
    # rounding each share on its own — three equal strategies would otherwise
    # be 0.3333 each and the weights would not add up at all.
    #
    # The invariant is on the STORED weights, which is where it matters: they
    # size real positions. The percentages a page or a message prints round
    # again to fewer digits and need not visibly total 100.0 — that is ordinary
    # display rounding, not a broken allocation.
    #
    # Largest-remainder: the leftover bps go to the shares that were cut
    # hardest, so the correction is spread one bp at a time rather than dumped
    # on a single strategy (which could push it past a cap it set itself).
    order  = {k: i for i, k in enumerate(names)}
    exact  = {k: v / total * 10000 for k, v in weights.items()}
    bps    = {k: int(x) for k, x in exact.items()}          # floor; no negatives here
    short  = 10000 - sum(bps.values())
    if short:
        ranked = sorted(names, key=lambda k: (bps[k] - exact[k], order[k]))
        for k in ranked[:short]:
            bps[k] += 1
    return {k: bps[k] / 10000 for k in names}


if __name__ == '__main__':
    # Self-check for the contract above — `python3 lib/allocator.py`.
    # Every rejection here is a silently-wrong live position size if it stops
    # working, so this stays runnable rather than living in a scratch file.
    names = ['a', 'b', 'c']

    # Weights are shown to the user as percentages; they must add to 100.0,
    # so the invariant is on whole basis points, not on a float sum.
    # The invariant: the stored weights add up to exactly 1, checked on the
    # quantisation grid so float addition cannot mask a miss.
    def _bps(w):
        return sum(round(v * 10000) for v in w.values())

    assert clean({'a': 1, 'b': 1, 'c': 1}, names) == {'a': 0.3334, 'b': 0.3333, 'c': 0.3333}
    for n in range(1, 60):
        many = [f's{i}' for i in range(n)]
        assert _bps(clean({k: 1 for k in many}, many)) == 10000, f'equal weight, n={n}'
    for raw in ({'a': 7, 'b': 2, 'c': 1}, {'a': 1, 'b': 2}, {'a': 1e-9, 'b': 1},
                {'a': 1, 'b': 1, 'c': 1000000}, {'a': 1, 'b': 1e-12}):
        assert _bps(clean(raw, names)) == 10000, f'{raw} did not sum to 100%'
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

    for reserved in BUILTIN_ALLOCATORS:
        try:
            load(reserved)
        except ValueError:
            pass
        else:
            raise AssertionError(f'load() accepted the reserved name {reserved!r}')

    # Use the window, declare the window — the guard that keeps a history-based
    # method from silently running windowless.
    import tempfile
    saved = ALLOCATORS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        globals()['ALLOCATORS_DIR'] = tmp
        cases = {
            'undeclared': ('PARAMS = {}\ndef allocate(returns, lookback):\n'
                           '    return {c: 1 for c in returns[-lookback:].columns}\n', True),
            'declared':   ('PARAMS = {"lookback": 90}\ndef allocate(returns, lookback):\n'
                           '    return {c: 1 for c in returns[-lookback:].columns}\n', False),
            'windowless': ('PARAMS = {}\ndef allocate(returns, lb):\n'
                           '    return {c: 1 for c in returns.columns}\n', False),
        }
        for name, (src, should_raise) in cases.items():
            os.makedirs(os.path.join(tmp, name))
            with open(allocator_path(name), 'w') as f:
                f.write(src)
            try:
                load(name)
            except ValueError:
                assert should_raise, f'{name}: load() refused a compliant file'
            else:
                assert not should_raise, f'{name}: load() accepted a window user with no lookback'
        globals()['ALLOCATORS_DIR'] = saved

    import types
    fake = types.SimpleNamespace(PARAMS={'k': 1, 'x': 2.0})
    apply_params(fake, {'k': 5})
    assert fake.PARAMS == {'k': 5, 'x': 2.0}, 'apply_params must update declared keys only'
    for bad in ({'zz': 1}, 'k=1'):
        try:
            apply_params(fake, bad)
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f'apply_params accepted {bad!r}')

    print('lib/allocator.py self-check OK')
