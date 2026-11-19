"""
STRATEGY LAB: tests the swing strategies on ~10 years of daily data (2016 to now)
and compares them with simply holding SPY or QQQ.

Run from GitHub: Actions -> "Strategy lab" -> Run workflow (about 20-40 minutes).

How trades are modeled: decisions use each day's closing prices; trades happen
at the NEXT day's opening price, minus a cost of COST (default 0.10%) on every
dollar traded. Fractional shares, like the live trader.

Honest limits:
  - The stock strategies use today's list of stocks. Companies that went bankrupt
    or were delisted are missing, which flatters momentum and pullback results.
    The leveraged trend strategy uses ETFs only, so it doesn't have this problem.
  - Ten years includes the 2020 crash and 2022 bear market, but the future can differ.
"""

import csv
import os
from datetime import datetime, timedelta, time as dtime

import numpy as np

import common as c
import swing_strategy as ss
from common import ET, log
from swing_data import load_daily

COST = float(os.environ.get("COST", "0.001"))
START = os.environ.get("START_DATE", "2016-01-01")
EQUITY0 = float(os.environ.get("START_EQUITY", "640"))
MIN_TRADE = 0.02   # skip rebalancing trades smaller than 2% of the account


def new_week(d0, d1):
    return d0.isocalendar()[:2] != d1.isocalendar()[:2]


def simulate(kind, dates, col, O, C, ind, start_t, p=None):
    T = len(dates)
    Cf = C.copy()                                 # last known close, for valuing
    for t in range(1, T):
        m = np.isnan(Cf[t])
        Cf[t, m] = Cf[t - 1, m]
    shares, held, cash = {}, {}, EQUITY0
    curve, trades = [], 0
    for t in range(start_t, T - 1):
        d = t + 1
        if kind == "momentum":
            targets, _ = ss.momentum_targets(t, ind, col, p or ss.PARAMS["momentum"],
                                             new_week(dates[t], dates[d]), current=list(shares))
        elif kind == "pullback":
            targets, _ = ss.pullback_targets(t, ind, col, held, p or ss.PARAMS["pullback"])
        else:
            targets, _ = ss.trend_targets(t, ind, col, p or ss.PARAMS["trend"])

        px = lambda j: O[d, j] if not np.isnan(O[d, j]) else Cf[t, j]
        equity = cash + sum(q * px(j) for j, q in shares.items())
        # Sells first
        for j in list(shares):
            if j not in targets and not np.isnan(O[d, j]):
                cash += shares.pop(j) * O[d, j] * (1 - COST)
                held.pop(j, None)
                trades += 1
        # Then buys / rebalances
        for j, w in targets.items():
            if w is None or np.isnan(O[d, j]):
                continue
            want = w * equity
            have = shares.get(j, 0.0) * O[d, j]
            diff = want - have
            if abs(diff) < MIN_TRADE * equity:
                continue
            if diff > 0:
                diff = min(diff, cash / (1 + COST))
                if diff <= 0:
                    continue
                cash -= diff * (1 + COST)
            else:
                cash += -diff * (1 - COST)
            shares[j] = shares.get(j, 0.0) + diff / O[d, j]
            if j not in held:
                held[j] = d
            trades += 1
        curve.append((dates[d], cash + sum(q * Cf[d, j] for j, q in shares.items()),
                      len(shares) > 0))
    return curve, trades


def buy_hold(sym, dates, col, O, Cf_col, start_t):
    j = col[sym]
    q = EQUITY0 * (1 - COST) / O[start_t + 1, j]
    return [(dates[d], q * Cf_col[d]) for d in range(start_t + 1, len(dates))]


def stats(curve):
    vals = np.array([v for _, v, *_ in curve])
    years = (curve[-1][0] - curve[0][0]).days / 365.25
    peak = np.maximum.accumulate(vals)
    year_end = {}
    for d, v, *_ in curve:
        year_end[d.year] = v          # last value seen in each year
    yr, last = {}, EQUITY0
    for y in sorted(year_end):
        yr[y] = year_end[y] / last - 1
        last = year_end[y]
    half = len(curve) // 2
    first = vals[half - 1] / EQUITY0 - 1
    second = vals[-1] / vals[half - 1] - 1
    invested = np.mean([x[2] for x in curve]) if len(curve[0]) > 2 else 1.0
    return {"end": vals[-1], "total": vals[-1] / EQUITY0 - 1, "cagr": (vals[-1] / EQUITY0) ** (1 / years) - 1,
            "maxdd": float(np.min(vals / peak - 1)), "years": yr, "first": first, "second": second,
            "invested": invested}


def main():
    if not c.KEY or not c.SECRET:
        raise SystemExit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")
    start = datetime.combine(datetime.fromisoformat(START).date(), dtime(0), ET)
    end = datetime.combine(datetime.now(ET).date(), dtime(0), ET)
    stocks = c.tradable_stocks()
    syms = ["SPY", "QQQ", "TQQQ"] + sorted(stocks)
    log(f"Loading ~10 years of daily prices for {len(syms)} symbols (takes a while)...")
    dates, col, O, C, V = load_daily(syms, start, end)
    log(f"Loaded {len(dates)} trading days. Computing indicators...")
    ind = ss.Indicators(C, V)
    start_t = ss.NEEDED_HISTORY
    Cf = C.copy()
    for t in range(1, len(dates)):
        m = np.isnan(Cf[t])
        Cf[t, m] = Cf[t - 1, m]

    runs = {}
    mom = ss.PARAMS["momentum"]
    for label, kind, p in [
        ("MOMENTUM: top 3 stocks", "momentum", mom),
        ("MOMENTUM: top 1 stock (max risk)", "momentum", {**mom, "top_k": 1}),
        ("MOMENTUM: top 5 stocks", "momentum", {**mom, "top_k": 5}),
        ("PULLBACK: buy the dip, up to 3", "pullback", None),
        ("LEVERAGED TREND: TQQQ when QQQ > 200-day, else cash", "trend", None),
    ]:
        log(f"Testing {label}...")
        curve, n = simulate(kind, dates, col, O, C, ind, start_t, p)
        runs[label] = (stats(curve), n, curve)
    for sym in ("SPY", "QQQ"):
        curve = buy_hold(sym, dates, col, O, Cf[:, col[sym]], start_t)
        runs[f"BENCHMARK: just hold {sym}"] = (stats([(d, v, True) for d, v in curve]), 1, curve)
    report(runs, dates[start_t + 1], dates[-1])


def report(runs, d0, d1):
    years = sorted({y for s, _, _ in runs.values() for y in s["years"]})
    lines = [f"# Strategy lab: {d0} to {d1}",
             f"Starting with ${EQUITY0:,.0f}. Trades at next day's open, {COST*100:.2f}% cost per trade.", "",
             "| Strategy | End value | Per year | Worst drop | 1st half | 2nd half | Trades | Invested |",
             "|---|---|---|---|---|---|---|---|"]
    for name, (s, n, _) in runs.items():
        lines.append(f"| {name} | ${s['end']:,.0f} | {s['cagr']*100:+.1f}% | {s['maxdd']*100:.0f}% | "
                     f"{s['first']*100:+.0f}% | {s['second']*100:+.0f}% | {n} | {s['invested']*100:.0f}% |")
    lines += ["", "### Year by year", "",
              "| Strategy | " + " | ".join(str(y) for y in years) + " |",
              "|---|" + "---|" * len(years)]
    for name, (s, _, _) in runs.items():
        lines.append(f"| {name} | " + " | ".join(f"{s['years'].get(y, 0)*100:+.0f}%" for y in years) + " |")
    lines += ["", "**How to read this:** a strategy is worth paper trading only if it beats just holding "
              "SPY over the whole period, does so in BOTH halves (not just one lucky stretch), and has a "
              "worst drop you could sit through without panicking. Stock strategies (momentum, pullback) "
              "look somewhat better here than reality because delisted companies are missing."]
    text = "\n".join(lines)
    print("\n" + text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")
    with open("strategy_lab_equity.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "date", "value"])
        for name, (_, _, curve) in runs.items():
            for row in curve:
                w.writerow([name, row[0], f"{row[1]:.2f}"])


if __name__ == "__main__":
    main()
