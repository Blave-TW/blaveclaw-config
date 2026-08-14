import os
import time
import threading
import requests
import pandas as pd
import pyarrow.parquet as pq
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


class _RateLimiter:
    """Token bucket: allows max_calls requests per period seconds."""
    def __init__(self, max_calls, period):
        self._max   = max_calls
        self._period = period
        self._calls  = []
        self._lock   = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.time()
            self._calls = [t for t in self._calls if now - t < self._period]
            if len(self._calls) >= self._max:
                wait = self._period - (now - self._calls[0])
                if wait > 0:
                    time.sleep(wait)
                    now = time.time()
                    self._calls = [t for t in self._calls if now - t < self._period]
            self._calls.append(time.time())

BASE      = 'https://api.blave.org'
_CACHE_DIR = Path(__file__).parent.parent / 'cache'


def _retry_get(url, max_retries=6, **kwargs):
    """GET with exponential backoff on transient failures (2, 4, 8, 16, 32, 64 s).

    Retries 429 (Blave per-IP rate limit, 500/5min), 5xx (incl. 503, which the
    API returns when upstream FinMind itself rate-limits), and connection/read
    timeouts (a slow batch endpoint under load — e.g. a big multi-symbol crypto
    kline request — reads exactly like this; previously an unlucky timeout just
    silently dropped that whole chunk's symbols with no retry). 403 is NOT
    retried — the API returns it only for a missing/invalid api-key, a permanent
    error that backing off would just delay surfacing.
    """
    for attempt in range(max_retries):
        try:
            r = requests.get(url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** (attempt + 1)
            print(f"  {type(e).__name__} transient — retrying in {wait}s ({url.split('/')[-2]}/{url.split('/')[-1]})")
            time.sleep(wait)
            continue
        if r.status_code != 429 and r.status_code < 500:
            r.raise_for_status()
            return r
        wait = 2 ** (attempt + 1)
        print(f"  {r.status_code} transient — retrying in {wait}s ({url.split('/')[-2]}/{url.split('/')[-1]})")
        time.sleep(wait)
    r.raise_for_status()
    return r


def _monthly_cache_dir(prefix, params):
    """cache/{prefix}_{param_str}/  — parent dir for monthly parquet files."""
    param_str = '_'.join(str(v) for _, v in sorted(params.items()))
    return _CACHE_DIR / f'{prefix}_{param_str}'


def _next_month(ym):
    """'2022-01' → '2022-02', '2022-12' → '2023-01'"""
    y, m = int(ym[:4]), int(ym[5:7])
    return f'{y+1}-01' if m == 12 else f'{y}-{m+1:02d}'


def _iter_months(start_str, end_str):
    """Yield 'YYYY-MM' strings from start month to end month (inclusive)."""
    ym, end_ym = start_str[:7], end_str[:7]
    while ym <= end_ym:
        yield ym
        ym = _next_month(ym)


def _contiguous_spans(months):
    """Group a sorted list of 'YYYY-MM' into runs of consecutive months.
    ['2022-01','2022-02','2022-05'] → [['2022-01','2022-02'], ['2022-05']]."""
    spans, cur = [], []
    for ym in months:
        if cur and _next_month(cur[-1]) == ym:
            cur.append(ym)
        else:
            if cur:
                spans.append(cur)
            cur = [ym]
    if cur:
        spans.append(cur)
    return spans


def _normalise_index(df):
    """Convert tz-aware index to tz-naive UTC in-place-safe copy."""
    if df.index.tz is not None:
        df = df.copy()
        df.index = df.index.tz_convert('UTC').tz_localize(None)
    return df


def _month_end_utc(ym):
    """Naive-UTC datetime of the first instant AFTER month `ym` ('YYYY-MM')."""
    nxt = _next_month(ym)
    return datetime(int(nxt[:4]), int(nxt[5:7]), 1)


def _written_before_month_end(path, ym):
    """True if `path` was last written before month `ym` was over (UTC).

    A past-month file written while that month was still the current month may
    be missing its tail (the delta-update path stops running once the month
    rolls over), so it needs one completing re-fetch. After that re-fetch the
    file's mtime is later than the month end and this never triggers again.
    An unstatable file counts as incomplete → re-fetch.
    """
    try:
        return datetime.utcfromtimestamp(path.stat().st_mtime) < _month_end_utc(ym)
    except Exception:
        return True


def _stale_incomplete_month(path, ttl_hours, ym, edge_days=15):
    """True if `path` is an empty or implausibly-partial past month older than
    `ttl_hours` — used by sources whose history is backfilled progressively
    server-side, where a month fetched mid-backfill caches a permanent hole.

    Only called when a TTL is set. mtime is checked first (cheap stat) so files
    younger than the TTL are never opened. Past the age gate only the index is
    read; the month counts as stale when it has 0 rows, or its first bar starts
    more than `edge_days` after the month begins, or its last bar ends more than
    `edge_days` before the month ends (15 days clears the longest TAIFEX Lunar
    New Year closure, ~11 calendar days). A month that passes gets its mtime
    refreshed so it is re-examined at most once per TTL window. An unreadable
    file counts as stale → re-fetch.

    Known blind spots (heuristic limits, accepted): a hole of <= `edge_days` at
    either month edge, and any hole strictly inside the month, pass the check and
    are never re-fetched. Also note the flip side: a month that can never be
    complete (source history starts mid-month, delisting, pre-history empty
    months inside the requested range) re-fetches once per TTL window forever —
    clamp the requested start to the source's known history start where possible.
    """
    try:
        if time.time() - path.stat().st_mtime <= ttl_hours * 3600:
            return False
        idx = pd.read_parquet(path, columns=[]).index   # index only, no data pages
        if len(idx) == 0:
            return True
        month_start = datetime(int(ym[:4]), int(ym[5:7]), 1)
        month_end   = _month_end_utc(ym)
        idx = pd.to_datetime(idx)
        if getattr(idx, 'tz', None) is not None:
            idx = idx.tz_convert('UTC').tz_localize(None)
        margin = timedelta(days=edge_days)
        if idx.min() > pd.Timestamp(month_start) + margin:
            return True
        if idx.max() < pd.Timestamp(month_end) - margin:
            return True
        try:
            os.utime(path)   # passed — skip the index read until the TTL expires again
        except OSError:
            pass             # can't refresh mtime — worst case we re-check next call
        return False
    except Exception:
        return True


def _atomic_to_parquet(df, path):
    """Write `df` to `path` via same-directory tmp file + os.replace.

    Readers (and concurrent writers racing on the same month) never see a
    half-written parquet: a crash/kill mid-write leaves only a *.tmp file,
    and os.replace on the same filesystem is atomic.
    """
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    try:
        df.to_parquet(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _extend_cache_monthly(prefix, params, fetch_raw_fn, start, end,
                          empty_marker_ttl_hours=None):
    """Monthly-partitioned cache.

    Past months (before current month) are stored immutably — fetched once, never re-fetched,
    with one exception: a past-month file last written BEFORE that month ended
    (i.e. cached while it was still the current month, so its tail may be
    missing) gets one completing re-fetch, merged with what is already cached.
    Current month is delta-updated: load cached, fetch from last bar to now, merge.

    empty_marker_ttl_hours (opt-in): by default (None) an empty past month is
    marked once and never re-fetched — correct for sources whose history is
    truly immutable. Sources that backfill history progressively server-side
    (e.g. twstock minute lines, Taiwan futures) pass a TTL: a past month that
    is empty or implausibly partial (first/last bar far from the month edges —
    the backfill-frontier month) and whose mtime is older than the TTL is
    treated as a cache miss and re-fetched, merged with the existing rows. If
    the re-fetch adds nothing the file is rewritten (mtime refreshed), so the
    source is hit at most once per TTL window per incomplete span.

    Directory: cache/{prefix}_{params}/
    Files:     YYYY-MM.parquet  (one per month)
    """
    cache_dir = _monthly_cache_dir(prefix, params)
    cache_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.utcnow()
    end_str   = end or now.strftime('%Y-%m-%d')
    current_ym = now.strftime('%Y-%m')
    tomorrow   = (now + timedelta(days=1)).strftime('%Y-%m-%d')

    all_months    = list(_iter_months(start, end_str))
    past_months   = [ym for ym in all_months if ym < current_ym]
    present_months = [ym for ym in all_months if ym >= current_ym]   # current (+ any future)

    # ── Backfill missing/incomplete PAST months in contiguous spans ───────────
    # Past months are immutable once complete. Fetch each contiguous run of missing
    # months with a SINGLE ranged call — raw_fn chunks it concurrently internally —
    # instead of one slow sequential call per month, then split the result into
    # per-month parquets. A month re-fetched here is MERGED with any existing rows,
    # never blindly overwritten, so a thin/partial re-fetch cannot shrink the cache.
    # Empty months still get a marker file so they are never re-fetched — unless
    # empty_marker_ttl_hours is set, in which case an expired empty or implausibly-
    # partial month is treated as missing and re-fetched (see docstring).
    def _needs_fetch(ym):
        path = cache_dir / f'{ym}.parquet'
        if not path.exists():
            return True
        if _written_before_month_end(path, ym):
            return True   # cached mid-month, tail may be missing — complete it once
        return (empty_marker_ttl_hours is not None
                and _stale_incomplete_month(path, empty_marker_ttl_hours, ym))
    missing = [ym for ym in past_months if _needs_fetch(ym)]
    for span in _contiguous_spans(missing):
        span_start = f'{span[0]}-01'
        span_end   = f'{_next_month(span[-1])}-01'   # exclusive upper bound
        df = fetch_raw_fn(span_start, span_end)
        if not df.empty:
            df = _normalise_index(df)
            df = df[~df.index.duplicated(keep='last')].sort_index()
            by_month = {ym: grp for ym, grp in df.groupby(df.index.strftime('%Y-%m'))}
        else:
            by_month = {}
        for ym in span:
            grp  = by_month.get(ym)
            path = cache_dir / f'{ym}.parquet'
            if path.exists():
                try:
                    existing = _normalise_index(pd.read_parquet(path))
                except Exception:
                    existing = pd.DataFrame()
                if not existing.empty:
                    grp = existing if grp is None else pd.concat([existing, grp])
                    grp = grp[~grp.index.duplicated(keep='last')].sort_index()
            _atomic_to_parquet(grp if grp is not None else pd.DataFrame(), path)

    # One batched read for all past months instead of one read_parquet call per month
    # file — with hundreds of ids x ~140 month files each, the per-file open/parse
    # overhead is what makes warm multi-stock backtests slow (2026-08: 151s of a
    # tw_low_pe run was this loop). Caveat: a multi-file read takes the FIRST file's
    # schema — an empty-month marker (no columns) first in the list would silently
    # blank the whole id, and a month with a divergent schema would silently lose
    # columns. So filter markers out with a footer-only schema read (~50x cheaper
    # than a full read) and only batch when every schema matches; otherwise fall
    # back to the per-file loop, whose concat unions columns.
    frames = []
    non_empty, names0, uniform = [], None, True
    for ym in past_months:
        path = cache_dir / f'{ym}.parquet'
        names = pq.read_schema(path).names
        if not names:
            continue                      # empty-month marker
        if names0 is None:
            names0 = names
        elif names != names0:
            uniform = False
        non_empty.append(path)
    if non_empty and uniform:
        try:
            cached = pd.read_parquet(non_empty)
            if not cached.empty:
                frames.append(cached)
        except Exception:                 # e.g. same names, clashing dtypes
            uniform = False
    if non_empty and not uniform:
        for path in non_empty:
            cached = pd.read_parquet(path)
            if not cached.empty:
                frames.append(cached)

    # ── Current month (and any future month in range) — delta update ──────────
    for ym in present_months:
        path     = cache_dir / f'{ym}.parquet'
        ym_start = f'{ym}-01'
        if path.exists():
            cached = _normalise_index(pd.read_parquet(path))
            last_ts = cached.index[-1].strftime('%Y-%m-%d')
            delta = fetch_raw_fn(last_ts, tomorrow)
            if not delta.empty:
                delta  = _normalise_index(delta)
                merged = pd.concat([cached, delta])
                merged = merged[~merged.index.duplicated(keep='last')].sort_index()
                _atomic_to_parquet(merged, path)
                frames.append(merged)
            else:
                frames.append(cached)
        else:
            df = fetch_raw_fn(ym_start, tomorrow)
            if df.empty:
                continue
            df = _normalise_index(df)
            df = df[~df.index.duplicated(keep='last')].sort_index()
            _atomic_to_parquet(df, path)
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    result = pd.concat(frames)
    result = _normalise_index(result)
    result = result[~result.index.duplicated(keep='last')].sort_index()

    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end_str) + pd.Timedelta(days=1)
    return result[(result.index >= start_ts) & (result.index < end_ts)]


def _save_monthly(prefix, params, df):
    """Split df by month and save each month's slice to its own parquet file.
    Used by batch fetchers to populate the monthly cache after a bulk API call.
    """
    cache_dir = _monthly_cache_dir(prefix, params)
    cache_dir.mkdir(parents=True, exist_ok=True)
    df = _normalise_index(df)
    df = df[~df.index.duplicated(keep='last')].sort_index()
    for ym, grp in df.groupby(df.index.strftime('%Y-%m')):
        path = cache_dir / f'{ym}.parquet'
        if path.exists():
            existing = _normalise_index(pd.read_parquet(path))
            grp = pd.concat([existing, grp])
            grp = grp[~grp.index.duplicated(keep='last')].sort_index()
        grp.to_parquet(path, compression='snappy')


# ── Kline ─────────────────────────────────────────────────────────────────────

def _sanity_check_ohlc(df, label):
    """Drop bars with impossible OHLC values (high<low, non-positive or NaN price).

    Corrupt upstream/exchange data would otherwise silently propagate into every
    indicator and signal computed on top of it — not a hypothetical, this is the
    failure mode a strategy author can't see just by eyeballing a chart.

    Called at READ time (on the assembled result, after the cache), never before
    writing the cache: the cache must keep the raw upstream bars, so a transient
    upstream glitch doesn't become a permanent hole in an immutable monthly
    parquet, and bars already cached before this check existed are covered too.

    Dropping leaves a gap in the bar series (shift/pct_change will span it) —
    same as an exchange outage. The dropped timestamps are printed so the gap
    is diagnosable; corrupt bars are strictly worse than a visible gap.
    """
    if df.empty or not all(c in df.columns for c in ('Open', 'High', 'Low', 'Close')):
        return df
    ohlc = df[['Open', 'High', 'Low', 'Close']]
    bad = (df['High'] < df['Low']) | (ohlc <= 0).any(axis=1) | ohlc.isna().any(axis=1)
    if bad.any():
        ts = ', '.join(str(t) for t in df.index[bad][:5])
        more = '' if int(bad.sum()) <= 5 else f' (+{int(bad.sum()) - 5} more)'
        print(f"  ⚠️  {label}: dropped {int(bad.sum())} bar(s) with invalid OHLC "
              f"(high<low, non-positive or NaN price) at: {ts}{more}")
        df = df[~bad]
    return df


def _is_sub_5min(interval):
    return pd.Timedelta(interval) < pd.Timedelta('5min')


def _sub_5min_earliest():
    """Midnight-floored, mirroring server-side validate_sub_5min_request — start is a
    date-only string, so an un-floored cutoff would reject the very date the error
    message advertises as valid (00:00 < now's time-of-day)."""
    return (datetime.utcnow() - timedelta(days=45)).replace(
        hour=0, minute=0, second=0, microsecond=0)


def _validate_sub_5min_start(interval, start):
    """Sub-5min klines only go back 45 days server-side; fail loudly, never clamp
    a user-facing range — a silently shortened backtest window is worse than an error."""
    if _is_sub_5min(interval):
        earliest = _sub_5min_earliest()
        if datetime.strptime(start, '%Y-%m-%d') < earliest:
            raise ValueError(
                f'{interval} kline only goes back 45 days '
                f'(start must be {earliest.strftime("%Y-%m-%d")} or later)')


def _fetch_kline_raw(symbol, interval, start, end, headers):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    sub_5min = _is_sub_5min(interval)
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
    if sub_5min:
        # The cache layer widens ranges to month starts, which can reach past the
        # API's 45-day floor even when the user's own start is legal — clamp only
        # here (internal ranges); user-facing starts are validated before this,
        # on the same _sub_5min_earliest basis so a start exactly at the validated
        # earliest is never silently clamped away.
        s = max(s, _sub_5min_earliest())
        if s >= e:
            return pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume'])
    chunks, cursor = [], s
    chunk_days = 30 if sub_5min else 365
    while cursor < e:
        chunk_end = min(cursor + timedelta(days=chunk_days), e)
        chunks.append((cursor.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
        cursor = chunk_end

    def _fetch_one(cs, ce):
        # Sub-5min cold fetches hit Binance fapi server-side and can take minutes;
        # _retry_get also covers transient 429/5xx/timeouts a bare requests.get dropped.
        try:
            r = _retry_get(f'{BASE}/kline', headers=headers, params={
                'symbol': symbol, 'period': interval,
                'start_date': cs, 'end_date': ce,
            }, timeout=300 if sub_5min else 60)
        except requests.HTTPError as exc:
            resp = exc.response
            raise RuntimeError(
                f'/kline {symbol} {interval} HTTP {resp.status_code}: {resp.text[:200]}'
            ) from exc
        return r.json()

    rows = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one, cs, ce): (cs, ce) for cs, ce in chunks}
        for future in as_completed(futures):
            rows.extend(future.result())

    if not rows:
        return pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume'])
    df = pd.DataFrame(rows)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df = df.set_index('time').sort_index()
    df = df[~df.index.duplicated(keep='first')]
    df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                            'close': 'Close', 'volume': 'Volume'})
    if 'Volume' not in df.columns:
        df['Volume'] = 0
    return df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)


def normalize_symbol(symbol):
    """Any venue/ccxt symbol form → platform canonical dashless uppercase
    ('BTC/USDT', 'BTC-USDT', 'BTC_USDT', 'btcusdt' → 'BTCUSDT').

    THE single normalization recipe (the get_positions() symbol contract in
    references/lib.md quotes it) — a new venue whose symbol format introduces a
    separator not covered here must extend this function, not a local copy.
    """
    return symbol.replace('/', '').replace('-', '').replace('_', '').upper()


def fetch_kline(symbol, interval, start, end, headers):
    """Fetch OHLCV kline data from Blave API with date chunking and local cache.

    Sub-5min intervals (1min..4min): served live from Binance by the API,
    45-day lookback max, real volume. Cache namespace is kline2 — the old
    kline cache has Volume hard-zeroed and must not be mixed with real volume.
    """
    # Venue forms like 'BTC/USDT' → Binance 'BTCUSDT'; the API 400s on
    # separator forms and the separator would leak into the cache dir name.
    symbol = normalize_symbol(symbol)
    _validate_sub_5min_start(interval, start)
    df = _extend_cache_monthly(
        'kline2', {'symbol': symbol, 'period': interval},
        lambda s, e: _fetch_kline_raw(symbol, interval, s, e, headers),
        start, end,
    )
    return _sanity_check_ohlc(df, f'{symbol} {interval} kline')


def fetch_kline_batch(symbols, interval, start, end, headers):
    """Batch fetch OHLCV kline for many symbols via /kline/batch (chunk_size=20).
    Returns dict {symbol: DataFrame(Open, High, Low, Close, Volume)} — keys are
    the NORMALIZED canonical symbols (see normalize_symbol), not the caller's
    original strings: index the result with 'BTCUSDT' even if you passed 'BTC/USDT'.

    Uses the same monthly cache dir naming as fetch_kline ('kline2_{interval}_{symbol}')
    so single-symbol and batch calls share cache — a symbol already cached via
    fetch_kline is a warm hit here too, and vice versa. Warm ids are extended through
    the batch endpoint too (not one call per symbol) — see _fetch_batch_cached."""
    symbols = [normalize_symbol(s) for s in symbols]
    _validate_sub_5min_start(interval, start)
    def _parse(records):
        df = pd.DataFrame(records)
        df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
        df = df.set_index('time').sort_index()
        df = df[~df.index.duplicated(keep='first')]
        df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                                'close': 'Close', 'volume': 'Volume'})
        if 'Volume' not in df.columns:
            df['Volume'] = 0
        return df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)

    results = _fetch_batch_cached(
        f'kline2_{interval}', f'{BASE}/kline/batch?period={interval}', 'symbols',
        lambda sid, s, e, hdrs: _fetch_kline_raw(sid, interval, s, e, hdrs),
        _parse, symbols, start, end, headers,
        chunk_size=20, start_param='start_date', end_param='end_date',
        date_chunk_days=30 if _is_sub_5min(interval) else 365,
    )
    return {sid: _sanity_check_ohlc(df, f'{sid} {interval} kline') for sid, df in results.items()}


# ── Exchange-native kline ─────────────────────────────────────────────────────
# Blave's own /kline serves Binance USDT-M perps only. A contract listed on the
# exchange the user actually trades — BingX's gold perp GOLD(XAU)-USDT, say — is
# simply not in it, and substituting a same-ish Binance symbol silently backtests
# a different instrument than the one the orders go to (that is a real incident,
# not a hypothetical). Fetch those straight from the exchange instead.

_BINGX_BASE = 'https://open-api.bingx.com'

# lib interval string → BingX interval. Blave's /kline periods spell minutes
# 'min'; BingX spells them 'm'.
_BINGX_INTERVALS = {
    '1min': '1m', '3min': '3m', '5min': '5m', '15min': '15m', '30min': '30m',
    '1h': '1h', '2h': '2h', '4h': '4h', '6h': '6h', '8h': '8h', '12h': '12h',
    '1d': '1d', '3d': '3d', '1w': '1w',
}

# Measured against the live endpoint: a response is capped at 1000 bars (not the
# documented limit=1440) and keeps the NEWEST end of the requested window, so
# paging walks backwards from endTime.
_BINGX_PAGE = 1000
_EPOCH = datetime(1970, 1, 1)


def _fetch_bingx_kline_raw(symbol, interval, start, end):
    bx_interval = _BINGX_INTERVALS.get(interval)
    if bx_interval is None:
        raise ValueError(f"fetch_bingx_kline: unsupported interval {interval!r} "
                         f"(supported: {', '.join(_BINGX_INTERVALS)})")

    to_ms = lambda s: int((datetime.strptime(s, '%Y-%m-%d') - _EPOCH).total_seconds() * 1000)
    start_ms = to_ms(start)
    end_ms   = to_ms(end) if end else int((datetime.utcnow() - _EPOCH).total_seconds() * 1000)

    rows, cursor_ms, prev_oldest = [], end_ms, None
    while cursor_ms > start_ms:
        r = _retry_get(f'{_BINGX_BASE}/openApi/swap/v3/quote/klines', params={
            'symbol': symbol, 'interval': bx_interval,
            'startTime': start_ms, 'endTime': cursor_ms, 'limit': _BINGX_PAGE,
        }, timeout=30)
        body = r.json()
        # BingX signals errors in the body with HTTP 200, so raise_for_status
        # inside _retry_get sees nothing. Fail loud rather than return a short
        # series that looks like "the contract just has no history there".
        if body.get('code') != 0:
            raise RuntimeError(f"BingX kline {symbol} {bx_interval}: "
                               f"code={body.get('code')} {body.get('msg')}")
        page = body.get('data') or []
        if not page:
            break
        rows.extend(page)
        oldest = min(int(bar['time']) for bar in page)
        if prev_oldest is not None and oldest >= prev_oldest:
            break            # no progress — stop instead of spinning forever
        prev_oldest = oldest
        cursor_ms = oldest - 1

    cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows)
    df['time'] = pd.to_datetime(df['time'].astype('int64'), unit='ms', utc=True)
    df = df.set_index('time').sort_index()
    df = df[~df.index.duplicated(keep='first')]
    df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                            'close': 'Close', 'volume': 'Volume'})
    return df[cols].astype(float)


def fetch_bingx_kline(symbol, interval, start, end):
    """OHLCV for a BingX perpetual, straight from BingX's public API — no key needed.

    `symbol` is the BingX API symbol, NOT the display name shown on the chart:
    GOLD(XAU)-USDT is `NCCOGOLD2USD-USDT`. Look it up in
    `GET /openApi/swap/v3/quote/contracts` (the `symbol` / `displayName` pair).
    Whatever you pass here must be the same symbol the orders use.
    """
    df = _extend_cache_monthly(
        'bingx_kline', {'symbol': symbol, 'period': interval},
        lambda s, e: _fetch_bingx_kline_raw(symbol, interval, s, e),
        start, end,
    )
    return _sanity_check_ohlc(df, f'{symbol} {interval} bingx_kline')


# ── Alpha data ────────────────────────────────────────────────────────────────

def _fetch_alpha_raw(endpoint, params, headers, start, end):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
    chunks, cursor = [], s
    while cursor < e:
        chunk_end = min(cursor + timedelta(days=365), e)
        chunks.append((cursor.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
        cursor = chunk_end

    def _fetch_one(cs, ce):
        r = requests.get(f'{BASE}/{endpoint}', headers=headers, params={
            **params, 'start_date': cs, 'end_date': ce,
        }, timeout=60)
        r.raise_for_status()
        data = r.json().get('data', {})
        return data.get('timestamp', []), data.get('alpha', [])

    ts_list, alpha_list = [], []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one, cs, ce): (cs, ce) for cs, ce in chunks}
        for future in as_completed(futures):
            ts, alpha = future.result()
            ts_list.extend(ts)
            alpha_list.extend(alpha)

    df = pd.DataFrame({
        'time':  pd.to_datetime(ts_list, unit='s', utc=True),
        'alpha': pd.to_numeric(alpha_list, errors='coerce'),
    }).set_index('time').sort_index()
    return df[~df.index.duplicated(keep='first')]


def _fetch_alpha(endpoint, params, headers, start, end):
    slug = endpoint.split('/')[0]
    return _extend_cache_monthly(
        slug, params,
        lambda s, e: _fetch_alpha_raw(endpoint, params, headers, s, e),
        start, end,
    )


def fetch_holder_concentration(symbol, interval, start, end, headers):
    """籌碼集中度 Holder Concentration. Returns DataFrame with 'alpha' column."""
    return _fetch_alpha('holder_concentration/get_alpha',
                        {'symbol': symbol, 'period': interval}, headers, start, end)


def fetch_funding_rate(symbol, interval, start, end, headers):
    """資金費率 Funding Rate (Binance). Returns DataFrame with 'alpha' column (alpha = funding rate × 100)."""
    return _fetch_alpha('funding_rate/get_alpha',
                        {'symbol': symbol, 'period': interval}, headers, start, end)


def fetch_taker_intensity(symbol, interval, start, end, headers, timeframe='24h'):
    """多空力道 Taker Intensity. Returns DataFrame with 'alpha' column."""
    return _fetch_alpha('taker_intensity/get_alpha',
                        {'symbol': symbol, 'period': interval, 'timeframe': timeframe},
                        headers, start, end)


def fetch_whale_hunter(symbol, interval, start, end, headers, timeframe='24h', score_type='score_oi'):
    """巨鯨警報 Whale Hunter. Returns DataFrame with 'alpha' column."""
    return _fetch_alpha('whale_hunter/get_alpha',
                        {'symbol': symbol, 'period': interval,
                         'timeframe': timeframe, 'score_type': score_type},
                        headers, start, end)


def fetch_unusual_movement(symbol, interval, start, end, headers, timeframe='24h'):
    """異常漲跌 Unusual Movement. Returns DataFrame with 'alpha' column."""
    return _fetch_alpha('unusual_movement/get_alpha',
                        {'symbol': symbol, 'period': interval, 'timeframe': timeframe},
                        headers, start, end)


def fetch_squeeze_momentum(symbol, start, end, headers):
    """擠壓動能 Squeeze Momentum (period fixed to 1d). Returns DataFrame with 'alpha' column."""
    return _fetch_alpha('squeeze_momentum/get_alpha',
                        {'symbol': symbol, 'period': '1d'}, headers, start, end)


def fetch_liquidation(symbol, interval, start, end, headers, timeframe='24h'):
    """爆倉指標 Liquidation. Returns DataFrame with 'alpha' column."""
    return _fetch_alpha('liquidation/get_alpha',
                        {'symbol': symbol, 'period': interval, 'timeframe': timeframe},
                        headers, start, end)


def fetch_market_direction(interval, start, end, headers):
    """市場方向 Market Direction (BTC only, no symbol). Returns DataFrame with 'alpha' column."""
    return _fetch_alpha('market_direction/get_alpha',
                        {'period': interval}, headers, start, end)


def fetch_capital_shortage(interval, start, end, headers):
    """資金稀缺 Capital Shortage (market-wide, no symbol). Returns DataFrame with 'alpha' column."""
    return _fetch_alpha('capital_shortage/get_alpha',
                        {'period': interval}, headers, start, end)


def fetch_market_sentiment(symbol, interval, start, end, headers):
    """市場情緒 Market Sentiment. Returns DataFrame with 'alpha' column."""
    return _fetch_alpha('market_sentiment/get_alpha',
                        {'symbol': symbol, 'period': interval}, headers, start, end)


def fetch_top_trader_exposure(interval, start, end, headers):
    """Blave頂尖交易員曝險 Top Trader Exposure (BTC only, no symbol). Returns DataFrame with 'alpha' column."""
    return _fetch_alpha('blave_top_trader/get_exposure',
                        {'period': interval}, headers, start, end)


# ── CME / NYMEX / ICE futures (via /studio/market/db) ────────────────────────

_DB_CHUNK_DAYS = {'ohlcv-1m': 28, 'ohlcv-1h': 365, 'ohlcv-1d': 3650}


def _fetch_db_raw(dataset, symbol, schema, start, end, headers):
    """Fetch OHLCV — chunks fetched concurrently, chunk size by schema."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    s    = datetime.strptime(start, '%Y-%m-%d')
    e    = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
    days = _DB_CHUNK_DAYS.get(schema, 30)

    chunks, cursor = [], s
    while cursor < e:
        chunk_end = min(cursor + timedelta(days=days), e)
        chunks.append((cursor.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
        cursor = chunk_end

    def _fetch_one(cs, ce):
        import time as _time
        for attempt in range(3):
            try:
                r = requests.get(
                    f'{BASE}/studio/market/db/ohlcv/{dataset}/{symbol}/{schema}',
                    headers=headers,
                    params={'start': cs, 'end': ce},
                    timeout=120,
                )
                r.raise_for_status()
                return r.json().get('data', [])
            except Exception:
                if attempt == 2:
                    raise
                _time.sleep(2 ** attempt)

    rows = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_one, cs, ce): (cs, ce) for cs, ce in chunks}
        for future in as_completed(futures):
            rows.extend(future.result())

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df['time'] = pd.to_datetime(df['ts'], utc=True)
    df = df.set_index('time').sort_index()
    df = df[~df.index.duplicated(keep='first')]
    df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                             'close': 'Close', 'volume': 'Volume'})
    ohlcv = df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)
    if 'instrument_id' in df.columns:
        ohlcv['instrument_id'] = df['instrument_id'].values
    return ohlcv


def settlement_signals_from_db(df, signal):
    """Force signal=0.0 on last bar before each instrument_id rollover (contract expiry).

    Returns (signal, exec_at_close) where exec_at_close is a bool Series marking
    settlement bars — those bars execute at this-bar close, not next-bar open.
    If instrument_id column is absent, exec_at_close is all-False.
    """
    import pandas as pd
    exec_at_close = pd.Series(False, index=df.index)
    if 'instrument_id' not in df.columns:
        return signal, exec_at_close
    changes = (df['instrument_id'] != df['instrument_id'].shift(1)).values
    for i, changed in enumerate(changes):
        if changed and i > 0:
            signal.iloc[i - 1]       = 0.0
            exec_at_close.iloc[i - 1] = True
    return signal, exec_at_close


def fetch_db_kline(dataset, symbol, schema, start, end, headers):
    """Fetch CME/NYMEX/ICE OHLCV with local cache."""
    slug = schema.replace('-', '')
    df = _extend_cache_monthly(
        f'db_{slug}', {'dataset': dataset.replace('.', ''), 'symbol': symbol},
        lambda s, e: _fetch_db_raw(dataset, symbol, schema, s, e, headers),
        start, end,
    )
    return _sanity_check_ohlc(df, f'{symbol} {schema} db_kline')


# ── Taiwan stock data ─────────────────────────────────────────────────────────

def _fetch_twstock_price_raw(stock_id, start, end, headers):
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = _retry_get(f'{BASE}/studio/market/twstock/price_adj/{stock_id}',
                   headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    data = r.json().get('data', [])
    if not data:
        return pd.DataFrame(columns=['Open', 'Close'])
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()[['open', 'close']].rename(
        columns={'open': 'Open', 'close': 'Close'}).astype(float)
    return df.replace(0, float('nan')).ffill()


def fetch_twstock_price_adj(stock_id, start, end, headers):
    """台股向後調整日K（除權息還原價）. Returns DataFrame with Open/Close columns.
    Use for backtesting — prices are dividend-adjusted so returns are comparable across time."""
    return _extend_cache_monthly(
        'twstock_price', {'id': stock_id},
        lambda s, e: _fetch_twstock_price_raw(stock_id, s, e, headers),
        start, end,
    )


def _fetch_twstock_price_nonadj_raw(stock_id, start, end, headers):
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = _retry_get(f'{BASE}/studio/market/twstock/price/{stock_id}',
                   headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    data = r.json().get('data', [])
    if not data:
        return pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume'])
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    cols = [c for c in ['open', 'high', 'low', 'close', 'volume'] if c in df.columns]
    df = df.set_index('date').sort_index()[cols].rename(
        columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}).astype(float)
    return df.replace(0, float('nan')).ffill()


def fetch_twstock_price(stock_id, start, end, headers):
    """台股原始日K（未除權息）. Returns DataFrame with Open/High/Low/Close/Volume columns.
    Use for visualization/charting — matches prices users see on broker apps.
    Do NOT use for backtesting (dividends cause artificial price drops that distort signals)."""
    df = _extend_cache_monthly(
        'twstock_price_nonadj', {'id': stock_id},
        lambda s, e: _fetch_twstock_price_nonadj_raw(stock_id, s, e, headers),
        start, end,
    )
    return _sanity_check_ohlc(df, f'{stock_id} twstock price')


def fetch_twstock_quote(stock_id, headers):
    """台股即時報價快照（約 10 秒更新）. Returns a flat dict — NOT a DataFrame, since a quote
    is a single point-in-time observation with no date range to index on. Keys: open/high/low/close
    (today so far), change_price, change_rate, average_price, volume (latest tick), total_volume
    (day cumulative), amount, total_amount, yesterday_volume, buy_price/buy_volume (best bid),
    sell_price/sell_volume (best ask), volume_ratio, quote_time (full timestamp), stock_id,
    tick_type (0=indeterminate/1=sell-initiated/2=buy-initiated).
    No local cache — the server enforces a 10s Redis TTL; every call means "right now"."""
    r = _retry_get(f'{BASE}/studio/market/twstock/quote/{stock_id}', headers=headers, timeout=30)
    return r.json().get('data', {})


def fetch_twstock_quote_batch(stock_ids, headers):
    """Batch 即時報價（最多 50 檔）. Returns dict {stock_id: quote_dict}, same fields as
    fetch_twstock_quote per entry. No local cache, same as the single-stock version."""
    r = _retry_get(f'{BASE}/studio/market/twstock/quote', headers=headers,
                    params={'stock_ids': ','.join(stock_ids)}, timeout=30)
    return r.json().get('data', {})


# ── Taiwan stock minute-line OHLCV ────────────────────────────────────────────

_TWSTOCK_MINUTE_CHUNK_DAYS = {'1d': 3650, '1m': 28, '5m': 28, '15m': 28, '30m': 28, '60m': 28}
# Server-side per-request range caps — also used as the default lookback window
# when start is omitted, matching the endpoint's own default.
_TWSTOCK_MINUTE_MAX_DAYS = {'1d': 3650, '1m': 31, '5m': 62, '15m': 93, '30m': 186, '60m': 365}


def _fetch_twstock_minute_raw(stock_id, schema, start, end, headers, adjust=False):
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
    chunk_days = _TWSTOCK_MINUTE_CHUNK_DAYS.get(schema, 28)

    chunks, cursor = [], s
    while cursor < e:
        chunk_end = min(cursor + timedelta(days=chunk_days), e)
        chunks.append((cursor.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
        cursor = chunk_end

    def _fetch_one(cs, ce):
        r = _retry_get(
            f'{BASE}/studio/market/twstock/minute/ohlcv/{stock_id}/{schema}',
            headers=headers,
            params={'start': cs, 'end': ce, 'adjust': '1' if adjust else '0'},
            timeout=60,
        )
        return r.json().get('data', [])

    rows = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_one, cs, ce): (cs, ce) for cs, ce in chunks}
        for future in as_completed(futures):
            rows.extend(future.result())

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df['time'] = pd.to_datetime(df['ts'], utc=True)
    df = df.set_index('time').sort_index()
    df = df[~df.index.duplicated(keep='first')]
    df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                             'close': 'Close', 'volume': 'Volume'})
    return df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)


def fetch_twstock_ohlcv(stock_id, schema, headers, start=None, end=None, adjust=False):
    """台股現股分線 OHLCV. Returns DataFrame with Open/High/Low/Close/Volume columns.

    stock_id: any listed TWSE/TPEx security (e.g. '2330')
    schema: '1d' | '1m' | '5m' | '15m' | '30m' | '60m'
    Volume is in lots (張), NOT shares. Bars carry minute-START labels (UTC
    index); the 13:30 Taipei bar is the closing auction. start/end optional
    (YYYY-MM-DD): omitted end = today, omitted start = end minus the server's
    max window for the schema (1m→31d, 5m→62d, 15m→93d, 30m→186d, 60m→365d,
    1d→3650d).

    adjust=True returns forward-adjusted (後復權) OHLC — use for backtests
    spanning ex-dividend dates; same factor pipeline as fetch_twstock_price_adj
    so the numbers match the Studio daily adjusted series exactly. Volume is
    unchanged. If the factor source is unavailable the server fails loud (503,
    retried then raised here) instead of silently returning raw prices.
    Raw and adjusted bars are cached in separate monthly cache dirs.

    History starts at 2019-01-01 (FinMind TaiwanStockKBar data origin); an
    earlier start is silently clamped to 2019-01-01 so pre-2019 months are
    never queried or cached.

    Server-side, the whole market's history is already backfilled from
    2019-01; only very newly listed stocks are demand-driven — seeded on
    their first-ever query and queued for deep backfill, so that query may
    return only recent data, with full history usually landing by the next
    day. Locally,
    an empty past month is cached with a 24-hour TTL (not permanently): once
    the server has the data, the next query after the TTL re-fetches and
    self-heals the cache.

    For 1d: index is Asia/Taipei tz so df.index[-1].date() returns the correct trading date.
    """
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    if not start:
        lookback = _TWSTOCK_MINUTE_MAX_DAYS.get(schema, 31)
        start = (datetime.strptime(end_str, '%Y-%m-%d')
                 - timedelta(days=lookback)).strftime('%Y-%m-%d')
    if start < '2019-01-01':
        start = '2019-01-01'
    df = _extend_cache_monthly(
        f'twstock_minute_{schema}', {'id': stock_id, 'adj': int(adjust)},
        lambda s, e: _fetch_twstock_minute_raw(stock_id, schema, s, e, headers, adjust=adjust),
        start, end,
        empty_marker_ttl_hours=24,
    )
    df = _sanity_check_ohlc(df, f'{stock_id} {schema} twstock minute')
    if schema == '1d' and not df.empty:
        # Cache stores naive UTC (midnight TWN = prev-day 16:00 UTC). Convert to Asia/Taipei
        # so the index date matches the actual trading date.
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True).tz_convert('Asia/Taipei')
    return df


def fetch_twstock_ohlcv_symbols(headers):
    """Stocks that currently have minute-line data server-side — the covered set
    for fetch_twstock_ohlcv. Returns a plain list of stock_id strings.

    Unlike the stock-futures variant, absence here is not a hard 400: any listed
    TWSE/TPEx stock_id can still be queried, and the first query seeds recent
    data + enrolls the stock for ongoing collection. Call this first anyway to
    know whether deep history is already backfilled before running a backtest.
    """
    r = _retry_get(f'{BASE}/studio/market/twstock/minute/ohlcv/symbols',
                   headers=headers, timeout=30)
    return r.json().get('data', [])


def _fetch_twstock_inst_raw(stock_id, start, end, headers):
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = _retry_get(f'{BASE}/studio/market/twstock/institutional/{stock_id}',
                   headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    data = r.json().get('data', [])
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()
    df['foreign_net'] = df['foreign_buy'] - df['foreign_sell']
    return df.fillna(0)


def fetch_twstock_institutional(stock_id, start, end, headers):
    """台股三大法人每日買賣超. Returns DataFrame with foreign_net and raw columns."""
    return _extend_cache_monthly(
        'twstock_inst', {'id': stock_id},
        lambda s, e: _fetch_twstock_inst_raw(stock_id, s, e, headers),
        start, end,
    )


def _fetch_twstock_shareholding_raw(stock_id, start, end, headers):
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = _retry_get(f'{BASE}/studio/market/twstock/shareholding/{stock_id}',
                   headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    data = r.json().get('data', [])
    if not data:
        return pd.DataFrame(columns=['shareholders'])
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    total = df[df['level'] == 'total'].set_index('date').sort_index()
    result = total[['people']].rename(columns={'people': 'shareholders'}).astype(float)
    return result[~result.index.duplicated(keep='last')]


def fetch_twstock_shareholding(stock_id, start, end, headers):
    """台股週頻股東人數（持股分級表 total）. Returns DataFrame with 'shareholders' column."""
    return _extend_cache_monthly(
        'twstock_shareholding', {'id': stock_id},
        lambda s, e: _fetch_twstock_shareholding_raw(stock_id, s, e, headers),
        start, end,
    )


def _fetch_twstock_per_raw(stock_id, start, end, headers):
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = _retry_get(f'{BASE}/studio/market/twstock/per/{stock_id}',
                   headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    data = r.json().get('data', [])
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    return df.set_index('date').sort_index()


def fetch_twstock_per(stock_id, start, end, headers):
    """台股每日本益比 / 股價淨值比 / 殖利率. Columns: dividend_yield, PER, PBR. Data from 2005-10-01."""
    return _extend_cache_monthly(
        'twstock_per', {'id': stock_id},
        lambda s, e: _fetch_twstock_per_raw(stock_id, s, e, headers),
        start, end,
    )


_DIVIDEND_COLUMNS = ['record_date', 'period', 'announce_date', 'cash_ex_date',
                     'stock_ex_date', 'pay_date', 'cash', 'stock', 'stock_ratio']


def _dividend_slice(df, start, end):
    """Range-filter on the API's three-tier effective date (cash_ex_date, else
    stock_ex_date, else record_date) — same ladder the server applies, so a
    locally-sliced full-history cache matches a server-side ranged query.
    Announced-but-undated rows fall through to record_date and stay visible."""
    if df.empty or (not start and not end):
        return df
    eff = df['cash_ex_date'].where(df['cash_ex_date'] != '', df['stock_ex_date'])
    eff = eff.where(eff != '', df['record_date'])
    mask = pd.Series(True, index=df.index)
    if start:
        mask &= eff >= start
    if end:
        mask &= eff <= end
    return df[mask]


def fetch_twstock_dividend(stock_id, start, end, headers):
    """台股股利事件 (one row per announcement row; cash + stock dividends).
    Columns: record_date, period, announce_date, cash_ex_date, stock_ex_date,
    pay_date, cash, stock, stock_ratio. Empty dates are '' (never NaN); `period`
    is an OPAQUE label ('114年第3季', '不適用', …) — never parse it as a year.
    Zero-value rows (cash==0 and stock==0) are announced no-distribution
    decisions and are kept. Returns an empty DataFrame for unknown ids / no
    dividend history (the API's 404). Full history is cached per stock with a
    1-day TTL (new announcements land daily) and sliced locally, so repeated
    calls with different ranges cost one API hit per stock per day."""
    path = _fundamental_cache_path('twstock_dividend', stock_id)
    df = _load_fundamental_cache(path, max_age_days=1)
    if df is None:
        try:
            r = _retry_get(f'{BASE}/studio/market/twstock/dividend/{stock_id}',
                           headers=headers, timeout=30)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return pd.DataFrame(columns=_DIVIDEND_COLUMNS)
            raise
        df = pd.DataFrame(r.json().get('data', []))
        if not df.empty:
            _save_fundamental_cache(path, df)
    return _dividend_slice(df, start, end).reset_index(drop=True)


def fetch_twstock_dividend_batch(stock_ids, start, end, headers):
    """Batch 台股股利事件. Returns dict {stock_id: DataFrame} (same columns as
    fetch_twstock_dividend). Ids with no dividend history are silently absent
    (the batch API's contract); ids in the API's `failed` list are reported and
    absent — re-call for those. Cache-first per stock (1-day TTL, full history),
    uncached ids fetched in chunks of 50; ranges sliced locally."""
    results, uncached = {}, []
    for sid in stock_ids:
        path = _fundamental_cache_path('twstock_dividend', sid)
        df = _load_fundamental_cache(path, max_age_days=1)
        if df is not None:
            results[sid] = _dividend_slice(df, start, end).reset_index(drop=True)
        else:
            uncached.append(sid)

    for i in range(0, len(uncached), 50):
        chunk = uncached[i:i + 50]
        try:
            r = _retry_get(f'{BASE}/studio/market/twstock/batch/dividend',
                           headers=headers,
                           params={'stock_ids': ','.join(chunk)}, timeout=120)
            payload = r.json()
            failed = payload.get('failed', [])
            if failed:
                print(f'  [batch] dividend server-side fetch failed for {failed} — '
                      f'absent from results, re-call for those ids')
            for sid, records in payload.get('data', {}).items():
                if not records:
                    continue
                df = pd.DataFrame(records)
                _save_fundamental_cache(
                    _fundamental_cache_path('twstock_dividend', sid), df)
                results[sid] = _dividend_slice(df, start, end).reset_index(drop=True)
        except Exception as e:
            print(f'  [batch] dividend chunk {i//50 + 1} error: {e}')

    return results


def _broker_day_cache_path(stock_id, date_str):
    """cache/twstock_broker_stock_<stock_id>/<date>.parquet"""
    d = _CACHE_DIR / f'twstock_broker_stock_{stock_id}'
    d.mkdir(parents=True, exist_ok=True)
    return d / f'{date_str}.parquet'


def _make_date_chunks(dates, chunk_days=90):
    """Split a sorted date list into chunks, each spanning ≤ chunk_days calendar days."""
    if not dates:
        return []
    chunks = []
    start = dates[0]
    for i, d in enumerate(dates):
        if i == len(dates) - 1 or (dates[i + 1] - start).days >= chunk_days:
            chunks.append((start, d))
            if i < len(dates) - 1:
                start = dates[i + 1]
    return chunks


def _populate_broker_day_cache(stock_id, weekdays, headers,
                               chunk_days=90, rate_limit=270, period=300,
                               max_retries=5):
    """Ensure all weekdays have cached broker data. Uses 90-day range API chunks."""
    EMPTY_COLS = ['date', 'stock_id', 'broker_id', 'broker_name', 'price', 'buy', 'sell']
    missing = [d for d in weekdays if not _broker_day_cache_path(stock_id, d.isoformat()).exists()]
    if not missing:
        return

    chunks  = _make_date_chunks(missing, chunk_days)
    limiter = _RateLimiter(rate_limit, period)
    total   = len(chunks)

    for idx, (cs, ce) in enumerate(chunks):
        chunk_missing = [d for d in missing if cs <= d <= ce]
        for attempt in range(max_retries):
            try:
                limiter.acquire()
                r = requests.get(
                    f'{BASE}/studio/market/twstock/broker/stock/{stock_id}',
                    headers=headers,
                    params={'start': cs.isoformat(), 'end': ce.isoformat()},
                    timeout=120,
                )
                if r.status_code == 429:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if r.status_code >= 500:
                    time.sleep(2 ** (attempt + 1))
                    continue
                r.raise_for_status()
                data    = r.json().get('data', [])
                df_all  = pd.DataFrame(data) if data else pd.DataFrame(columns=EMPTY_COLS)
                by_date = {}
                if not df_all.empty and 'date' in df_all.columns:
                    for date_str, grp in df_all.groupby('date'):
                        by_date[date_str] = grp
                for d in chunk_missing:
                    date_str = d.isoformat()
                    df_day   = by_date.get(date_str, pd.DataFrame(columns=EMPTY_COLS)).copy()
                    df_day['date'] = date_str
                    df_day.to_parquet(_broker_day_cache_path(stock_id, date_str),
                                      index=False, compression='snappy')
                print(f"  [broker_cache {stock_id}] chunk {idx+1}/{total} "
                      f"({cs.isoformat()}~{ce.isoformat()})", flush=True)
                break
            except requests.exceptions.Timeout:
                time.sleep(2 ** (attempt + 1))
            except Exception as e:
                print(f"  [broker_cache {stock_id}] error chunk {cs}~{ce}: {e}")
                break


def _populate_trader_day_cache(trader_id, weekdays, headers,
                               chunk_days=90, rate_limit=270, period=300,
                               max_retries=5):
    """Ensure all weekdays have cached trader data. Uses 90-day range API chunks."""
    EMPTY_COLS = ['date', 'broker_id', 'broker_name', 'stock_id', 'price', 'buy', 'sell']
    missing = [d for d in weekdays if not _trader_day_cache_path(trader_id, d.isoformat()).exists()]
    if not missing:
        return

    chunks  = _make_date_chunks(missing, chunk_days)
    limiter = _RateLimiter(rate_limit, period)
    total   = len(chunks)

    for idx, (cs, ce) in enumerate(chunks):
        chunk_missing = [d for d in missing if cs <= d <= ce]
        for attempt in range(max_retries):
            try:
                limiter.acquire()
                r = requests.get(
                    f'{BASE}/studio/market/twstock/broker/trader/{trader_id}',
                    headers=headers,
                    params={'start': cs.isoformat(), 'end': ce.isoformat()},
                    timeout=120,
                )
                if r.status_code == 429:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if r.status_code >= 500:
                    time.sleep(2 ** (attempt + 1))
                    continue
                r.raise_for_status()
                data    = r.json().get('data', [])
                df_all  = pd.DataFrame(data) if data else pd.DataFrame(columns=EMPTY_COLS)
                by_date = {}
                if not df_all.empty and 'date' in df_all.columns:
                    for date_str, grp in df_all.groupby('date'):
                        by_date[date_str] = grp
                for d in chunk_missing:
                    date_str = d.isoformat()
                    df_day   = by_date.get(date_str, pd.DataFrame(columns=EMPTY_COLS)).copy()
                    df_day['date'] = date_str
                    df_day.to_parquet(_trader_day_cache_path(trader_id, date_str),
                                      index=False, compression='snappy')
                print(f"  [trader_cache {trader_id}] chunk {idx+1}/{total} "
                      f"({cs.isoformat()}~{ce.isoformat()})", flush=True)
                break
            except requests.exceptions.Timeout:
                time.sleep(2 ** (attempt + 1))
            except Exception as e:
                print(f"  [trader_cache {trader_id}] error chunk {cs}~{ce}: {e}")
                break


def fetch_twstock_broker_net(stock_id, broker_id, start, end, headers,
                             max_workers=10, rate_limit=270, period=300,
                             max_retries=5):
    """台股特定分點每日淨買賣超 (buy - sell 股數).

    broker_id: securities_trader_id, e.g. '9217' for 凱基-松山.
    Local cache: cache/twstock_broker_stock_<stock_id>/<YYYY-MM-DD>.parquet
    每天存全部分點完整資料，任何 broker_id 都可直接從 local cache 過濾，無需重抓。
    只 fetch 尚未 cache 的日期；concurrent requests + token bucket rate limit。
    """
    from datetime import date as _date

    end_dt   = _date.fromisoformat(end)   if end   else _date.today()
    start_dt = _date.fromisoformat(start)

    weekdays = [start_dt + timedelta(days=i)
                for i in range((end_dt - start_dt).days + 1)
                if (start_dt + timedelta(days=i)).weekday() < 5]

    _populate_broker_day_cache(stock_id, weekdays, headers,
                               rate_limit=rate_limit, period=period, max_retries=max_retries)

    frames = []
    for d in weekdays:
        path = _broker_day_cache_path(stock_id, d.isoformat())
        if not path.exists():
            continue
        df_day = pd.read_parquet(path)
        row = df_day[df_day['broker_id'] == broker_id]
        if not row.empty:
            net = float(row['buy'].sum() - row['sell'].sum())
            frames.append({'date': d.isoformat(), 'net': net})

    if not frames:
        return pd.DataFrame(columns=['net'])
    result = pd.DataFrame(frames)
    result['date'] = pd.to_datetime(result['date'])
    return result.set_index('date').sort_index()


def fetch_twstock_all_broker_net(stock_id, start, end, headers,
                                  max_workers=10, rate_limit=270, period=300,
                                  max_retries=5):
    """台股每日全市場所有分點合計淨買賣超 (sum of buy - sell across ALL broker branches).

    Shares the same per-day parquet cache as fetch_twstock_broker_net.
    Returns pd.Series indexed by date (trading days only).
    """
    from datetime import date as _date

    end_dt   = _date.fromisoformat(end)   if end   else _date.today()
    start_dt = _date.fromisoformat(start)

    weekdays = [start_dt + timedelta(days=i)
                for i in range((end_dt - start_dt).days + 1)
                if (start_dt + timedelta(days=i)).weekday() < 5]

    _populate_broker_day_cache(stock_id, weekdays, headers,
                               rate_limit=rate_limit, period=period, max_retries=max_retries)

    frames = []
    for d in weekdays:
        path = _broker_day_cache_path(stock_id, d.isoformat())
        if not path.exists():
            continue
        df_day = pd.read_parquet(path)
        net = float(df_day['buy'].sum() - df_day['sell'].sum()) if not df_day.empty else 0.0
        frames.append({'date': d.isoformat(), 'net': net})

    if not frames:
        return pd.Series(dtype=float, name='net')
    result = pd.DataFrame(frames)
    result['date'] = pd.to_datetime(result['date'])
    return result.set_index('date')['net'].sort_index()


def fetch_twstock_branch_daily_net(stock_id, start, end, headers,
                                    max_workers=10, rate_limit=270, period=300,
                                    max_retries=5):
    """台股每日各分點淨買賣超明細.

    Returns DataFrame shape (dates, broker_ids): each cell = daily net (buy - sell)
    for that branch on that date. Missing branch-day pairs are 0.
    Shares the same per-day parquet cache as fetch_twstock_broker_net.
    """
    from datetime import date as _date

    end_dt   = _date.fromisoformat(end)   if end   else _date.today()
    start_dt = _date.fromisoformat(start)

    weekdays = [start_dt + timedelta(days=i)
                for i in range((end_dt - start_dt).days + 1)
                if (start_dt + timedelta(days=i)).weekday() < 5]

    _populate_broker_day_cache(stock_id, weekdays, headers,
                               rate_limit=rate_limit, period=period, max_retries=max_retries)

    frames = []
    for d in weekdays:
        path = _broker_day_cache_path(stock_id, d.isoformat())
        if not path.exists():
            continue
        df_day = pd.read_parquet(path)
        if df_day.empty:
            frames.append(pd.Series(dtype=float, name=d.isoformat()))
            continue
        df_day['_net'] = df_day['buy'] - df_day['sell']
        net_per_branch = df_day.groupby('broker_id')['_net'].sum()
        net_per_branch.name = d.isoformat()
        frames.append(net_per_branch)

    if not frames:
        return pd.DataFrame()
    result = pd.DataFrame(frames)   # shape: (dates × branches)
    result.index = pd.to_datetime(result.index)
    return result.sort_index().fillna(0.0)


# ── Taiwan fundamental data (quarterly / monthly) ────────────────────────────

def _fundamental_cache_path(prefix, stock_id):
    return _CACHE_DIR / f'{prefix}_{stock_id}.parquet'


def _load_fundamental_cache(path, max_age_days=30):
    if not path.exists():
        return None
    if (time.time() - path.stat().st_mtime) / 86400 > max_age_days:
        return None
    return pd.read_parquet(path)


def _save_fundamental_cache(path, df):
    path.parent.mkdir(exist_ok=True)
    df.to_parquet(path, compression='snappy')


def _fetch_twstock_fundamental_raw(endpoint, stock_id, headers):
    r = _retry_get(f'{BASE}/studio/market/twstock/{endpoint}/{stock_id}',
                   headers=headers, timeout=60)
    data = r.json().get('data', [])
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    return df.set_index('date').sort_index()


def _fetch_fundamental(prefix, endpoint, stock_id, headers):
    path = _fundamental_cache_path(prefix, stock_id)
    df = _load_fundamental_cache(path)
    if df is not None:
        return df
    df = _fetch_twstock_fundamental_raw(endpoint, stock_id, headers)
    if not df.empty:
        _save_fundamental_cache(path, df)
    return df


def fetch_twstock_financials(stock_id, headers):
    """台股季頻綜合損益表 (long format). index=date, columns: type, value, origin_name.
    Key types: Revenue, GrossProfit, OperatingIncome, IncomeAfterTaxes, EPS.
    Pivot: df.pivot_table(index='date', columns='type', values='value', aggfunc='last')"""
    return _fetch_fundamental('twstock_fin', 'financials', stock_id, headers)


def fetch_twstock_balance_sheet(stock_id, headers):
    """台股季頻資產負債表 (long format). index=date, columns: type, value, origin_name.
    Key types: TotalAssets, Equity. ROE = IncomeAfterTaxes / Equity."""
    return _fetch_fundamental('twstock_bs', 'balance_sheet', stock_id, headers)


def fetch_twstock_monthly_revenue(stock_id, headers):
    """台股月營收. index=date, columns: revenue (NTD 元, full amount not thousands), revenue_month, revenue_year.
    YoY = (rev - rev_same_month_last_year) / abs(rev_same_month_last_year)."""
    return _fetch_fundamental('twstock_rev', 'monthly_revenue', stock_id, headers)


def _twstock_list_cache_path():
    return _CACHE_DIR / 'twstock_list.parquet'


def fetch_twstock_list(headers):
    """全市場股票清單（上市+上櫃，含 ETF）。DataFrame indexed by stock_id, columns:
    name, close, industry_code, listing_date (YYYY-MM-DD). Basic company data, not a
    time series — refreshed once a day: single-file cache like fundamentals (see
    references/cache.md), just 1-day TTL instead of 30-day.
    ETFs and other non-company securities have industry_code/listing_date = None/NaN
    (use .notna() to filter, not `is not None` — parquet round-trips None as NaN).
    industry_code is TWSE/TPEx's raw numeric 產業別 code (e.g. '24'=半導體業), not a
    decoded name — group/filter by it, don't assume a fixed label mapping."""
    path = _twstock_list_cache_path()
    df = _load_fundamental_cache(path, max_age_days=1)
    if df is not None:
        return df
    r = _retry_get(f'{BASE}/studio/market/twstock/list', headers=headers, timeout=60)
    data = r.json().get('data', [])
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data).set_index('stock_id')
    _save_fundamental_cache(path, df)
    return df


def fetch_twstock_info(stock_id, headers):
    """單支股票基本資料: {stock_id, name, close, industry_code, listing_date}, or None
    if not currently listed. Looks up within fetch_twstock_list's cached universe
    (same 1-day-fresh data) instead of a separate network call."""
    df = fetch_twstock_list(headers)
    if df.empty or stock_id not in df.index:
        return None
    return {'stock_id': stock_id, **df.loc[stock_id].to_dict()}


def _fetch_fundamental_batch(prefix, endpoint, stock_ids, headers):
    """Batch fetch fundamental data. Returns dict {stock_id: DataFrame}.
    Uses cache first; fetches uncached stocks in chunks of 50 via batch API."""
    results = {}
    uncached = []

    for sid in stock_ids:
        path = _fundamental_cache_path(prefix, sid)
        df = _load_fundamental_cache(path)
        if df is not None:
            results[sid] = df
        else:
            uncached.append(sid)

    for i in range(0, len(uncached), 50):
        chunk = uncached[i:i + 50]
        try:
            r = _retry_get(f'{BASE}/studio/market/twstock/batch/{endpoint}',
                           headers=headers,
                           params={'stock_ids': ','.join(chunk)},
                           timeout=120)
            batch_data = r.json().get('data', {})
            for sid, records in batch_data.items():
                if not records:
                    continue
                df = pd.DataFrame(records)
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date').sort_index()
                _save_fundamental_cache(_fundamental_cache_path(prefix, sid), df)
                results[sid] = df
        except Exception as e:
            print(f'  [batch] {endpoint} chunk {i//50 + 1} error: {e}')

    return results


def fetch_twstock_financials_batch(stock_ids, headers):
    """Batch fetch 台股季頻綜合損益表. Returns dict {stock_id: DataFrame}."""
    return _fetch_fundamental_batch('twstock_fin', 'financials', stock_ids, headers)


def fetch_twstock_balance_sheet_batch(stock_ids, headers):
    """Batch fetch 台股季頻資產負債表. Returns dict {stock_id: DataFrame}."""
    return _fetch_fundamental_batch('twstock_bs', 'balance_sheet', stock_ids, headers)


def fetch_twstock_monthly_revenue_batch(stock_ids, headers):
    """Batch fetch 台股月營收. Returns dict {stock_id: DataFrame}."""
    return _fetch_fundamental_batch('twstock_rev', 'monthly_revenue', stock_ids, headers)


def _mark_empty_months(prefix, sid, start, end):
    """Write empty-marker parquets for every PAST month in [start, end] that has
    no cached file.

    The /batch endpoint only returns months that have data, so the empty early
    months (e.g. institutional before the dataset existed) never get a file from
    _save_monthly — and the next run's extend path would re-fetch every one of
    them. Marking them here keeps a cold batch fetch a true cache hit on re-run.
    """
    cache_dir = _monthly_cache_dir(prefix, {'id': sid})
    cache_dir.mkdir(parents=True, exist_ok=True)
    current_ym = datetime.utcnow().strftime('%Y-%m')
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    for ym in _iter_months(start, end_str):
        if ym >= current_ym:        # never freeze the current (still-growing) month
            continue
        path = cache_dir / f'{ym}.parquet'
        if not path.exists():
            pd.DataFrame().to_parquet(path)


def _fetch_batch_cached(prefix, batch_url, id_param_name, raw_fn, parse_fn, ids, start, end, headers,
                         chunk_size=50, mark_empty_months=True, start_param='start', end_param='end',
                         date_chunk_days=None):
    """Shared batch fetcher for monthly-cached datasets (Taiwan stocks and stock futures).

    Phase 1: ids that already have a local cache are extended (current-month delta).
             The delta itself is fetched through batch_url in chunk_size-id chunks — NOT
             one request per id — then handed to _extend_cache_monthly for the merge/write.
             (Previously this phase called raw_fn per id individually: on a warm run with
             ids in the hundreds that meant that many single-id HTTP calls, each paying
             api_plan_required's full auth cost — bcrypt check + 2 uncached MySQL lookups
             + 2 Redis rate-limit hits — independently, which is what actually made warm
             runs slow, not the local parquet reads. See twstock_momentum backtest timeout,
             2026-07.)
    Phase 2: ids with no local cache yet are fetched the same way, chunk_size ids per
             request, full requested range.

    raw_fn(id, start, end, headers) -> DataFrame   single-id fallback (missing past months)
    parse_fn(records) -> DataFrame                 one id's batch records -> cached frame
    """
    results, uncached = {}, []

    to_extend = []
    for _id in ids:
        cache_dir = _monthly_cache_dir(prefix, {'id': _id})
        if cache_dir.exists() and list(cache_dir.glob('*.parquet')):
            to_extend.append(_id)
        else:
            uncached.append(_id)

    def _date_spans(range_start, range_end):
        """Split [range_start, range_end] into <= date_chunk_days pieces. Some batch
        endpoints (crypto /kline*) silently clamp an over-long single request to their
        own max window instead of erroring — chunking client-side is the only way to
        actually get the full requested range back."""
        if not date_chunk_days:
            return [(range_start, range_end)]
        s = datetime.strptime(range_start, '%Y-%m-%d')
        e = datetime.utcnow() if not range_end else datetime.strptime(range_end, '%Y-%m-%d')
        spans, cursor = [], s
        while cursor <= e:
            span_end = min(cursor + timedelta(days=date_chunk_days), e)
            spans.append((cursor.strftime('%Y-%m-%d'), span_end.strftime('%Y-%m-%d')))
            cursor = span_end + timedelta(days=1)
        return spans

    def _fetch_batch_range(id_list, range_start, range_end):
        """chunk_size ids per request x date_chunk_days-sized date spans, all issued
        concurrently. Returns ({id: DataFrame}, failed_ids): frames merged across spans,
        missing/empty ids simply absent (caller treats absence as 'no data') — EXCEPT
        ids in failed_ids, whose chunk errored or was server-side rate-limited; for
        those, absence is unknown, not 'empty', and must never be cached as empty."""
        out, failed_ids = {}, set()
        id_chunks = [id_list[i:i + chunk_size] for i in range(0, len(id_list), chunk_size)]
        date_spans = _date_spans(range_start, range_end)
        jobs = [(idx, chunk, span) for idx, chunk in enumerate(id_chunks) for span in date_spans]

        def _fetch_chunk(idx, chunk, span):
            span_start, span_end = span
            partial = {}
            try:
                params = {id_param_name: ','.join(chunk), start_param: span_start, end_param: span_end}
                r = _retry_get(batch_url, headers=headers, params=params, timeout=120)
                body = r.json()
                failed = body.get('failed', [])
                if failed:
                    print(f'  [batch] {batch_url} server-side fetch failed (rate limit or upstream error), dropped: {failed}')
                    failed_ids.update(failed)
                for _id, records in body.get('data', {}).items():
                    if records:
                        partial[_id] = _normalise_index(parse_fn(records))
            except Exception as e:
                print(f'  [batch] {batch_url} chunk {idx + 1} {span}: error: {e}')
                failed_ids.update(chunk)
            return partial

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_fetch_chunk, idx, chunk, span) for idx, chunk, span in jobs]
            for future in as_completed(futures):
                for _id, df in future.result().items():
                    out[_id] = pd.concat([out[_id], df]) if _id in out else df

        for _id, df in out.items():
            out[_id] = df[~df.index.duplicated(keep='last')].sort_index()
        return out, failed_ids

    # Pre-fetch the delta for every to_extend id in one batched pass instead of
    # per-id inside _extend_one below. _extend_cache_monthly asks for
    # (last_cached_ts, tomorrow) which falls inside the current month, so a
    # current-month-start..tomorrow batch fetch covers the common case. After a
    # month rollover it ALSO asks each id to complete the previous month (its file
    # was last written mid-month — see _written_before_month_end); when any id
    # needs that, widen the batch window to the previous month's start so those
    # spans are served from the same batched pass — otherwise every warm id would
    # fall back to one single-id HTTP call each (the twstock_momentum-timeout
    # storm, once per month across the whole warm cache). Extra days before each
    # id's own last_ts are harmless, _extend_cache_monthly dedupes by index.
    prefetch_batch, prefetch_failed, prefetch_start = {}, set(), None
    if to_extend:
        now = datetime.utcnow()
        current_ym = now.strftime('%Y-%m')
        prev_ym = (now.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
        tomorrow = (now + timedelta(days=1)).strftime('%Y-%m-%d')

        def _prev_month_needs_completion(_id):
            path = _monthly_cache_dir(prefix, {'id': _id}) / f'{prev_ym}.parquet'
            return not path.exists() or _written_before_month_end(path, prev_ym)

        widen = (prev_ym >= start[:7]
                 and any(_prev_month_needs_completion(_id) for _id in to_extend))
        prefetch_start = f'{prev_ym}-01' if widen else f'{current_ym}-01'
        prefetch_batch, prefetch_failed = _fetch_batch_range(to_extend, prefetch_start, tomorrow)

    def _make_fetch_fn(_id):
        def _fetch(s, e):
            # Any span inside the pre-fetched window (current-month delta, and the
            # previous-month completion after a rollover): serve from the batch.
            if prefetch_start is not None and s >= prefetch_start:
                if _id in prefetch_failed:
                    # Absence is unknown, not 'no data' — serving an empty frame here
                    # would let the previous-month completion path rewrite the month
                    # file (mtime past month end = complete) and freeze the hole.
                    # Raising demotes this id to the phase-2 full-range batch refetch.
                    raise RuntimeError(f'batch prefetch failed for {_id}')
                df = prefetch_batch.get(_id)
                if df is None:
                    return pd.DataFrame()
                return df[(df.index >= s) & (df.index < e)]
            # Deeper past-month holes (rare — a partially-cached id) fall back to
            # the single-id fetch; not worth batching for an edge case.
            return raw_fn(_id, s, e, headers)
        return _fetch

    def _extend_one(_id):
        return _extend_cache_monthly(
            prefix, {'id': _id},
            _make_fetch_fn(_id),
            start, end,
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_extend_one, _id): _id for _id in to_extend}
        for future in as_completed(futures):
            _id = futures[future]
            try:
                results[_id] = future.result()
            except Exception:
                uncached.append(_id)

    fetched, failed_ids = _fetch_batch_range(uncached, start, end)
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end_str) + pd.Timedelta(days=1)
    for _id, df in fetched.items():
        _save_monthly(prefix, {'id': _id}, df)
        # match _extend_cache_monthly's own [start, end] clamp so both phases return
        # the same range regardless of how much extra the raw fetch pulled back
        results[_id] = df[(df.index >= start_ts) & (df.index < end_ts)]
    # Mark every in-range past month with no data as an empty parquet, so the next
    # run is a cache hit instead of re-fetching the empty months. Ids with any
    # failed chunk are skipped for the whole range: a transient fetch failure is
    # not evidence of an empty month, and a wrongly-frozen empty parquet would
    # hide that id's history on every future warm run.
    if start and mark_empty_months:
        for _id in uncached:
            if _id in failed_ids:
                continue
            _mark_empty_months(prefix, _id, start, end)

    return results


def _fetch_twstock_cached_batch(prefix, endpoint, raw_fn, parse_fn, stock_ids, start, end, headers):
    """Shared batch fetcher for monthly-cached 台股 datasets. Thin wrapper over
    _fetch_batch_cached — kept for existing callers (stock_ids param name, 50/chunk)."""
    return _fetch_batch_cached(
        prefix, f'{BASE}/studio/market/twstock/batch/{endpoint}', 'stock_ids',
        raw_fn, parse_fn, stock_ids, start, end, headers, chunk_size=50)


def fetch_twstock_shareholding_batch(stock_ids, start, end, headers):
    """Batch fetch 台股週頻股東人數. Returns dict {stock_id: DataFrame(shareholders)}."""
    def _parse(records):
        df = pd.DataFrame(records)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        total = df[df['level'] == 'total'][['people']].rename(columns={'people': 'shareholders'}).astype(float)
        return total[~total.index.duplicated(keep='last')]
    return _fetch_twstock_cached_batch(
        'twstock_shareholding', 'shareholding', _fetch_twstock_shareholding_raw, _parse,
        stock_ids, start, end, headers)


def fetch_twstock_price_adj_batch(stock_ids, start, end, headers):
    """Batch fetch 台股向後調整日K. Returns dict {stock_id: DataFrame(Open, Close)}."""
    def _parse(records):
        df = pd.DataFrame(records)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()[['open', 'close']].rename(
            columns={'open': 'Open', 'close': 'Close'}).astype(float)
        return df.replace(0, float('nan')).ffill()
    return _fetch_twstock_cached_batch(
        'twstock_price', 'price_adj', _fetch_twstock_price_raw, _parse,
        stock_ids, start, end, headers)


def fetch_twstock_price_batch(stock_ids, start, end, headers):
    """Batch fetch 台股原始日K OHLCV（未除權息）. Returns dict {stock_id: DataFrame(Open,
    High, Low, Close, Volume)}. Same data and cache as fetch_twstock_price — use for
    High/Low-based screens (KD, breakout, range) across many stocks; do NOT use for
    backtesting across ex-dividend dates (use fetch_twstock_price_adj_batch)."""
    def _parse(records):
        df = pd.DataFrame(records)
        df['date'] = pd.to_datetime(df['date'])
        cols = [c for c in ['open', 'high', 'low', 'close', 'volume'] if c in df.columns]
        df = df.set_index('date').sort_index()[cols].rename(
            columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                     'close': 'Close', 'volume': 'Volume'}).astype(float)
        return df.replace(0, float('nan')).ffill()
    results = _fetch_twstock_cached_batch(
        'twstock_price_nonadj', 'price', _fetch_twstock_price_nonadj_raw, _parse,
        stock_ids, start, end, headers)
    return {sid: _sanity_check_ohlc(df, f'{sid} twstock price')
            for sid, df in results.items()}


def fetch_twstock_per_batch(stock_ids, start, end, headers):
    """Batch fetch 台股每日本益比/股價淨值比/殖利率. Returns dict {stock_id:
    DataFrame(dividend_yield, PER, PBR)}. Same data and cache as fetch_twstock_per —
    use for value screens (殖利率 > x%, PER < y) across many stocks."""
    def _parse(records):
        df = pd.DataFrame(records)
        df['date'] = pd.to_datetime(df['date'])
        return df.set_index('date').sort_index()
    return _fetch_twstock_cached_batch(
        'twstock_per', 'per', _fetch_twstock_per_raw, _parse,
        stock_ids, start, end, headers)


def fetch_twstock_institutional_batch(stock_ids, start, end, headers):
    """Batch fetch 台股三大法人. Returns dict {stock_id: DataFrame(foreign_net, ...)}."""
    def _parse(records):
        df = pd.DataFrame(records)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        df['foreign_net'] = df['foreign_buy'] - df['foreign_sell']
        return df.fillna(0)
    return _fetch_twstock_cached_batch(
        'twstock_inst', 'institutional', _fetch_twstock_inst_raw, _parse,
        stock_ids, start, end, headers)


def _fetch_twstock_foreign_shareholding_raw(stock_id, start, end, headers):
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = _retry_get(f'{BASE}/studio/market/twstock/foreign_shareholding/{stock_id}',
                   headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    data = r.json().get('data', [])
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    return df.set_index('date').sort_index()


def fetch_twstock_foreign_shareholding_batch(stock_ids, start, end, headers):
    """Batch fetch 台股外資持股表. Returns dict {stock_id: DataFrame}.
    Key columns: ForeignInvestmentSharesRatio (持股比率%), ForeignInvestmentShares (持股股數),
    ForeignInvestmentRemainRatio (剩餘可投資比率%), NumberOfSharesIssued (已發行股數)."""
    def _parse(records):
        df = pd.DataFrame(records)
        df['date'] = pd.to_datetime(df['date'])
        return df.set_index('date').sort_index()
    return _fetch_twstock_cached_batch(
        'twstock_foreign_sh', 'foreign_shareholding', _fetch_twstock_foreign_shareholding_raw, _parse,
        stock_ids, start, end, headers)


# ── Taiwan market-wide data (大盤) ────────────────────────────────────────────
# 全市場層級,沒有 stock_id 維度。個股層級的同名資料請用上面的 fetch_twstock_* 系列。

def _fetch_twmarket_index_raw(index_id, start, end, headers):
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = _retry_get(f'{BASE}/studio/market/twmarket/index/{index_id}',
                   headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    data = r.json().get('data', [])
    if not data:
        return pd.DataFrame(columns=['Open', 'High', 'Low', 'Close'])
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    return df.set_index('date').sort_index()[['open', 'high', 'low', 'close']].rename(
        columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}).astype(float)


def fetch_twmarket_index(start, end, headers, index_id='TAIEX'):
    """大盤加權指數日K（發行量加權股價指數）. Returns DataFrame with Open/High/Low/Close.
    1999-01-05 起;`TAIEX` 是目前唯一支援的 index_id（其他值 API 回 400）。
    指數本身沒有成交量欄位——大盤成交量/成交金額請用 fetch_twmarket_turnover。"""
    df = _extend_cache_monthly(
        'twmarket_index', {'id': index_id},
        lambda s, e: _fetch_twmarket_index_raw(index_id, s, e, headers),
        start, end,
    )
    return _sanity_check_ohlc(df, f'{index_id} twmarket index')


def _fetch_twmarket_raw(endpoint, columns, start, end, headers):
    """Shared raw fetch for the market-wide (no stock_id) twmarket endpoints."""
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = _retry_get(f'{BASE}/studio/market/twmarket/{endpoint}',
                   headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    data = r.json().get('data', [])
    if not data:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()
    cols = [c for c in columns if c in df.columns]
    return df[cols].astype(float)


_TWMARKET_TURNOVER_COLUMNS = ['volume', 'value', 'trades']
_TWMARKET_INST_COLUMNS = ['foreign', 'investment_trust', 'dealer', 'total']
_TWMARKET_MARGIN_COLUMNS = ['margin_balance', 'margin_balance_prev', 'margin_balance_value',
                            'short_balance', 'short_balance_prev']


def fetch_twmarket_turnover(start, end, headers):
    """全市場每日成交量值（TWSE 集中市場）. Returns DataFrame with columns:
    volume（成交股數,股）、value（成交金額,元）、trades（成交筆數）. 1990-01-04 起。"""
    return _extend_cache_monthly(
        'twmarket_turnover', {'id': 'TWSE'},
        lambda s, e: _fetch_twmarket_raw('turnover', _TWMARKET_TURNOVER_COLUMNS, s, e, headers),
        start, end,
    )


def fetch_twmarket_institutional(start, end, headers):
    """全市場三大法人每日買賣超. Returns DataFrame with columns:
    foreign / investment_trust / dealer / total,皆為淨買賣超金額（元,買 - 賣）。
    2004-04-07 起。外資自營商計入 dealer,不計入 foreign。
    個股層級請改用 fetch_twstock_institutional。"""
    return _extend_cache_monthly(
        'twmarket_institutional', {'id': 'TWSE'},
        lambda s, e: _fetch_twmarket_raw('institutional', _TWMARKET_INST_COLUMNS, s, e, headers),
        start, end,
    )


def fetch_twmarket_margin(start, end, headers):
    """全市場融資融券餘額. Returns DataFrame with columns:
    margin_balance / margin_balance_prev（融資餘額與前日餘額,張）、
    margin_balance_value（融資金額,元）、
    short_balance / short_balance_prev（融券餘額與前日餘額,張）. 2001-01-03 起。"""
    return _extend_cache_monthly(
        'twmarket_margin', {'id': 'TWSE'},
        lambda s, e: _fetch_twmarket_raw('margin', _TWMARKET_MARGIN_COLUMNS, s, e, headers),
        start, end,
    )


def fetch_twmarket_dividend_points(start, end, headers):
    """加權指數每日除息點數 (TAIEX daily index dividend points) — the correction
    term for 正逆價差 fair-basis math. DatetimeIndex, columns: points (index
    points), estimated (bool: False = realized, TR-index derived, from 2003;
    True = forecast — announced dividends + last-year template, zero-filled on
    event-less future weekdays, horizon today+120).

    Deliberately NOT month-file cached (unlike the other twmarket series): the
    estimated leg is recomputed server-side every day and the realized/estimated
    boundary sweeps through the current month, so month files would freeze stale
    forecasts as if they were history. Instead the FULL series (one API call)
    sits in a single-file cache with a 1-hour TTL — realized history rides along
    for free, forecasts are never older than an hour, and cost is capped at 24
    calls/day. Slice locally. Realized leg updates ~17:00 Taipei daily.

    The API's `meta` (estimated_coverage / degraded) rides along in the
    returned frame's `df.attrs['meta']` — pandas attrs survive the parquet
    cache round-trip, so cache hits carry the meta of the fetch that filled
    the cache (same ≤1h freshness as the data itself). Callers whose math
    depends on the estimated leg (e.g. mispricing D(t)) MUST check
    `attrs['meta'].get('degraded')` and refuse to compute on a lower-bound
    estimate; the print below is a courtesy for ad-hoc use, not the guard."""
    path = _CACHE_DIR / 'twmarket_dividend_points.parquet'
    df = _load_fundamental_cache(path, max_age_days=1 / 24)
    if df is None:
        r = _retry_get(f'{BASE}/studio/market/twmarket/dividend_points',
                       headers=headers, timeout=60)
        payload = r.json()
        meta = payload.get('meta') or {}
        if meta.get('degraded'):
            print(f"  [dividend_points] WARNING degraded estimate — synthesis "
                  f"coverage {meta.get('estimated_coverage')}; treat the "
                  f"estimated leg as a lower bound")
        data = payload.get('data', [])
        if not data:
            out = pd.DataFrame(columns=['points', 'estimated'])
            out.attrs['meta'] = meta
            return out
        df = pd.DataFrame(data)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        df.attrs['meta'] = meta
        _save_fundamental_cache(path, df)
    out = df
    if start:
        out = out[out.index >= pd.Timestamp(start)]
    if end:
        out = out[out.index <= pd.Timestamp(end)]
    out.attrs = dict(df.attrs)   # slicing must not drop the meta
    return out


# ── Taiwan futures data ───────────────────────────────────────────────────────

_TW_FUTURES_CHUNK_DAYS = {'1d': 3650, '1m': 28, '5m': 28, '15m': 28, '30m': 28, '60m': 28}


def _fetch_twfutures_raw(symbol, schema, start, end, headers):
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
    chunk_days = _TW_FUTURES_CHUNK_DAYS.get(schema, 28)

    chunks, cursor = [], s
    while cursor < e:
        chunk_end = min(cursor + timedelta(days=chunk_days), e)
        chunks.append((cursor.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
        cursor = chunk_end

    def _fetch_one(cs, ce):
        r = requests.get(
            f'{BASE}/studio/market/twfutures/ohlcv/{symbol}/{schema}',
            headers=headers,
            params={'start': cs, 'end': ce},
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get('data', [])

    rows = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_one, cs, ce): (cs, ce) for cs, ce in chunks}
        for future in as_completed(futures):
            rows.extend(future.result())

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df['time'] = pd.to_datetime(df['ts'], utc=True)
    df = df.set_index('time').sort_index()
    df = df[~df.index.duplicated(keep='first')]
    df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                             'close': 'Close', 'volume': 'Volume'})
    return df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)


class _ExportUnavailable(Exception):
    """Bulk-export endpoint not deployed / symbol not served — fall back to chunked JSON."""


_TW_FUTURES_RESAMPLE_RULES = {'5m': '5min', '15m': '15min', '30m': '30min', '60m': '60min'}


def _fetch_twfutures_via_export(symbol, schema, start, end, headers):
    """Fetch intraday OHLCV via the 1m-parquet bulk export endpoint
    (GET /studio/market/twfutures/ohlcv/<symbol>/export/<year>) and resample locally.

    One request per calendar year, zero server-side computation — the server just
    streams its own year parquet. Resample semantics replicate the server's
    (resample(rule).agg(first/max/min/last/sum), dropna on open), so the output is
    interchangeable with _fetch_twfutures_raw's. Processes one year at a time to
    keep peak memory at ~one year of 1m bars (~20MB), not the full span.

    Raises _ExportUnavailable when the endpoint isn't deployed yet (or rejects the
    symbol) so the caller can fall back to the chunked JSON path.
    """
    import io
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
    rule = _TW_FUTURES_RESAMPLE_RULES.get(schema)  # None for '1m' — no resample

    frames = []
    for year in range(s.year, e.year + 1):
        try:
            # _retry_get backs off on 429/5xx — matters when downloading many
            # year files in a row (100 symbols × 8 years brushes the rate limit)
            r = _retry_get(
                f'{BASE}/studio/market/twfutures/ohlcv/{symbol}/export/{year}',
                headers=headers, timeout=120,
            )
        except requests.HTTPError as exc:
            resp = exc.response
            if resp is not None and resp.status_code == 404:
                try:
                    if resp.json().get('error') == 'no_data':
                        continue  # valid year, just no data (e.g. before backfill start)
                except ValueError:
                    pass
                raise _ExportUnavailable(f'export route missing for {symbol}/{year}')
            raise _ExportUnavailable(f'export {symbol}/{year} -> '
                                     f'{resp.status_code if resp is not None else exc}')

        raw = pd.read_parquet(io.BytesIO(r.content))
        if raw.empty:
            continue
        raw.index = pd.to_datetime(raw['ts'], utc=True)
        data = raw[['open', 'high', 'low', 'close', 'volume']].sort_index()
        data = data[~data.index.duplicated(keep='first')]
        if rule:
            data = data.resample(rule).agg(
                open=('open', 'first'), high=('high', 'max'), low=('low', 'min'),
                close=('close', 'last'), volume=('volume', 'sum'),
            ).dropna(subset=['open'])
        frames.append(data)

    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames).sort_index()
    df = df[(df.index >= pd.Timestamp(start, tz='UTC')) &
            (df.index < pd.Timestamp(e.strftime('%Y-%m-%d'), tz='UTC') + pd.Timedelta(days=1))]
    df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                             'close': 'Close', 'volume': 'Volume'})
    df.index.name = 'time'
    return df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)


def _fetch_twfutures_raw_smart(symbol, schema, start, end, headers):
    """Long intraday spans → try the bulk-export path first (zero server CPU, one
    request per year); short spans, '1d', or export-unavailable → chunked JSON API."""
    if schema != '1d':
        s = datetime.strptime(start, '%Y-%m-%d')
        e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
        if (e - s).days > 62:
            try:
                return _fetch_twfutures_via_export(symbol, schema, start, end, headers)
            except _ExportUnavailable as exc:
                print(f'  [twfutures] export unavailable ({exc}); falling back to chunked fetch')
    return _fetch_twfutures_raw(symbol, schema, start, end, headers)


def fetch_twfutures_ohlcv(symbol, schema, start, end, headers):
    """台灣期貨 OHLCV. Returns DataFrame with Open/High/Low/Close/Volume/Amount columns.

    symbol: 'TXF' ('MXF'/'TMF' accepted as aliases — see below)
    schema: '1d' | '1m' | '5m' | '15m' | '30m' | '60m'
    Volume is in contracts (口數).

    A Shioaji-style 'R1' suffix (TXFR1, MXFR1, CDFR1…) is accepted and mapped to
    the endpoint's own name (TXF…): the underlying series IS the R1 continuous
    near-month, only the naming differs. 'R2' (next-month continuous) is NOT this
    data and is deliberately not mapped — it still 400s server-side.

    'MXF' / 'TMF' are EXECUTION-INSTRUMENT aliases for the TXF series: a
    strategy's SYMBOL declares the contract it actually trades (大台/小台/微台),
    but signals and backtests always run on TXF data — arbitrage pins all three
    to the same price, TXF minute history is the deepest, and the server has no
    TMF minute data at all. Both map to 'TXF' here (shared cache dir), so
    SYMBOL='TMF' fetches TXF bars while the order layer trades TM0000.

    For 1d: index is Asia/Taipei tz so df.index[-1].date() returns the correct trading date.
    """
    symbol = symbol.upper()
    if symbol.endswith('R1') and len(symbol) > 2:
        symbol = symbol[:-2]
    if symbol in ('MXF', 'TMF'):
        symbol = 'TXF'
    df = _extend_cache_monthly(
        f'twfutures_{schema}', {'symbol': symbol},
        lambda s, e: _fetch_twfutures_raw_smart(symbol, schema, s, e, headers),
        start, end,
        empty_marker_ttl_hours=24,   # history is backfilled progressively server-side
    )
    df = _sanity_check_ohlc(df, f'{symbol} {schema} twfutures')
    if schema == '1d' and not df.empty:
        # Cache stores naive UTC (midnight TWN = prev-day 16:00 UTC). Convert to Asia/Taipei
        # so the index date matches the actual trading date.
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True).tz_convert('Asia/Taipei')
    return df


def fetch_twfutures_ohlcv_batch(symbols, schema, start, end, headers, max_workers=8):
    """Batch fetch_twfutures_ohlcv across many symbols, concurrently.

    Same per-symbol semantics (monthly cache, export-first for long intraday
    spans, chunked fallback) — this just runs the symbols through a thread pool
    so the per-request fixed overhead (auth round-trips) is amortised instead
    of paid serially. Safe to parallelise: each symbol has its own cache dir.

    Returns dict {symbol: DataFrame} for symbols that succeeded; failures are
    dropped with a printed warning (mirrors fetch_stock_futures_batch_daily).
    """
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(fetch_twfutures_ohlcv, sym, schema, start, end, headers): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            sym = futures[future]
            try:
                results[sym] = future.result()
            except Exception as e:
                print(f"  [twfutures batch] skip {sym}: {e}")
    return results


def _fetch_twfutures_bid_ask_vol_raw(start, end, headers):
    """Fetch raw bid/ask vol for a date range (≤31 days per chunk)."""
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d') + timedelta(days=1)
    chunk_days = 28

    chunks, cursor = [], s
    while cursor < e:
        chunk_end = min(cursor + timedelta(days=chunk_days), e)
        chunks.append((cursor.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
        cursor = chunk_end

    def _fetch_one(cs, ce):
        r = requests.get(
            f'{BASE}/studio/market/twfutures/bid_ask_vol/TXF',
            headers=headers,
            params={'start': cs, 'end': ce},
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get('data', [])

    rows = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_one, cs, ce): (cs, ce) for cs, ce in chunks}
        for future in as_completed(futures):
            rows.extend(future.result())

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df['time'] = pd.to_datetime(df['ts'], utc=True)
    df = df.set_index('time').sort_index()
    df = df[~df.index.duplicated(keep='first')]
    return df[['bid_vol', 'ask_vol', 'total_vol']].astype(int)


def fetch_twfutures_pcr(start, end, headers):
    """台指選擇權買賣權未平倉量比率（日）. Returns DataFrame with 'pcr' column.

    Source: TAIFEX (台灣期貨交易所). (History range: see the blave-quant skill / Notion API doc.)
    index: date (daily, trading days only)
    pcr: 買賣權未平倉量比率%
    """
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = _retry_get(f'{BASE}/studio/market/twfutures/option/pcr',
                   headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    data = r.json().get('data', [])
    if not data:
        return pd.DataFrame(columns=['pcr'])
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    return df.set_index('date').sort_index()[['pcr']].astype(float)


def fetch_twfutures_bid_ask_vol(start, end, headers):
    """台指期內外盤成交量（1 分鐘）. Returns DataFrame indexed by UTC time.

    Columns: bid_vol (內盤口數), ask_vol (外盤口數), total_vol (總口數).
    Both day session (08:45-13:45 TWN) and night session included.
    (History range: see the blave-quant skill / Notion API doc.)
    Monthly cache: cache/twfutures_bav_TXF/YYYY-MM.parquet
    """
    result = _extend_cache_monthly(
        'twfutures_bav', {'symbol': 'TXF'},
        lambda s, e: _fetch_twfutures_bid_ask_vol_raw(s, e, headers),
        start, end,
        empty_marker_ttl_hours=24,   # history is backfilled progressively server-side
    )
    if result.empty:
        return result
    for col in ['bid_vol', 'ask_vol', 'total_vol']:
        if col in result.columns:
            result[col] = result[col].astype(int)
    return result


def fetch_stock_futures_batch_daily(futures_ids, start, end, headers):
    """Batch daily OHLCV/OI for individual stock futures (max 250 ids per call,
    server-side parallel fetch + cache). Returns dict {futures_id: DataFrame}
    for ids with data; ids that hit persistent upstream rate-limiting are
    dropped and printed as a warning (a genuinely empty dataset for a valid id
    is not an error, just an empty DataFrame — not omitted).

    Locally cached per (futures_id, start, end) under cache/twfutures_stockfut/ —
    an EXACT-range cache, not the monthly-delta cache the other twstock/twfutures
    fetchers use. This dataset has multiple rows per day per id (every listed
    contract month x trading_session), so the monthly cache's dedup-by-date would
    silently collapse those down to one row per day. An exact-range cache avoids
    that at the cost of not supporting incremental "extend to today" delta fetches —
    fine for backtests, which per the END-modes convention re-run with a fixed
    START/END anyway (re-run with the same START/END to get a cache hit; live mode
    with END=None always re-fetches).

    Same fields as fetch_twfutures_daily: date, futures_id, contract_date,
    open, max, min, close, spread, spread_per, volume, settlement_price,
    open_interest, trading_session. futures_ids must be valid stock futures
    ids (股票期貨, e.g. 'CDF') — arbitrary ids are rejected (400).
    """
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    cache_dir = _CACHE_DIR / 'twfutures_stockfut'
    cache_dir.mkdir(parents=True, exist_ok=True)

    results, to_fetch = {}, []
    for fid in futures_ids:
        path = cache_dir / f'{fid}__{start}__{end_str}.parquet'
        if path.exists():
            results[fid] = pd.read_parquet(path)
        else:
            to_fetch.append(fid)

    def _fetch_chunk(chunk):
        r = _retry_get(
            f'{BASE}/studio/market/twfutures/stock_futures/batch/daily',
            headers=headers,
            params={'futures_ids': ','.join(chunk), 'start': start, 'end': end},
            timeout=120,
        )
        body = r.json()
        failed = body.get('failed', [])
        if failed:
            print(f"[fetch_stock_futures_batch_daily] rate-limited after retries, dropped: {failed}")
        for fid, rows in body.get('data', {}).items():
            df = pd.DataFrame(rows)
            df.to_parquet(cache_dir / f'{fid}__{start}__{end_str}.parquet')
            results[fid] = df

    chunks = [to_fetch[i:i + 200] for i in range(0, len(to_fetch), 200)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_fetch_chunk, chunks))

    return results


def fetch_stock_futures_ohlcv_symbols(headers):
    """Currently-allowed symbols for fetch_twfutures_ohlcv (intraday/minute-line
    coverage) — always includes 'TXF' plus whichever individual stock futures
    ids currently have backfilled Shioaji minute-line data (a dynamically-
    growing subset of the 231 total). Returns a plain list of symbol strings.

    Call this before fetch_twfutures_ohlcv on a stock future to check coverage
    up front, instead of trial-and-erroring against the 400 response.

    Entries are suffix-less names ('TXF', 'CDF') — strip a Shioaji-style 'R1'
    suffix before the membership check (fetch_twfutures_ohlcv itself accepts
    'CDFR1' and maps it, but 'CDFR1' will never appear in this list). 'MXF' /
    'TMF' likewise never appear — fetch_twfutures_ohlcv aliases them to 'TXF'.
    """
    r = _retry_get(f'{BASE}/studio/market/twfutures/ohlcv/symbols', headers=headers, timeout=30)
    return r.json().get('data', [])


def txf_settlement_mask(index):
    """Return a boolean Series (same index) that is True on the last bar strictly
    before each TAIFEX monthly settlement (3rd Wednesday, 13:30 TWN).

    Interval-agnostic: 1m data marks the 13:29 bar, 60m data marks the 13:00 bar,
    etc. Applies to every TAIFEX monthly-settled product — TXF and individual
    stock futures share the same settlement calendar — and MUST be applied by any
    strategy on `fetch_twfutures_*` data: the source is Shioaji's R1 continuous
    near-month series, which switches contracts at settlement WITHOUT price
    adjustment, so an unmasked position books the contract-basis gap as fake PnL
    (measured 2018-2026 across 10 stock futures: mean +0.36%/roll, std 3.9%,
    August dividend-season mean -1.9%).

    Usage in compute_signals:
        settle = txf_settlement_mask(df.index)
        signal[settle] = 0.0        # Type A;  Type C: weights.loc[settle] = 0.0
        return signal, settle       # settle doubles as exec_at_close
    """
    import datetime
    import pytz

    twn = pytz.timezone('Asia/Taipei')

    def _third_wed(year, month):
        d, count = datetime.date(year, month, 1), 0
        while True:
            if d.weekday() == 2:
                count += 1
                if count == 3:
                    return d
            d = d + datetime.timedelta(days=1)

    mask  = pd.Series(False, index=index)
    start = index.min()
    end   = index.max()

    year, month = start.year, start.month
    while True:
        wed = _third_wed(year, month)
        ts_settle = pd.Timestamp(
            twn.localize(datetime.datetime(wed.year, wed.month, wed.day, 13, 30))
            .astimezone(pytz.utc)
        )
        if index.tz is None:
            ts_settle = ts_settle.tz_localize(None)
        if ts_settle > end:
            break
        # last bar with label strictly before the settlement moment; guard
        # against marking a far-away bar when the symbol has a data gap
        pos = index.searchsorted(ts_settle) - 1
        if pos >= 0 and (ts_settle - index[pos]) <= pd.Timedelta(days=1):
            mask.iloc[pos] = True
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return mask


def _trader_day_cache_path(trader_id, date_str):
    """cache/twstock_broker_trader_<trader_id>/<date>.parquet"""
    d = _CACHE_DIR / f'twstock_broker_trader_{trader_id}'
    d.mkdir(parents=True, exist_ok=True)
    return d / f'{date_str}.parquet'


def fetch_twstock_trader_flows(trader_id, start, end, headers,
                               rate_limit=270, period=300, max_retries=5, **_kwargs):
    """分點對所有股票每日淨買賣超 (buy - sell).

    trader_id: securities_trader_id, e.g. '9217' for 凱基-松山.
    Local cache: cache/twstock_broker_trader_<trader_id>/<YYYY-MM-DD>.parquet
    每天存全部股票完整資料，只 fetch 沒有的日期（90-day range chunks）。
    Returns long-format DataFrame indexed by (date, stock_id) with 'net' column.
    """
    from datetime import date as _date

    end_dt   = _date.fromisoformat(end)   if end   else _date.today()
    start_dt = _date.fromisoformat(start)

    weekdays = [start_dt + timedelta(days=i)
                for i in range((end_dt - start_dt).days + 1)
                if (start_dt + timedelta(days=i)).weekday() < 5]

    _populate_trader_day_cache(trader_id, weekdays, headers,
                               rate_limit=rate_limit, period=period, max_retries=max_retries)

    frames = []
    for d in weekdays:
        path = _trader_day_cache_path(trader_id, d.isoformat())
        if not path.exists():
            continue
        df_day = pd.read_parquet(path)
        if df_day.empty or 'stock_id' not in df_day.columns:
            continue
        df_day = df_day.copy()
        df_day['net'] = df_day['buy'].astype(float) - df_day['sell'].astype(float)
        df_day['date'] = pd.to_datetime(d.isoformat())
        frames.append(df_day[['date', 'stock_id', 'net']])

    if not frames:
        return pd.DataFrame(columns=['date', 'stock_id', 'net']).set_index(['date', 'stock_id'])
    result = pd.concat(frames, ignore_index=True)
    return result.groupby(['date', 'stock_id'])['net'].sum().to_frame()


def fetch_economic_calendar(headers, start=None, end=None, countries=None,
                            max_priority=None, limit=None, lang='zh'):
    """總經事件行事曆（授權資料源）—— 事件時間、市場預期值、前值、實際值。

    **這是總經事件與其數字的唯一來源。** 任何「本週有什麼重要事件」「這個數據預期多少 /
    前值多少」的問題都用這支，不要自己上網搜、更不要憑記憶寫數字：實測 agent 自行搜尋會
    整張抄到內容農場的錯誤表格（把已公布的實際值當成預期值），沒抄到的欄位再用訓練資料
    填空，連「前值」這種唯一解的數字都會寫錯，也會把 A 指標的數字安到 B 指標上。
    查不到的欄位就說查不到，不要填。

    資料只涵蓋前後約五週（滾動窗口，非歷史庫）；超出範圍的區間回空 DataFrame，不是錯誤。

    start / end: 'YYYY-MM-DD' 台北日期，含頭含尾（省略 = 不限）。
    countries:   ISO 兩碼 list，如 ['US', 'CN', 'TW']（省略 = 全部）。
    max_priority: 只回 priority <= 此值。**priority 1 最重要、3 最不重要**（1 是非農、
                 利率決議這種級別）—— 所以「只要最重要的事件」是 max_priority=1，不是 3。
    limit:       筆數上限（依事件時間排序後截斷）。
    lang:        指標與國名的顯示語言，'zh'（預設）或 'en'。伺服器只換掉對照表裡有的
                 名稱，沒收錄的維持原本的中文，所以 'en' 會拿到中英混雜的結果。

    Returns DataFrame sorted by event time, one row per event:
      datetime      台北時間（time 為 null 的事件用當日 00:00）
      date / time   台北日期、'HH:MM'（部分事件沒有公布時間，time 為 None）
      country       ISO 兩碼 / country_name 中文國名
      subject       指標名稱；subject_title 是期別，如 '<7月>'、'<2季>'
      predict       市場預期（consensus）；未提供為 None
      last          前值
      real          實際值；尚未公布為 None
      unit          單位（'%'、'point'、'億USD' …）
      priority      1~3，1 最重要
    """
    params = {'lang': lang}
    if start:
        params['start'] = start
    if end:
        params['end'] = end
    if countries:
        params['country'] = ','.join(countries)
    if max_priority is not None:
        params['max_priority'] = max_priority
    if limit is not None:
        params['limit'] = limit

    r = _retry_get(f'{BASE}/studio/market/anue/economic_calendar',
                   headers=headers, params=params, timeout=60)
    payload = r.json()
    data = payload.get('data', []) if isinstance(payload, dict) else payload
    if not data:
        return pd.DataFrame(columns=['datetime', 'date', 'time', 'country', 'country_name',
                                     'subject_title', 'subject', 'predict', 'last', 'real',
                                     'unit', 'priority'])

    df = pd.DataFrame(data).rename(columns={
        'countryId': 'country', 'countryName': 'country_name', 'subjectTitle': 'subject_title',
    })
    # startDate 是事件當天（台北日期）的 epoch 秒；time 是台北時間的 'HH:MM'
    df['date'] = pd.to_datetime(df['startDate'], unit='s').dt.normalize()
    df['datetime'] = df['date'] + pd.to_timedelta(
        df['time'].fillna('00:00') + ':00'
    )
    # Re-apply the filters locally: this workspace and the API deploy on separate
    # schedules, so an older server silently ignores the query params and hands
    # back all ~1,400 rows. Filtering here keeps the contract true either way.
    if start:
        df = df[df['date'] >= pd.Timestamp(start)]
    if end:
        df = df[df['date'] <= pd.Timestamp(end)]
    if countries:
        wanted = {c.upper() for c in countries}
        df = df[df['country'].str.upper().isin(wanted)]
    if max_priority is not None:
        df = df[df['priority'] <= max_priority]  # 上游 1 最重要、3 最不重要

    cols = ['datetime', 'date', 'time', 'country', 'country_name', 'subject_title',
            'subject', 'predict', 'last', 'real', 'unit', 'priority']
    df = df[[c for c in cols if c in df.columns]].sort_values('datetime').reset_index(drop=True)
    return df.head(limit) if limit else df
