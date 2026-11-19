"""
LIVE swing trader. Runs once each weekday right after the 9:30 AM open:
  1. Loads the last year of daily prices (up to yesterday's close).
  2. Asks the chosen strategy what to hold today.
  3. Sells what shouldn't be held, then buys what should (fractional shares,
     so the whole account is used).
  4. Writes a plain-English report on the GitHub run page.

Choose the strategy with the repo variable STRATEGY: momentum, pullback or trend.
Until STRATEGY is set, it does nothing.

Safety: if the account falls 40% below its peak (or below LOSS_FLOOR), it sells
everything and stops (the GitHub run fails, which emails you).
"""

import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, time as dtime

import common as c
import swing_strategy as ss
from common import ET, log
from swing_data import load_daily

STRATEGY = os.environ.get("STRATEGY", "").strip().lower()
TOP_K = os.environ.get("TOP_K", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "no").lower() == "yes"
LOSS_FLOOR = float(os.environ.get("LOSS_FLOOR", "0") or 0)
PEAK_DRAWDOWN_STOP = float(os.environ.get("PEAK_DRAWDOWN_STOP", "0.40") or 0.40)
MIN_TRADE = 0.02
REPORT = []


def say(msg):
    log(msg)
    REPORT.append(msg)


def trade(method, path, body=None):
    if DRY_RUN and method != "GET":
        say(f"[dry run] would {method} {path} {body or ''}")
        return {}
    return c.trade_api(method, path, body)


def write_report():
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("## Swing trader\n\n" + "\n".join(f"- {m}" for m in REPORT) + "\n")


def main():
    log(f"Mode: {'LIVE (real money)' if c.LIVE else 'PAPER (fake money)'}{' / DRY RUN' if DRY_RUN else ''}")
    if STRATEGY not in ("momentum", "pullback", "trend"):
        say("No strategy chosen yet. Set the repo variable STRATEGY to momentum, pullback or trend.")
        return write_report()
    if not c.KEY or not c.SECRET:
        sys.exit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")

    # Wait for the open (runs are scheduled a few minutes before 9:30 AM ET)
    clock = c.trade_api("GET", "/clock")
    if not clock["is_open"]:
        wait = (c.parse_ts(clock["next_open"]) - datetime.now(ET)).total_seconds()
        if wait > 40 * 60:
            log("Market isn't opening soon. Nothing to do.")
            return
        log(f"Waiting {wait/60:.0f} min for the open...")
        time.sleep(max(wait, 0) + 60)
    today = datetime.now(ET).date()

    # Only trade once per day
    after = datetime.combine(today, dtime(0), ET).astimezone(c.UTC).isoformat()
    todays = c.trade_api("GET", f"/orders?status=all&limit=500&after={urllib.parse.quote(after)}") or []
    if any((o.get("client_order_id") or "").startswith("sw-") for o in todays):
        log("Already traded today.")
        return

    account = c.trade_api("GET", "/account")
    equity = float(account["equity"])
    hist = c.trade_api("GET", "/account/portfolio/history?period=1A&timeframe=1D") or {}
    peak = max([e for e in (hist.get("equity") or []) if e] + [equity])
    if (LOSS_FLOOR and equity < LOSS_FLOOR) or equity < peak * (1 - PEAK_DRAWDOWN_STOP):
        say(f"SAFETY STOP: account ${equity:,.2f} vs peak ${peak:,.2f} (floor {LOSS_FLOOR or 'off'}). "
            "Selling everything; trading stopped. Review, then re-enable the workflow.")
        trade("DELETE", "/orders")
        trade("DELETE", "/positions")
        write_report()
        sys.exit(1)

    # Prices up to yesterday's close
    if STRATEGY == "trend":
        syms = ["SPY", "QQQ", "TQQQ"]
    else:
        syms = ["SPY", "QQQ", "TQQQ"] + sorted(c.tradable_stocks())
    start = datetime.combine(today - timedelta(days=430), dtime(0), ET)
    dates, col, O, C, V = load_daily(syms, start, datetime.combine(today, dtime(0), ET))
    if dates[-1] >= today:
        dates, O, C, V = dates[:-1], O[:-1], C[:-1], V[:-1]
    ind = ss.Indicators(C, V)
    t = len(dates) - 1
    name = {v: k for k, v in col.items()}

    positions = c.trade_api("GET", "/positions") or []
    pos = {p["symbol"]: p for p in positions}
    held_cols = [col[s] for s in pos if s in col]

    if STRATEGY == "momentum":
        p = dict(ss.PARAMS["momentum"])
        if TOP_K:
            p["top_k"] = int(TOP_K)
        rebalance = today.isocalendar()[:2] != dates[t].isocalendar()[:2]
        targets, why = ss.momentum_targets(t, ind, col, p, rebalance, current=held_cols)
    elif STRATEGY == "pullback":
        held = {}
        old = c.trade_api("GET", "/orders?status=closed&limit=500&direction=desc&after="
                          + urllib.parse.quote((datetime.now(ET) - timedelta(days=40)).astimezone(c.UTC).isoformat())) or []
        for s in pos:
            if s not in col:
                continue
            buys = [o for o in old if o["symbol"] == s and o["side"] == "buy" and o.get("filled_at")]
            entry_day = c.parse_ts(buys[0]["filled_at"]).astimezone(ET).date() if buys else dates[t]
            held[col[s]] = max(i for i, d in enumerate(dates) if d <= entry_day) if entry_day >= dates[0] else 0
        targets, why = ss.pullback_targets(t, ind, col, held)
    else:
        targets, why = ss.trend_targets(t, ind, col)

    want = {name[j]: w for j, w in targets.items()}
    say(f"Strategy: **{STRATEGY}**. {why}".strip())
    say("Target: " + (", ".join(f"{s} {'keep' if w is None else f'{w*100:.0f}%'}" for s, w in want.items()) or "all cash"))

    # Sells first
    sold = False
    for s, p_ in pos.items():
        if s not in want:
            say(f"SELL all {s} (${float(p_['market_value']):,.2f}).")
            trade("DELETE", f"/positions/{s}")
            sold = True
    for s, w in want.items():
        if w is None or s not in pos:
            continue
        diff = w * equity - float(pos[s]["market_value"])
        if diff < -MIN_TRADE * equity:
            say(f"TRIM {s} by ${-diff:,.2f}.")
            try:
                trade("POST", "/orders", {"symbol": s, "notional": f"{-diff:.2f}", "side": "sell", "type": "market",
                                          "time_in_force": "day", "client_order_id": f"sw-{today:%Y%m%d}-{s}-trim"})
                sold = True
            except RuntimeError as e:
                say(f"{s}: trim rejected ({e}).")
    if sold and not DRY_RUN:
        time.sleep(15)

    # Then buys
    account = c.trade_api("GET", "/account")
    cash = float(account["buying_power"]) if not DRY_RUN else equity
    cash = min(cash, max(float(account["cash"]), 0) or cash)
    for s, w in want.items():
        if w is None:
            continue
        have = float(pos[s]["market_value"]) if s in pos else 0.0
        diff = min(w * equity - have, cash - 1)
        if diff < max(MIN_TRADE * equity, 1.0):
            continue
        say(f"BUY ${diff:,.2f} of {s}.")
        try:
            trade("POST", "/orders", {"symbol": s, "notional": f"{diff:.2f}", "side": "buy", "type": "market",
                                      "time_in_force": "day", "client_order_id": f"sw-{today:%Y%m%d}-{s}"})
            cash -= diff
        except RuntimeError as e:
            say(f"{s}: order rejected ({e}).")
    if not any(m.startswith(("BUY", "SELL", "TRIM")) for m in REPORT):
        say("No changes needed today.")
    # Mark that we ran today even when nothing traded
    say(f"Account: ${equity:,.2f}.")
    write_report()


if __name__ == "__main__":
    main()
