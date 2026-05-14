import numpy as np
import pandas as pd


def add_realized_vol(df, lookback=720, periods_per_year=8760):
    """Compute rolling realized volatility and add as df['realized_vol'] in-place."""
    log_ret = np.log(df['Close'] / df['Close'].shift(1))
    df['realized_vol'] = log_ret.rolling(lookback).std() * np.sqrt(periods_per_year)


def apply_vol_scaling(signal, df, target_vol=0.10, vol_cap=2.0):
    """Scale signal by vol targeting. signal × (target_vol / realized_vol)."""
    vol = df.get('realized_vol', pd.Series(np.nan, index=df.index))
    scale = (target_vol / vol).clip(upper=vol_cap)
    return signal * scale
