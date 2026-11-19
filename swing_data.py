"""Loads daily prices for many stocks into aligned tables (one row per trading day)."""

import urllib.parse
from datetime import datetime, timedelta

import numpy as np

import common as c
from common import ET, UTC, log


def _pages(symbols, start, end, feed="sip"):
    token = None
    while True:
        q = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start.astimezone(UTC).isoformat(),
             "end": end.astimezone(UTC).isoformat(), "limit": 10000, "feed": feed, "adjustment": "all"}
        if token:
            q["page_token"] = token
        res = c.api("GET", f"{c.DATA_URL}/stocks/bars?{urllib.parse.urlencode(q)}") or {}
        yield res.get("bars") or {}
        token = res.get("next_page_token")
        if not token:
            return


def load_daily(symbols, start, end, batch=200):
    """
    Returns (dates, col, O, C, V): dates = list of trading dates (from SPY),
    col = {symbol: column}, and T x N float arrays (NaN where a stock has no bar).
    """
    end = min(end, datetime.now(ET) - timedelta(minutes=16))
    symbols = list(dict.fromkeys(["SPY"] + [s for s in symbols if s != "SPY"]))
    spy = []
    for page in _pages(["SPY"], start, end):
        spy.extend(page.get("SPY", []))
    dates = [c.bar_time_et(b).date() for b in spy]
    row = {d: i for i, d in enumerate(dates)}
    col = {s: j for j, s in enumerate(symbols)}
    T, N = len(dates), len(symbols)
    O = np.full((T, N), np.nan, dtype=np.float32)
    C = np.full((T, N), np.nan, dtype=np.float32)
    V = np.full((T, N), np.nan, dtype=np.float32)
    for k in range(0, N, batch):
        chunk = symbols[k:k + batch]
        for page in _pages(chunk, start, end):
            for s, bars in page.items():
                j = col[s]
                for b in bars:
                    i = row.get(c.bar_time_et(b).date())
                    if i is not None:
                        O[i, j], C[i, j], V[i, j] = b["o"], b["c"], b["v"]
        if (k // batch) % 5 == 0:
            log(f"  loaded {min(k + batch, N)}/{N} symbols")
    return dates, col, O.astype(float), C.astype(float), V.astype(float)
