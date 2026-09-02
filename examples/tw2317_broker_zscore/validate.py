# MANUAL MCPT — normally unnecessary: every Type A backtest already runs MCPT and writes
# "MCPT p-value" / "MCPT Permutations" / "MCPT Distribution" into stats.json (lib/runner.py,
# default n=2000; set MCPT_N in strategy.py to change it there). This file only shows the
# manual flow for when you want a specific permutation count, daily-stock vol-targeting
# parameters, or the mcpt.png chart. Never wire MCPT into scan.py — a scan never runs it.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np
from dotenv import dotenv_values
import strategy as s
from lib.validation import mcpt, plot_mcpt, write_mcpt_to_stats

N_PERM = 5000  # the one thing this script changes vs the automatic run

env  = dotenv_values()
hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

data  = s.fetch_data(hdrs)
df    = data.iloc[s.WARMUP:]
pos   = s.compute_signals(data).iloc[s.WARMUP:].ffill().fillna(0).values
close = df['Close'].values

print(f"Running MCPT for {s.STRATEGY_NAME} (n={N_PERM}, {len(close)} bars)...")
actual, p_value, dist = mcpt(
    close, pos,
    n=N_PERM,
    fee=s.FEE,
    target_vol=0.30,
    max_lev=1.0,
    vol_window=60,
    periods_per_year=252,
)
sig = "*** p < 0.05: significant edge ***" if p_value < 0.05 else "p >= 0.05: no significant edge"
print(f"  MCPT Sharpe: {actual:.2f}  p-value: {p_value:.3f}  {sig}")
# Overwrites the backtest's automatic MCPT keys with this run (p, n, and the histogram).
write_mcpt_to_stats(s.STRATEGY_NAME, p_value, len(dist), dist, actual)
plot_mcpt(actual, dist,
          label=s.STRATEGY_NAME,
          output_path=str(Path(__file__).parent / 'mcpt.png'))
