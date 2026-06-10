# Local Cache System

All data fetched via `lib/data.py` is cached locally under `cache/` to avoid redundant API calls.

## Directory structure

```
cache/
├── kline_1h_BTCUSDT/          ← one directory per (fetcher, params)
│   ├── 2022-01.parquet
│   ├── 2022-02.parquet
│   └── ...
├── twfutures_1m_TXF/
│   ├── 2020-03.parquet
│   ├── 2020-04.parquet
│   └── ...
├── twfutures_bav_TXF/         ← bid/ask vol (monthly since 2022-01)
│   └── ...
├── twstock_price_2330/        ← per stock, monthly
│   └── ...
├── twstock_broker_stock_2330/ ← per-day files (broker data only)
│   ├── 2024-01-02.parquet
│   └── ...
└── twstock_fin_2330.parquet   ← fundamental data (single file, 30-day TTL)
```

## Monthly partitioning

All time-series data (kline, alpha indicators, TW futures, TW stock price/inst/shareholding) uses **monthly parquet files**:

- **Past months** (`YYYY-MM < current month`): fetched once, stored immutably. Never re-fetched.
- **Current month**: delta-updated on each call — loads cached file, fetches only new bars since the last cached bar, merges and saves.

### Why monthly

| Old approach | Monthly approach |
|---|---|
| One file per `(prefix, params, start)` | One file per `(prefix, params, month)` |
| `start=2022-01` and `start=2023-01` → two separate files with overlapping data | Any `start` reads the same monthly files — no duplication |
| Changing `start` triggers full re-download | Changing `start` just reads different months, all already cached |
| Growing file re-saved entirely on every delta fetch | Only current month file is re-saved |

## Cache helpers (internal)

```python
_monthly_cache_dir(prefix, params)   # → cache/{prefix}_{params}/
_extend_cache_monthly(prefix, params, fetch_raw_fn, start, end)  # load+update monthly files
_save_monthly(prefix, params, df)    # split df by month, save each month (used by batch fetchers)
_normalise_index(df)                 # convert tz-aware → tz-naive UTC
```

## What is NOT monthly-partitioned

| Type | Cache style | Location |
|---|---|---|
| Broker/trader per-day flows | One file per trading day | `cache/twstock_broker_stock_{id}/{date}.parquet` |
| Fundamental data (financials, balance sheet, revenue) | Single file, 30-day mtime TTL | `cache/twstock_fin_{id}.parquet` |

These types have their own caching logic and are not affected by the monthly system.

## Stale cache from old format

Old-format files (`twfutures_1m_TXF_2020-03-22.parquet`, `kline_1h_BTCUSDT_2022-01-01.parquet`, etc.) in `cache/` root are ignored by the new code — they are flat files, not directories. They can be deleted once the monthly cache has been populated.
