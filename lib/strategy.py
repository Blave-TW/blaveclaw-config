import numpy as np
import pandas as pd


def add_realized_vol(df, lookback=720, periods_per_year=8760):
    """Compute rolling realized volatility and add as df['realized_vol'] in-place."""
    log_ret = np.log(df['Close'] / df['Close'].shift(1))
    df['realized_vol'] = log_ret.rolling(lookback).std() * np.sqrt(periods_per_year)


def apply_vol_scaling(signal, df, target_vol=0.30, vol_cap=2.0):
    """Scale signal by vol targeting. signal × (target_vol / realized_vol)."""
    vol = df.get('realized_vol', pd.Series(np.nan, index=df.index))
    scale = (target_vol / vol).clip(upper=vol_cap)
    return signal * scale


def hysteresis(x, enter, exit, side=1):
    """One-sided threshold hysteresis, vectorized (no per-bar loop).

    side=+1 → long (1.0) once x > enter, flat (0.0) once x < exit, position held in
    between; side=-1 → short (-1.0) once x < enter, flat once x > exit. NaN bars hold
    the previous position; bars before the first signal are flat. Bar-for-bar identical
    to threshold_position() with the other side switched off; threshold_position()
    dispatches here itself whenever one side can never trigger, so a per-side scan
    gets the vectorized path without the caller knowing about this function.
    """
    x = pd.Series(x)
    if side >= 0:
        raw = np.where(x > enter, 1.0, np.where(x < exit, 0.0, np.nan))
    else:
        raw = np.where(x < enter, -1.0, np.where(x > exit, 0.0, np.nan))
    return pd.Series(raw, index=x.index).ffill().fillna(0.0)


def threshold_position(x, buy_th, sell_th, cover_th, short_th):
    """Two-sided four-threshold state machine → position Series of 1 / 0 / -1.

    long  when x > buy_th, exits to flat once x < sell_th;
    short when x < short_th, exits to flat once x > cover_th.
    Exit is checked before entry, so a bar that leaves one side's hold band and crosses
    the opposite entry flips in one bar. The flat band is bounded on both sides, which
    makes the position history-dependent — a vectorized where/ffill cannot express it
    (a gap across the band would keep the stale side), hence the loop. NaN holds.

    The loop costs ~0.5 s per 390k bars (5-min since 2023, Lightsail medium) — fine once
    per backtest, not per scan_grid cell. A side whose entry no bar ever reaches (the
    scan idiom: short_th=-1e9 / buy_th=1e9) can never hold a position, so the other side
    is a plain one-sided hysteresis and takes the vectorized path (~5 ms).
    """
    x = pd.Series(x)
    if not (x < short_th).any():         # short can never enter → long-only hysteresis
        return hysteresis(x, buy_th, sell_th, side=1)
    if not (x > buy_th).any():           # long can never enter → short-only hysteresis
        return hysteresis(x, short_th, cover_th, side=-1)
    vals = x.tolist()                    # Python floats: 4-5× faster than np.float64 scalars
    out = np.zeros(len(vals))
    pos = 0
    for i, xi in enumerate(vals):
        if xi != xi:                     # NaN
            out[i] = pos
            continue
        if pos == 1 and xi < sell_th:
            pos = 0
        elif pos == -1 and xi > cover_th:
            pos = 0
        if pos == 0:
            if xi > buy_th:
                pos = 1
            elif xi < short_th:
                pos = -1
        out[i] = pos
    return pd.Series(out, index=x.index)
