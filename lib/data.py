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
_CACHE_DIR = Path('cache')


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


def _cache_path(prefix, params, start):
    """Build cache file path. Key = prefix + sorted param values + start date."""
    param_str = '_'.join(str(v) for _, v in sorted(params.items()))
    return _CACHE_DIR / f'{prefix}_{param_str}_{start}.parquet'


def _extend_cache(path, fetch_raw_fn, start, end):
    """Load cache, fetch only missing delta, save, return trimmed df."""
    e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
    end_ts = pd.Timestamp(end) + pd.Timedelta(days=1) if end else None  # tz-naive cutoff

    if path.exists():
        cached = pd.read_parquet(path)
        # normalize index to tz-naive to avoid tz-aware vs tz-naive TypeError
        if cached.index.tz is not None:
            cached.index = cached.index.tz_convert('UTC').tz_localize(None)
        last = cached.index[-1].to_pydatetime()
        if end and last >= e - timedelta(days=1):
            return cached[cached.index < end_ts]
        new_df = fetch_raw_fn(last.strftime('%Y-%m-%d'), None)
        if new_df.index.tz is not None:
            new_df.index = new_df.index.tz_convert('UTC').tz_localize(None)
        df     = pd.concat([cached, new_df])
    else:
        df = fetch_raw_fn(start, end)

    # normalize before saving so cache is always tz-naive
    if df.index.tz is not None:
        df.index = df.index.tz_convert('UTC').tz_localize(None)
    df = df[~df.index.duplicated(keep='last')].sort_index()
    path.parent.mkdir(exist_ok=True)
    df.to_parquet(path)

    if end_ts is not None:
        return df[df.index < end_ts]
    return df


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
    params = {'symbol': symbol, 'period': interval}
    return _extend_cache(
        _cache_path('kline', params, start),
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
    return _extend_cache(
        _cache_path(slug, params, start),
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
    return _extend_cache(
        _cache_path(f'db_{slug}', {'dataset': dataset.replace('.', ''), 'symbol': symbol}, start),
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
    """台股向後調整日K. Returns DataFrame with Open/Close columns."""
    return _extend_cache(
        _cache_path('twstock_price', {'id': stock_id}, start),
        lambda s, e: _fetch_twstock_price_raw(stock_id, s, e, headers),
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
    return _extend_cache(
        _cache_path('twstock_inst', {'id': stock_id}, start),
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
    return _extend_cache(
        _cache_path('twstock_shareholding', {'id': stock_id}, start),
        lambda s, e: _fetch_twstock_shareholding_raw(stock_id, s, e, headers),
        start, end,
    )


def _broker_day_cache_path(stock_id, date_str):
    """cache/twstock_broker_stock_<stock_id>/<date>.parquet"""
    d = _CACHE_DIR / f'twstock_broker_stock_{stock_id}'
    d.mkdir(parents=True, exist_ok=True)
    return d / f'{date_str}.parquet'


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

    missing = [d for d in weekdays if not _broker_day_cache_path(stock_id, d.isoformat()).exists()]

    # ── fetch missing dates ───────────────────────────────────────────────────
    if missing:
        total   = len(missing)
        limiter = _RateLimiter(rate_limit, period)
        counter = {'done': 0}
        _lock   = threading.Lock()

        def fetch_one(cur):
            date_str = cur.isoformat()
            for attempt in range(max_retries):
                try:
                    limiter.acquire()
                    r = requests.get(
                        f'{BASE}/studio/market/twstock/broker/stock/{stock_id}',
                        headers=headers,
                        params={'date': date_str},
                        timeout=30,
                    )
                    if r.status_code == 429:
                        time.sleep(2 ** (attempt + 1))
                        continue
                    if r.status_code >= 500:
                        time.sleep(2 ** (attempt + 1))
                        continue
                    r.raise_for_status()
                    data = r.json().get('data', [])
                    path = _broker_day_cache_path(stock_id, date_str)
                    df_day = pd.DataFrame(data) if data else pd.DataFrame(columns=['date','stock_id','broker_id','broker_name','price','buy','sell'])
                    df_day['date'] = date_str
                    df_day.to_parquet(path, index=False, compression='snappy')
                    return date_str
                except requests.exceptions.Timeout:
                    time.sleep(2 ** (attempt + 1))
                except Exception as e:
                    print(f"  [broker_net] error {date_str}: {e}")
                    return date_str
            return date_str

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch_one, d): d for d in missing}
            for future in as_completed(futures):
                future.result()
                with _lock:
                    counter['done'] += 1
                    done = counter['done']
                if done % 50 == 0 or done == total:
                    print(f"  [broker_net] {done}/{total} fetched", flush=True)

    # ── read from local cache and filter ─────────────────────────────────────
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
    """
    return _extend_cache(
        _cache_path(f'twfutures_{schema}', {'symbol': symbol}, start),
        lambda s, e: _fetch_twfutures_raw(symbol, schema, s, e, headers),
        start, end,
    )


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
                               max_workers=10, rate_limit=270, period=300, max_retries=5):
    """分點對所有股票每日淨買賣超 (buy - sell).

    trader_id: securities_trader_id, e.g. '9217' for 凱基-松山.
    Local cache: cache/twstock_broker_trader_<trader_id>/<YYYY-MM-DD>.parquet
    每天存全部股票完整資料，只 fetch 沒有的日期。
    Returns long-format DataFrame indexed by (date, stock_id) with 'net' column.
    """
    from datetime import date as _date

    end_dt   = _date.fromisoformat(end)   if end   else _date.today()
    start_dt = _date.fromisoformat(start)

    weekdays = [start_dt + timedelta(days=i)
                for i in range((end_dt - start_dt).days + 1)
                if (start_dt + timedelta(days=i)).weekday() < 5]

    missing = [d for d in weekdays if not _trader_day_cache_path(trader_id, d.isoformat()).exists()]

    if missing:
        total   = len(missing)
        limiter = _RateLimiter(rate_limit, period)
        counter = {'done': 0}
        _lock   = threading.Lock()

        def fetch_one(cur):
            date_str = cur.isoformat()
            for attempt in range(max_retries):
                try:
                    limiter.acquire()
                    r = requests.get(
                        f'{BASE}/studio/market/twstock/broker/trader/{trader_id}',
                        headers=headers,
                        params={'date': date_str},
                        timeout=30,
                    )
                    if r.status_code == 429:
                        time.sleep(2 ** (attempt + 1))
                        continue
                    if r.status_code >= 500:
                        time.sleep(2 ** (attempt + 1))
                        continue
                    r.raise_for_status()
                    data = r.json().get('data', [])
                    path = _trader_day_cache_path(trader_id, date_str)
                    df_day = pd.DataFrame(data) if data else pd.DataFrame(
                        columns=['date', 'broker_id', 'broker_name', 'stock_id', 'price', 'buy', 'sell'])
                    df_day['date'] = date_str
                    df_day.to_parquet(path, index=False, compression='snappy')
                    return date_str
                except requests.exceptions.Timeout:
                    time.sleep(2 ** (attempt + 1))
                except Exception as e:
                    print(f"  [trader_flows] error {date_str}: {e}")
                    return date_str
            return date_str

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch_one, d): d for d in missing}
            for future in as_completed(futures):
                future.result()
                with _lock:
                    counter['done'] += 1
                    done = counter['done']
                if done % 50 == 0 or done == total:
                    print(f"  [trader_flows] {done}/{total} fetched", flush=True)

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
