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
├── twstock_price_2330.parquet      ← daily 台股/台市 datasets: ONE file per stock, coverage meta in the parquet footer (see below)
├── twstock_broker_stock_2330/ ← per-day files (broker data only)
│   ├── 2024-01-02.parquet
│   └── ...
└── twstock_fin_2330.parquet   ← fundamental data (single file, 30-day TTL)
```

## Monthly partitioning

Intraday / minute-frequency time series (kline, alpha indicators, TW futures, TW stock minute lines) use **monthly parquet files**:

- **Past months** (`YYYY-MM < current month`): fetched once, stored immutably. Never re-fetched — except a month whose file was last written before that month ended (cached mid-month) gets one completing re-fetch, merged with what is cached.
- **Current month**: delta-updated on each call — loads cached file, fetches only new bars since the last cached bar, merges and saves.

### Why monthly

| Old approach | Monthly approach |
|---|---|
| One file per `(prefix, params, start)` | One file per `(prefix, params, month)` |
| `start=2022-01` and `start=2023-01` → two separate files with overlapping data | Any `start` reads the same monthly files — no duplication |
| Changing `start` triggers full re-download | Changing `start` just reads different months, all already cached |
| Growing file re-saved entirely on every delta fetch | Only current month file is re-saved |

## Single file per id — daily 台股 / 台市 datasets

Daily-frequency datasets (`twstock_price`, `twstock_price_nonadj`, `twstock_inst`, `twstock_shareholding`, `twstock_per`, `twstock_foreign_sh`, `twmarket_index`, `twmarket_turnover`, `twmarket_institutional`, `twmarket_margin` — the set is `_SINGLE_FILE_PREFIXES` in `lib/data.py`) keep **one parquet per (prefix, id)** whose parquet footer metadata (`blave_cache_meta`: `from` / `to` month covered, `tail_fetched_at`) carries the coverage — frame and meta are always written together in one atomic replace. Same contract as monthly (past data immutable, current month delta-updated, a month fetched before it ended is completed once), just stored in one file: a daily series is ~20 rows a month, so a 300-stock universe under the monthly layout was 41,400 tiny files — measured on a customer box, cold write ≈ 13 min and warm read 32 s vs 2.3 s / 1.1 s for 300 single files. An old monthly directory for the same id is consolidated into the single file on first touch and removed — existing machines migrate by themselves.

## Cache helpers (internal)

```python
_monthly_cache_dir(prefix, params)   # → cache/{prefix}_{params}/
_extend_cache_monthly(prefix, params, fetch_raw_fn, start, end)  # load+update monthly files
_save_monthly(prefix, params, df)    # split df by month, save each month (used by batch fetchers)
_extend_cache_single / _save_single  # one-file-per-id twins, auto-selected for _SINGLE_FILE_PREFIXES
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
