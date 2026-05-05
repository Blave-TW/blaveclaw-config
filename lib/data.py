import requests
import pandas as pd
from datetime import datetime, timedelta


def fetch_kline(symbol, interval, start, end, headers):
    """Fetch OHLCV kline data from Blave API with annual chunking."""
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.utcnow() if not end else datetime.strptime(end, '%Y-%m-%d')
    rows, cursor = [], s
    while cursor < e:
        chunk_end = min(cursor + timedelta(days=365), e)
        r = requests.get('https://api.blave.org/kline', headers=headers, params={
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
