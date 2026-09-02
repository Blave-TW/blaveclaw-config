import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np
from dotenv import dotenv_values
import strategy as s
from lib.validation import mcpt, plot_mcpt, write_mcpt_to_stats

env  = dotenv_values()
hdrs = {'api-key': env.get('blave_api_key', ''), 'secret-key': env.get('blave_secret_key', '')}

data  = s.fetch_data(hdrs)
df    = data.iloc[s.WARMUP:]
pos   = s.compute_signals(data).iloc[s.WARMUP:].ffill().fillna(0).values
close = df['Close'].values

print(f"Running MCPT for {s.STRATEGY_NAME} (n=2000, {len(close)} bars)...")
actual, p_value, dist = mcpt(
    close, pos,
    n=2000,
    fee=s.FEE,
    target_vol=0.30,
    max_lev=1.0,
    vol_window=60,
    periods_per_year=252,
)
sig = "*** p < 0.05: significant edge ***" if p_value < 0.05 else "p >= 0.05: no significant edge"
print(f"  MCPT Sharpe: {actual:.2f}  p-value: {p_value:.3f}  {sig}")
write_mcpt_to_stats(s.STRATEGY_NAME, p_value, len(dist))   # → stats.json,web 回測數據顯示 p 值
plot_mcpt(actual, dist,
          label=s.STRATEGY_NAME,
          output_path=str(Path(__file__).parent / 'mcpt.png'))
