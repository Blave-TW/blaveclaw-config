"""Minimal check for the absent-day fill — `python3 manager/check_absent_fill.py`.

The two properties that go silently wrong if this breaks: a method fitting on
days a strategy did not exist (weights become optimiser noise again), and the
fill leaking into realised PnL (the walk-forward's random benchmark pays for
holding a strategy that never existed, flattering the managed result).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from manager import build_returns, ABSENT_FILL_ANNUAL
from management_backtest import rolling_managed_returns

PAD = ABSENT_FILL_ANNUAL / 365.0

# `old` runs the whole span, `young` starts late and `old` goes stale early —
# the two shapes that produce NaN. The explicit 0.0 on 2020-01-03 stands in for
# a non-trading day: lib/pnl writes those, so they are data, not absence.
valid = {
    'old':   {'daily_dates':   ['2020-01-01', '2020-01-02', '2020-01-03', '2020-01-04'],
              'daily_returns': [0.01, 0.02, 0.0, 0.03]},
    'young': {'daily_dates':   ['2020-01-03', '2020-01-04', '2020-01-05'],
              'daily_returns': [0.04, 0.05, 0.06]},
}
fit, real = build_returns(valid)

assert list(fit.index.strftime('%Y-%m-%d')) == ['2020-01-01', '2020-01-02', '2020-01-03',
                                                '2020-01-04', '2020-01-05']
assert list(fit.columns) == list(real.columns), 'the two frames must stay aligned'

# Absent days: charged while fitting, zero in the money.
assert fit.loc['2020-01-01', 'young'] == PAD, 'young is absent before it starts'
assert fit.loc['2020-01-05', 'old'] == PAD, 'old is absent after its backtest ends'
assert real.loc['2020-01-01', 'young'] == 0.0, 'absence must not cost money'
assert real.loc['2020-01-05', 'old'] == 0.0, 'absence must not cost money'

# A written 0 is data. Filling it would charge a strategy for not trading on a
# day its market was shut, every week, forever.
assert fit.loc['2020-01-03', 'old'] == 0.0, 'an explicit zero is a real return, not absence'

# Real days are untouched in both frames.
for f in (fit, real):
    assert f.loc['2020-01-02', 'old'] == 0.02
    assert f.loc['2020-01-04', 'young'] == 0.05

# The walk-forward earns on `real` and fits on `fit`. Pin a weight on the
# absent leg so the two frames give different answers, and check which one the
# managed return came from.
w = {'old': 0.5, 'young': 0.5}
managed, hist = rolling_managed_returns(fit, 4, lambda window, lb: dict(w), real_df=real)
assert len(managed) == 1 and managed.index[0].strftime('%Y-%m-%d') == '2020-01-05'
expected = 0.5 * 0.0 + 0.5 * 0.06          # old is absent that day: 0, not PAD
assert abs(managed.iloc[0] - expected) < 1e-12, (
    f'managed return {managed.iloc[0]!r} used the fitting frame, not the real one')
assert dict(hist.iloc[0]) == w

# The window handed to the method is the charged one.
seen = {}
rolling_managed_returns(fit, 4, lambda window, lb: seen.update(w=window) or dict(w),
                        real_df=real)
assert seen['w'].loc['2020-01-01', 'young'] == PAD, 'the method must see the charge'

# Defaulting real_df keeps a direct caller doing what it did before.
managed_one, _ = rolling_managed_returns(real, 4, lambda window, lb: dict(w))
assert abs(managed_one.iloc[0] - expected) < 1e-12

print('manager/check_absent_fill.py OK')
