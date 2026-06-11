import time
import threading
import requests
import pandas as pd
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
    """GET with exponential backoff on 429 (2, 4, 8, 16, 32, 64 s)."""
    for attempt in range(max_retries):
        r = requests.get(url, **kwargs)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        wait = 2 ** (attempt + 1)
        print(f"  429 rate limit — retrying in {wait}s ({url.split('/')[-2]}/{url.split('/')[-1]})")
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


def _normalise_index(df):
    """Convert tz-aware index to tz-naive UTC in-place-safe copy."""
    if df.index.tz is not None:
        df = df.copy()
        df.index = df.index.tz_convert('UTC').tz_localize(None)
    return df


def _extend_cache_monthly(prefix, params, fetch_raw_fn, start, end):
    """Monthly-partitioned cache.

    Past months (before current month) are stored immutably — fetched once, never re-fetched.
    Current month is delta-updated: load cached, fetch from last bar to now, merge.

    Directory: cache/{prefix}_{params}/
    Files:     YYYY-MM.parquet  (one per month)
    """
    cache_dir = _monthly_cache_dir(prefix, params)
    cache_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.utcnow()
    end_str   = end or now.strftime('%Y-%m-%d')
    current_ym = now.strftime('%Y-%m')
    tomorrow   = (now + timedelta(days=1)).strftime('%Y-%m-%d')

    frames = []
    for ym in _iter_months(start, end_str):
        path    = cache_dir / f'{ym}.parquet'
        ym_start = f'{ym}-01'
        ym_end   = f'{_next_month(ym)}-01'   # exclusive upper bound when fetching

        if ym < current_ym:
            # Past month — immutable: fetch once, never touch again
            if not path.exists():
                df = fetch_raw_fn(ym_start, ym_end)
                if df.empty:
                    continue
                df = _normalise_index(df)
                df = df[~df.index.duplicated(keep='last')].sort_index()
                df.to_parquet(path)
            frames.append(pd.read_parquet(path))
        else:
            # Current month — may have new bars
            if path.exists():
                cached = _normalise_index(pd.read_parquet(path))
                last_ts = cached.index[-1].strftime('%Y-%m-%d')
                delta = fetch_raw_fn(last_ts, tomorrow)
                if not delta.empty:
                    delta  = _normalise_index(delta)
                    merged = pd.concat([cached, delta])
                    merged = merged[~merged.index.duplicated(keep='last')].sort_index()
                    merged.to_parquet(path)
                    frames.append(merged)
                else:
                    frames.append(cached)
            else:
                df = fetch_raw_fn(ym_start, tomorrow)
                if df.empty:
                    continue
                df = _normalise_index(df)
                df = df[~df.index.duplicated(keep='last')].sort_index()
                df.to_parquet(path)
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

def _fetch_kline_raw(symbol, interval, start, end, headers):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
    chunks, cursor = [], s
    while cursor < e:
        chunk_end = min(cursor + timedelta(days=365), e)
        chunks.append((cursor.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
        cursor = chunk_end

    def _fetch_one(cs, ce):
        r = requests.get(f'{BASE}/kline', headers=headers, params={
            'symbol': symbol, 'period': interval,
            'start_date': cs, 'end_date': ce,
        }, timeout=60)
        r.raise_for_status()
        return r.json()

    rows = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one, cs, ce): (cs, ce) for cs, ce in chunks}
        for future in as_completed(futures):
            rows.extend(future.result())

    df = pd.DataFrame(rows)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df = df.set_index('time').sort_index()
    df = df[~df.index.duplicated(keep='first')]
    df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'})
    df['Volume'] = 0
    return df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)


def fetch_kline(symbol, interval, start, end, headers):
    """Fetch OHLCV kline data from Blave API with annual chunking and local cache."""
    return _extend_cache_monthly(
        'kline', {'symbol': symbol, 'period': interval},
        lambda s, e: _fetch_kline_raw(symbol, interval, s, e, headers),
        start, end,
    )


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
    return _extend_cache_monthly(
        f'db_{slug}', {'dataset': dataset.replace('.', ''), 'symbol': symbol},
        lambda s, e: _fetch_db_raw(dataset, symbol, schema, s, e, headers),
        start, end,
    )


# ── Taiwan stock data ─────────────────────────────────────────────────────────

def _fetch_twstock_price_raw(stock_id, start, end, headers):
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = _retry_get(f'{BASE}/studio/market/twstock/price_adj/{stock_id}',
                   headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    df = pd.DataFrame(r.json()['data'])
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
    df = pd.DataFrame(r.json()['data'])
    df['date'] = pd.to_datetime(df['date'])
    cols = [c for c in ['open', 'high', 'low', 'close', 'volume'] if c in df.columns]
    df = df.set_index('date').sort_index()[cols].rename(
        columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}).astype(float)
    return df.replace(0, float('nan')).ffill()


def fetch_twstock_price(stock_id, start, end, headers):
    """台股原始日K（未除權息）. Returns DataFrame with Open/High/Low/Close/Volume columns.
    Use for visualization/charting — matches prices users see on broker apps.
    Do NOT use for backtesting (dividends cause artificial price drops that distort signals)."""
    return _extend_cache_monthly(
        'twstock_price_nonadj', {'id': stock_id},
        lambda s, e: _fetch_twstock_price_nonadj_raw(stock_id, s, e, headers),
        start, end,
    )


def _fetch_twstock_inst_raw(stock_id, start, end, headers):
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = _retry_get(f'{BASE}/studio/market/twstock/institutional/{stock_id}',
                   headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    df = pd.DataFrame(r.json()['data'])
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


def fetch_twstock_shareholding_batch(stock_ids, start, end, headers):
    """Batch fetch 台股週頻股東人數. Returns dict {stock_id: DataFrame(shareholders)}."""
    results = {}
    uncached = []

    for sid in stock_ids:
        cache_dir = _monthly_cache_dir('twstock_shareholding', {'id': sid})
        if cache_dir.exists() and list(cache_dir.glob('*.parquet')):
            try:
                results[sid] = _extend_cache_monthly(
                    'twstock_shareholding', {'id': sid},
                    lambda s, e, _sid=sid: _fetch_twstock_shareholding_raw(_sid, s, e, headers),
                    start, end,
                )
                continue
            except Exception:
                pass
        uncached.append(sid)

    for i in range(0, len(uncached), 50):
        chunk = uncached[i:i + 50]
        try:
            params = {'stock_ids': ','.join(chunk)}
            if start:
                params['start'] = start
            if end:
                params['end'] = end
            r = _retry_get(f'{BASE}/studio/market/twstock/batch/shareholding',
                           headers=headers, params=params, timeout=120)
            batch_data = r.json().get('data', {})
            for sid, records in batch_data.items():
                if not records:
                    continue
                df = pd.DataFrame(records)
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date').sort_index()
                total = df[df['level'] == 'total'][['people']].rename(columns={'people': 'shareholders'}).astype(float)
                total = total[~total.index.duplicated(keep='last')]
                _save_monthly('twstock_shareholding', {'id': sid}, total)
                results[sid] = total
        except Exception as e:
            print(f'  [batch] shareholding chunk {i//50 + 1} error: {e}')

    return results


def fetch_twstock_price_adj_batch(stock_ids, start, end, headers):
    """Batch fetch 台股向後調整日K. Returns dict {stock_id: DataFrame(Open, Close)}."""
    results = {}
    uncached = []

    for sid in stock_ids:
        cache_dir = _monthly_cache_dir('twstock_price', {'id': sid})
        if cache_dir.exists() and list(cache_dir.glob('*.parquet')):
            try:
                results[sid] = _extend_cache_monthly(
                    'twstock_price', {'id': sid},
                    lambda s, e, _sid=sid: _fetch_twstock_price_raw(_sid, s, e, headers),
                    start, end,
                )
                continue
            except Exception:
                pass
        uncached.append(sid)

    for i in range(0, len(uncached), 50):
        chunk = uncached[i:i + 50]
        try:
            params = {'stock_ids': ','.join(chunk)}
            if start:
                params['start'] = start
            if end:
                params['end'] = end
            r = _retry_get(f'{BASE}/studio/market/twstock/batch/price_adj',
                           headers=headers, params=params, timeout=120)
            batch_data = r.json().get('data', {})
            for sid, records in batch_data.items():
                if not records:
                    continue
                df = pd.DataFrame(records)
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date').sort_index()[['open', 'close']].rename(
                    columns={'open': 'Open', 'close': 'Close'}).astype(float)
                df = df.replace(0, float('nan')).ffill()
                _save_monthly('twstock_price', {'id': sid}, df)
                results[sid] = df
        except Exception as e:
            print(f'  [batch] price_adj chunk {i//50 + 1} error: {e}')

    return results


def fetch_twstock_institutional_batch(stock_ids, start, end, headers):
    """Batch fetch 台股三大法人. Returns dict {stock_id: DataFrame(foreign_net, ...)}."""
    results = {}
    uncached = []

    for sid in stock_ids:
        cache_dir = _monthly_cache_dir('twstock_inst', {'id': sid})
        if cache_dir.exists() and list(cache_dir.glob('*.parquet')):
            try:
                results[sid] = _extend_cache_monthly(
                    'twstock_inst', {'id': sid},
                    lambda s, e, _sid=sid: _fetch_twstock_inst_raw(_sid, s, e, headers),
                    start, end,
                )
                continue
            except Exception:
                pass
        uncached.append(sid)

    for i in range(0, len(uncached), 50):
        chunk = uncached[i:i + 50]
        try:
            params = {'stock_ids': ','.join(chunk)}
            if start:
                params['start'] = start
            if end:
                params['end'] = end
            r = _retry_get(f'{BASE}/studio/market/twstock/batch/institutional',
                           headers=headers, params=params, timeout=120)
            batch_data = r.json().get('data', {})
            for sid, records in batch_data.items():
                if not records:
                    continue
                df = pd.DataFrame(records)
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date').sort_index()
                df['foreign_net'] = df['foreign_buy'] - df['foreign_sell']
                df = df.fillna(0)
                _save_monthly('twstock_inst', {'id': sid}, df)
                results[sid] = df
        except Exception as e:
            print(f'  [batch] institutional chunk {i//50 + 1} error: {e}')

    return results


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
    results = {}
    uncached = []

    for sid in stock_ids:
        cache_dir = _monthly_cache_dir('twstock_foreign_sh', {'id': sid})
        if cache_dir.exists() and list(cache_dir.glob('*.parquet')):
            try:
                results[sid] = _extend_cache_monthly(
                    'twstock_foreign_sh', {'id': sid},
                    lambda s, e, _sid=sid: _fetch_twstock_foreign_shareholding_raw(_sid, s, e, headers),
                    start, end,
                )
                continue
            except Exception:
                pass
        uncached.append(sid)

    for i in range(0, len(uncached), 50):
        chunk = uncached[i:i + 50]
        try:
            params = {'stock_ids': ','.join(chunk)}
            if start:
                params['start'] = start
            if end:
                params['end'] = end
            r = _retry_get(f'{BASE}/studio/market/twstock/batch/foreign_shareholding',
                           headers=headers, params=params, timeout=120)
            batch_data = r.json().get('data', {})
            for sid, records in batch_data.items():
                if not records:
                    continue
                df = pd.DataFrame(records)
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date').sort_index()
                _save_monthly('twstock_foreign_sh', {'id': sid}, df)
                results[sid] = df
        except Exception as e:
            print(f'  [batch] foreign_shareholding chunk {i//50 + 1} error: {e}')

    return results


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


def fetch_twfutures_ohlcv(symbol, schema, start, end, headers):
    """台灣期貨 OHLCV. Returns DataFrame with Open/High/Low/Close/Volume/Amount columns.

    symbol: 'TXF'
    schema: '1d' | '1m' | '5m' | '15m' | '30m' | '60m'
    Volume is in contracts (口數).

    For 1d: index is Asia/Taipei tz so df.index[-1].date() returns the correct trading date.
    """
    df = _extend_cache_monthly(
        f'twfutures_{schema}', {'symbol': symbol},
        lambda s, e: _fetch_twfutures_raw(symbol, schema, s, e, headers),
        start, end,
    )
    if schema == '1d' and not df.empty:
        # Cache stores naive UTC (midnight TWN = prev-day 16:00 UTC). Convert to Asia/Taipei
        # so the index date matches the actual trading date.
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True).tz_convert('Asia/Taipei')
    return df


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

    Data from TAIFEX (台灣期貨交易所), from 2001-12-24.
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
    Data from 2022-01-04. Both day session (08:45-13:45 TWN) and night session included.
    Monthly cache: cache/twfutures_bav_TXF/YYYY-MM.parquet
    """
    result = _extend_cache_monthly(
        'twfutures_bav', {'symbol': 'TXF'},
        lambda s, e: _fetch_twfutures_bid_ask_vol_raw(s, e, headers),
        start, end,
    )
    if result.empty:
        return result
    for col in ['bid_vol', 'ask_vol', 'total_vol']:
        if col in result.columns:
            result[col] = result[col].astype(int)
    return result


def txf_settlement_mask(index):
    """Return a boolean Series (same index) that is True on the last 1-min bar
    before TXF monthly settlement (3rd Wednesday of each month, 13:30 TWN).

    Usage in compute_signals:
        settle = txf_settlement_mask(df.index)
        signal[settle] = 0.0
        return signal, settle   # exec_at_close
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

    settlement_bars = set()
    start = index.min().to_pydatetime()
    end   = index.max().to_pydatetime()
    year, month = start.year, start.month
    while True:
        wed = _third_wed(year, month)
        ts_settle = twn.localize(
            datetime.datetime(wed.year, wed.month, wed.day, 13, 30)
        ).astimezone(datetime.timezone.utc).replace(tzinfo=None)
        if ts_settle > end:
            break
        last_bar = ts_settle - datetime.timedelta(minutes=1)
        settlement_bars.add(last_bar)
        month += 1
        if month > 12:
            month, year = 1, year + 1

    mask = pd.Series(False, index=index)
    for ts in settlement_bars:
        if ts in index:
            mask[ts] = True
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
