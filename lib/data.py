import requests
import pandas as pd
from datetime import datetime, timedelta

BASE = 'https://api.blave.org'


def fetch_kline(symbol, interval, start, end, headers):
    """Fetch OHLCV kline data from Blave API with annual chunking."""
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
    rows, cursor = [], s
    while cursor < e:
        chunk_end = min(cursor + timedelta(days=365), e)
        r = requests.get(f'{BASE}/kline', headers=headers, params={
            'symbol': symbol, 'period': interval,
            'start_date': cursor.strftime('%Y-%m-%d'),
            'end_date':   chunk_end.strftime('%Y-%m-%d'),
        }, timeout=60)
        r.raise_for_status()
        rows.extend(r.json())
        cursor = chunk_end
    df = pd.DataFrame(rows)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df = df.set_index('time').sort_index()
    df = df[~df.index.duplicated(keep='first')]
    df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'})
    df['Volume'] = 0
    return df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)


# ── Alpha data (annual chunking, returns DatetimeIndex DataFrame) ─────────────

def _fetch_alpha(endpoint, params, headers, start, end):
    """Internal: fetch alpha endpoint with annual chunking. Returns DataFrame with 'alpha' column."""
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
    ts_list, alpha_list, cursor = [], [], s
    while cursor < e:
        chunk_end = min(cursor + timedelta(days=365), e)
        r = requests.get(f'{BASE}/{endpoint}', headers=headers, params={
            **params,
            'start_date': cursor.strftime('%Y-%m-%d'),
            'end_date':   chunk_end.strftime('%Y-%m-%d'),
        }, timeout=60)
        r.raise_for_status()
        data = r.json().get('data', {})
        ts_list.extend(data.get('timestamp', []))
        alpha_list.extend(data.get('alpha', []))
        cursor = chunk_end
    df = pd.DataFrame({
        'time':  pd.to_datetime(ts_list, unit='s', utc=True),
        'alpha': pd.to_numeric(alpha_list, errors='coerce'),
    }).set_index('time').sort_index()
    return df[~df.index.duplicated(keep='first')]


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


# ── Taiwan stock data ─────────────────────────────────────────────────────────

def fetch_twstock_price_adj(stock_id, start, end, headers):
    """台股向後調整日K. Returns DataFrame with Open/Close columns."""
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = requests.get(f'{BASE}/studio/market/twstock/price_adj/{stock_id}',
                     headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    r.raise_for_status()
    df = pd.DataFrame(r.json()['data'])
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()[['open', 'close']].rename(
        columns={'open': 'Open', 'close': 'Close'}).astype(float)
    return df.replace(0, float('nan')).ffill()


def fetch_twstock_institutional(stock_id, start, end, headers):
    """台股三大法人每日買賣超. Returns DataFrame with foreign_net and raw columns."""
    end_str = end or datetime.utcnow().strftime('%Y-%m-%d')
    r = requests.get(f'{BASE}/studio/market/twstock/institutional/{stock_id}',
                     headers=headers, params={'start': start, 'end': end_str}, timeout=60)
    r.raise_for_status()
    df = pd.DataFrame(r.json()['data'])
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()
    df['foreign_net'] = df['foreign_buy'] - df['foreign_sell']
    return df.fillna(0)
