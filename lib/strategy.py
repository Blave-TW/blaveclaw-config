import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).parent.parent


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


# ── Strategy versions (.claude/docs/strategy-versions.md) ─────────────────────
# One version = one BACKTEST of one STRATEGY_NAME. lib/runner.py mints them into
# strategies/<name>/versions/; the functions below read them back for the agent
# ("which version had the best Sharpe") and for the web's 還原 flow.

VERSIONS_KEEP = 20  # canon §8 — the api sweeps its own copy, the machine sends no DELETE


def versions_dir(name):
    """strategies/<name>/versions/ — index.json + v<N>.json + drift.json live here."""
    return _REPO_ROOT / 'strategies' / str(name) / 'versions'


def code_hash(src_bytes):
    """An index entry's `code_hash`: sha256 of the strategy file's raw bytes, first 16
    hex chars. Truncated because the whole entry is budgeted at ~200 bytes (canon §9)
    and 64 bits is far more than "is this file still what v<N> stored" needs."""
    return hashlib.sha256(src_bytes).hexdigest()[:16]


def load_index(name):
    """versions/index.json as a dict, or None when absent / unreadable / not an object.
    Fail-soft on purpose: both callers (the runner minting the next version, the live
    tick's drift check) must treat a missing or half-written index as "no versions yet"
    rather than fail the run."""
    try:
        with open(versions_dir(name) / 'index.json', encoding='utf-8') as f:
            idx = json.load(f)
    except (OSError, ValueError):
        return None
    return idx if isinstance(idx, dict) else None


def list_versions(name):
    """Every stored version of `name`, newest last — the summary entries, no code and no
    equity curve (open versions/v<N>.json for those). This is what answers 「這支策略有
    哪些版本 / 哪一版 Sharpe 最好」 from one small file instead of reading 20 blobs.

    Same shape contract as lib.report.list_schedules: a list of dicts, [] when the
    strategy has no versions, and one {"error": …} entry when the index is unreadable —
    never an exception, never a silent [] for a broken file (that reads as "no versions"
    and the agent would tell the user something false)."""
    path = versions_dir(name) / 'index.json'
    if not path.exists():
        return []
    try:
        with open(path, encoding='utf-8') as f:
            idx = json.load(f)
        items = idx['items']
        if not isinstance(items, list):
            raise ValueError('items is not a list')
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [{'error': f'bad index.json: {e}'}]
    return [i for i in items if isinstance(i, dict)]


def strategy_source_path(name):
    """The file a strategy's code lives in — strategies/<name>/strategy.py (dir layout)
    or strategies/<name>.py (single file), the two layouts the workspace enumerates —
    or None when neither exists."""
    for p in (_REPO_ROOT / 'strategies' / str(name) / 'strategy.py',
              _REPO_ROOT / 'strategies' / f'{name}.py'):
        if p.is_file():
            return p
    return None


def restore(name, v):
    """Copy version v's code back over the strategy file. Returns {"path", "version"}.

    Step 1 of the four the web's 還原 prompt asks for (references/strategy-code.md ›
    *Versions*): this call only puts the code back. Setting VERSION_NOTE to 「還原自 vN」
    (step 3) and re-running the backtest (step 4) are the agent's — a restored file left
    sitting on the previous run's stats.json is exactly the "v5's code, v7's numbers"
    state canon §5 exists to prevent, and the new version comes from that re-run.

    REFUSES, before touching any file, when the strategy is funded: a live strategy
    re-imports strategy.py every bar, so an in-place restore flips a real position with
    no warning (the 2026-08-05 incident's shape). Raises instead of returning a message
    so the refusal cannot be read as success, and reads the amounts through
    lib.portfolio.strategy_amounts() so the UI-authoritative mirror applies here too.
    An unreadable config raises out of here as well — the gate fails closed."""
    from lib.portfolio import strategy_amounts   # lazy: heavy module, only needed here
    # The gate below reads manager/ CWD-RELATIVE (load_portfolio_config, the UI-mirror
    # choke point). Called from anywhere else — an SSH BYO agent sitting in strategies/,
    # exactly the divergence canon §6 names — that read returns the empty default, the
    # gate passes, and a live strategy's file gets overwritten. Refuse instead.
    if Path.cwd().resolve() != _REPO_ROOT.resolve():
        raise RuntimeError(
            f"restore() must run from the workspace root ({_REPO_ROOT}) — the live-strategy "
            f"gate reads manager/portfolio_config.json relative to the working directory")
    amount = float(strategy_amounts().get(name) or 0)
    if amount > 0:
        raise ValueError(
            f"{name} is live (amount {amount:g} > 0) — restoring its code in place would "
            f"flip a real position on the next bar. Build the change as a new strategy "
            f"instead: references/strategy-code.md › Editing a live strategy.")
    try:
        with open(versions_dir(name) / f'v{int(v)}.json', encoding='utf-8') as f:
            blob = json.load(f)
        code = blob['code']
        if not isinstance(code, str) or not code:
            raise ValueError('no code stored')
    except FileNotFoundError:
        have = [i.get('n') for i in list_versions(name) if 'n' in i]
        raise FileNotFoundError(
            f"{name} has no v{v} on this machine — only the last {VERSIONS_KEEP} versions "
            f"are kept (have: {have or 'none'})") from None
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f"{name} v{v} is unreadable: {e}") from None
    path = strategy_source_path(name)
    if path is None:
        raise FileNotFoundError(f"strategy {name} has no source file to restore into")
    # newline='': write the stored code back byte for byte — the default would translate
    # every \n to \r\n on Windows, so a restore would silently reformat the whole file.
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(code)
    return {'path': str(path), 'version': int(v)}
