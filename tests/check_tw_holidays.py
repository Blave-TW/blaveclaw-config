"""Minimal check for the Taiwan market calendar and industry lookup in lib/data.py — no network.
fetch_twstock_holidays degrades to None (unreachable / not published) and carries the
attribution through; is_tw_trading_day is True / False / None; twstock_industry_name never
guesses an unknown code.
Run: cd blaveclaw-config && .venv/bin/python tests/check_tw_holidays.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
import pandas as pd
import requests
from lib import data as d

fails = 0
def check(cond, msg):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + msg); fails += (not cond)

class Resp:
    def __init__(self, payload): self.payload = payload
    def json(self): return self.payload

calls = []
def serve(payload):
    def fake(url, **kw):
        calls.append(kw)
        if isinstance(payload, Exception):
            raise payload
        return Resp(payload)
    d._retry_get = fake
    d._HOLIDAY_MEMO.clear()

H = {"api-key": "x", "secret-key": "y"}
SRC = "Taiwan Stock Exchange, 2026 … https://data.gov.tw/license …"
TABLE = {"year": 2026, "published": True, "stale": False, "source": SRC, "source_zh": "臺灣證券交易所 2026 …",
         "note": "Official TWSE annual schedule only…",
         "data": [{"date": "2026-02-12", "name": "市場無交易，僅辦理結算交割作業", "type": "settlement_only"},
                  {"date": "2026-09-25", "name": "中秋節", "type": "holiday", "note": "x"}]}

serve(requests.exceptions.HTTPError("404 Not Found"))
check(d.fetch_twstock_holidays(H, 2026) is None and d.is_tw_trading_day("2026-09-25", H) is None,
      "endpoint not deployed (404): None / unknown, not 'no holidays'")
check(all(kw.get("max_retries", 6) <= 2 for kw in calls), "5xx backoff capped (a template must not hang ~2 min on it)")
serve({"year": 2027, "published": False, "reason": "not_yet_published", "data": [], "stale": False, "source": SRC})
check(d.fetch_twstock_holidays(H, 2027) is None and d.is_tw_trading_day("2027-01-05", H) is None,
      "published:false: None / unknown")

serve(TABLE)
t = d.fetch_twstock_holidays(H, 2026)
check(t is not None and t.attrs.get("source") == SRC and t.attrs.get("source_zh") and list(t.columns) == ["date", "name", "type", "note"],
      "published table: rows + source / source_zh carried in attrs")
n = len(calls); d.fetch_twstock_holidays(H, 2026)
check(len(calls) == n and calls[-1].get("params") == {"year": 2026}, "same year re-read from the in-process memo")
check(d.is_tw_trading_day("2026-09-25", H) is False, "holiday row → False")
check(d.is_tw_trading_day("2026-02-12", H) is False, "settlement_only row → False")
check(d.is_tw_trading_day("2026-09-24", H) is True, "weekday not in table → True")
check(d.is_tw_trading_day(pd.Timestamp("2026-09-24 17:00", tz="UTC"), H) is False
      and d.is_tw_trading_day(datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc), H) is False,
      "tz-aware UTC 09-24 17:00 is Taipei 09-25 (holiday) → False, not the UTC date")
check(d.is_tw_trading_day(pd.Timestamp("2026-09-24 17:00"), H) is True, "naive value is taken as a Taipei date")
check(d.is_tw_trading_day("2026-9-25", H) is False, "'2026-9-25' (no zero padding) is normalised, not skipped")
for bad in ("9/25", "20260925", "2026/09/25"):
    try:
        d.is_tw_trading_day(bad, H); check(False, f"{bad!r} rejected with ValueError")
    except ValueError:
        check(True, f"{bad!r} rejected with ValueError")
serve(AssertionError("weekend must not fetch"))
check(d.is_tw_trading_day("2026-09-26", H) is False and d.is_tw_trading_day("2026-09-27", H) is False,
      "Saturday / Sunday → False without fetching")

# Default year = the Taipei year: a UTC clock at 2026-12-31 17:30 is already 2027 in Taipei.
EDGE = datetime(2026, 12, 31, 17, 30, tzinfo=timezone.utc)
class ClockDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return EDGE.astimezone(tz) if tz else EDGE.replace(tzinfo=None)
    @classmethod
    def utcnow(cls):
        return EDGE.replace(tzinfo=None)
serve(TABLE); d.datetime = ClockDT
try:
    d.fetch_twstock_holidays(H)
finally:
    d.datetime = datetime
check(calls[-1].get("params") == {"year": 2027}, "year=None asks for the Taipei year (2027), not the UTC year (2026)")

check(len(d.TWSE_INDUSTRY_NAMES) == 35 and d.twstock_industry_name("24") == "半導體業" and d.twstock_industry_name(24) == "半導體業",
      "35 codes; '24' and 24 → 半導體業")
check(d.twstock_industry_name("5") == "電機機械" and d.twstock_industry_name("91") == "存託憑證", "single digit zero-padded; 91 = DR")
check(d.twstock_industry_name(None) is None and d.twstock_industry_name(float("nan")) is None, "ETF (None / NaN) → None")
check(d.twstock_industry_name("07") == "07" and d.twstock_industry_name("99") == "99", "unknown code comes back unchanged")

print("all checks passed" if not fails else f"FAILED: {fails}"); sys.exit(1 if fails else 0)
