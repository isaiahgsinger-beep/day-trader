"""Alpaca connection and market-data helpers shared by the live trader and the backtester."""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

LIVE = os.environ.get("ALPACA_LIVE", "no").lower() == "yes"
KEY = os.environ.get("ALPACA_API_KEY", "")
SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
TRADE_URL = "https://api.alpaca.markets/v2" if LIVE else "https://paper-api.alpaca.markets/v2"
DATA_URL = "https://data.alpaca.markets/v2"

# Leveraged / inverse / volatility products and other funds are never traded.
FUND_WORDS = re.compile(r"\b(ETF|ETN|FUND|TRUST|PROSHARES|DIREXION|ISHARES|SPDR|INVESCO|VANECK|"
                        r"2X|3X|-1X|-2X|-3X|BULL|BEAR|LEVERAGED|INVERSE|ULTRA|DAILY|VOLATILITY|"
                        r"WARRANT|RIGHTS|UNITS|NOTES|DEPOSITARY SHARES REPRESENTING)\b", re.I)


def log(msg):
    print(f"[{datetime.now(ET):%Y-%m-%d %H:%M:%S} ET] {msg}", flush=True)


def parse_ts(s):
    s = s.replace("Z", "+00:00")
    s = re.sub(r"\.(\d{6})\d+", r".\1", s)
    return datetime.fromisoformat(s)


def api(method, url, body=None, tries=5):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 ** attempt + 1)
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} -> {e.code}: {msg}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries - 1:
                time.sleep(2 ** attempt + 1)
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} -> network error: {e}")


def trade_api(method, path, body=None):
    return api(method, TRADE_URL + path, body)


def tradable_stocks():
    """Common stocks on major US exchanges (no funds, warrants, leveraged products)."""
    assets = trade_api("GET", "/assets?status=active&asset_class=us_equity")
    out = {}
    for a in assets:
        sym, name = a["symbol"], a.get("name") or ""
        if not a.get("tradable") or a.get("exchange") not in ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"):
            continue
        if not sym.isalpha() or len(sym) > 5 or FUND_WORDS.search(name):
            continue
        out[sym] = {"shortable": a.get("shortable") and a.get("easy_to_borrow")}
    return out


def bars(symbols, timeframe, start, end, feed="sip", batch=200):
    """Multi-symbol bars with paging. Returns {sym: [bar, ...]} oldest->newest."""
    out = {}
    symbols = list(symbols)
    if feed == "sip":  # the free data plan can't see the most recent 15 minutes of SIP data
        end = min(end, datetime.now(ET) - timedelta(minutes=16))
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        token = None
        while True:
            q = {"symbols": ",".join(chunk), "timeframe": timeframe, "start": start.astimezone(UTC).isoformat(),
                 "end": end.astimezone(UTC).isoformat(), "limit": 10000, "feed": feed, "adjustment": "split"}
            if token:
                q["page_token"] = token
            res = api("GET", f"{DATA_URL}/stocks/bars?{urllib.parse.urlencode(q)}") or {}
            for s, bs in (res.get("bars") or {}).items():
                out.setdefault(s, []).extend(bs)
            token = res.get("next_page_token")
            if not token:
                break
    return out


def bar_time_et(bar):
    return parse_ts(bar["t"]).astimezone(ET)


def session_bars(bars_list, day, start=dtime(9, 30), end=dtime(16, 0)):
    """Keep only regular-session bars for a given date (by timestamp, not by count)."""
    return [b for b in bars_list
            if bar_time_et(b).date() == day and start <= bar_time_et(b).time() < end]


def opening_bars(bars_list, day, or_minutes):
    """The bars making up the opening range, checked by timestamp."""
    end = (datetime.combine(day, dtime(9, 30)) + timedelta(minutes=or_minutes)).time()
    return session_bars(bars_list, day, dtime(9, 30), end)
