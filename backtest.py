"""
BACKTEST: replays the exact strategy rules (strategy.py) over past market data
to see how it would have done, before any money is risked.

Run it from GitHub: Actions -> "Backtest" -> Run workflow. Takes ~20-40 minutes.
Results appear on the run's Summary page, plus a trades CSV to download.

Settings (environment variables):
  BT_MONTHS      how far back to test (default 12)
  START_EQUITY   starting account size for "your" versions (default 640)
  SLIPPAGE       exit cost as a fraction (default 0.0008 = 0.08%)
  ENTRY_SLIPPAGE breakout entry cost (default 0.0015 = 0.15%; breakouts fill worse)

Honest limits of any backtest, including this one:
  - Uses today's list of stocks, so companies that were delisted are missing
    (this usually makes results look a bit better than reality).
  - Real fills can be worse than modeled, especially at the open.
  - The weekly loss limit and 30% drawdown stop are not modeled (daily limit is).
  - Past results don't guarantee future ones.
"""

import csv
import os
from datetime import datetime, timedelta, time as dtime

import common as c
import strategy as s
from common import ET, log

MONTHS = int(os.environ.get("BT_MONTHS", "12"))
START_EQUITY = float(os.environ.get("START_EQUITY", "640"))
SLIPPAGE = float(os.environ.get("SLIPPAGE", "0.0008"))              # exits
ENTRY_SLIPPAGE = float(os.environ.get("ENTRY_SLIPPAGE", "0.0015"))  # breakout entries cost more
WARMUP_DAYS = 16

V = lambda **kw: {**s.PARAMS, **kw}
VARIANTS = [
    ("YOUR SETUP (auto: 1 at a time under $2k, grows with the account)", START_EQUITY, V()),
    ("Watch all 20 stocks in play, not just the top 6", START_EQUITY, V(watch_top=20)),
    ("Move stop to break-even once up 1x risk", START_EQUITY, V(breakeven_at_r=1.0)),
    ("2 trades at a time (half the cash each)", START_EQUITY, V(max_positions=2)),
    ("15-minute opening range", START_EQUITY, V(or_minutes=15)),
    ("Wider stop (25% of ATR)", START_EQUITY, V(stop_atr=0.25)),
    ("With market filter", START_EQUITY, V(market_filter=True)),
    ("Same rules starting at $2,000 (adds shorts + 2x)", 2000.0, V()),
    ("Your setup with LOW trading costs (0.05% each way)", START_EQUITY, V(entry_slip=0.0005, exit_slip=0.0005)),
    ("CHECK: research setup ($25k, 20 at once, 4x leverage, long+short)", 25000.0,
     V(watch_top=20, max_positions=20, max_leverage=4.0, allow_short=True, risk_pct=0.01,
       max_trades_per_day=40, daily_loss_limit=1.0, min_fill=0.0)),
    ("CHECK with low costs (closest to the research's assumptions)", 25000.0,
     V(watch_top=20, max_positions=20, max_leverage=4.0, allow_short=True, risk_pct=0.01,
       max_trades_per_day=40, daily_loss_limit=1.0, min_fill=0.0, entry_slip=0.0005, exit_slip=0.0005)),
]


def main():
    if not c.KEY or not c.SECRET:
        raise SystemExit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")
    end_day = datetime.now(ET).date() - timedelta(days=1)
    start_day = end_day - timedelta(days=int(MONTHS * 30.4))
    log(f"Backtesting {start_day} to {end_day}.")

    stocks = c.tradable_stocks()
    syms = sorted(set(stocks) | {"SPY"})
    log(f"Loading daily bars for {len(syms)} stocks...")
    d_start = datetime.combine(start_day - timedelta(days=WARMUP_DAYS * 2 + 40), dtime(0), ET)
    d_end = datetime.combine(end_day + timedelta(days=1), dtime(0), ET)
    daily = c.bars(syms, "1Day", d_start, d_end, feed="sip")
    spy_days = [c.bar_time_et(b).date() for b in daily.get("SPY", [])]
    all_days = [d for d in spy_days if d <= end_day]
    test_days = [d for d in all_days if d >= start_day]
    warm_days = [d for d in all_days if d < start_day][-WARMUP_DAYS:]
    day_index = {sym: {c.bar_time_et(b).date(): i for i, b in enumerate(bs)} for sym, bs in daily.items()}

    hist = {5: {}, 15: {}}      # or_minutes -> {sym: [volumes by day]}
    results = {name: {"equity": eq, "start": eq, "curve": [], "trades": []} for name, eq, _ in VARIANTS}
    or_choices = sorted({p["or_minutes"] for _, _, p in VARIANTS})

    for n, day in enumerate(warm_days + test_days):
        # Stats known BEFORE this day's open
        stats = {}
        for sym, bs in daily.items():
            i = day_index[sym].get(day)
            if i is None:
                continue  # didn't trade that day
            st = s.daily_stats(bs[max(0, i - 15):i])
            if s.is_liquid(st):
                stats[sym] = st
        liquid = sorted(stats)
        o = datetime.combine(day, dtime(9, 30), ET)
        first = c.bars(liquid + ["SPY"], "5Min", o, o + timedelta(minutes=max(or_choices)), feed="sip")
        today_or = {m: {} for m in or_choices}
        for sym in liquid + ["SPY"]:
            for m in or_choices:
                rng = s.opening_range(c.opening_bars(first.get(sym, []), day, m), m)
                if rng:
                    today_or[m][sym] = rng

        if day in test_days:
            # Rank Stocks in Play per opening-range length, then fetch their full day
            ranked = {m: s.rank_in_play({k: v for k, v in today_or[m].items() if k != "SPY"},
                                        hist[m], stats, s.PARAMS) for m in or_choices}
            need = sorted({x["symbol"] for m in or_choices for x in ranked[m]})
            # 1-minute bars so we can tell whether the entry or the stop happened first
            full = c.bars(need, "1Min", o, o + timedelta(hours=6, minutes=30), feed="sip") if need else {}
            spy_or = {m: today_or[m].get("SPY") for m in or_choices}

            for name, _, p0 in VARIANTS:
                r = results[name]
                eq = r["equity"]
                p = s.resolve(p0, eq)
                m = p["or_minutes"]
                cands = s.rank_in_play({k: v for k, v in today_or[m].items() if k != "SPY"}, hist[m], stats, p)
                if p["market_filter"] and spy_or[m] and spy_or[m]["c"] < spy_or[m]["o"]:
                    cands = [x for x in cands if x["direction"] == "short"]
                plans = s.plan_trades(cands, eq, eq * p["max_leverage"], p)
                sims = []
                for plan in plans:
                    day_bars = c.session_bars(full.get(plan["symbol"], []), day)
                    after = [b for b in day_bars if c.bar_time_et(b) >= o + timedelta(minutes=m)]
                    if not after:
                        continue
                    mins = [(c.bar_time_et(b) - o).total_seconds() / 60 for b in after]
                    entry_ok = [i for i, x in enumerate(mins) if x < p["last_entry_minute"]]
                    exit_ok = [i for i, x in enumerate(mins) if x < 375]   # last bar before 3:45 PM
                    if not entry_ok or not exit_ok:
                        continue
                    res = s.simulate_trade(plan, after, p.get("exit_slip", SLIPPAGE), p.get("entry_slip", ENTRY_SLIPPAGE),
                                           entry_ok[-1], exit_ok[-1], p["breakeven_at_r"])
                    if res:
                        # compare trades across stocks by clock time, not bar number
                        res["entry_index"], res["exit_index"] = mins[res["entry_index"]], mins[res["exit_index"]]
                        res.update(symbol=plan["symbol"], side=plan["side"], qty=plan["qty"], day=day)
                        sims.append(res)
                taken = s.take_trades(sims, p, eq)
                r["equity"] = max(eq + sum(t["pnl"] for t in taken), 0)
                r["curve"].append((day, r["equity"]))
                r["trades"].extend(taken)

        # Roll the opening-volume history forward
        for m in or_choices:
            for sym, rng in today_or[m].items():
                h = hist[m].setdefault(sym, [])
                h.append(rng["v"])
                if len(h) > 14:
                    h.pop(0)
        if n % 20 == 0:
            log(f"  {day}: {len(liquid)} liquid stocks. Your setup: ${results[VARIANTS[0][0]]['equity']:,.2f}")

    report(results, daily, test_days)


def max_drawdown(curve, start):
    peak, worst = start, 0.0
    for _, e in curve:
        peak = max(peak, e)
        worst = min(worst, e / peak - 1 if peak else 0)
    return worst


def report(results, daily, test_days):
    spy = {c.bar_time_et(b).date(): b["c"] for b in daily["SPY"]}
    spy_start = spy[[d for d in spy if d < test_days[0]][-1]]
    spy_ret = spy[test_days[-1]] / spy_start - 1
    lines = [f"# Backtest: {test_days[0]} to {test_days[-1]} ({len(test_days)} trading days)",
             f"Just holding SPY: **{spy_ret*100:+.1f}%**  (costs assumed: {ENTRY_SLIPPAGE*100:.2f}% per entry, {SLIPPAGE*100:.2f}% per exit)", "",
             "| Version | Start | End | Return | Trades | Win rate | Avg win | Avg loss | Worst drawdown |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, r in results.items():
        t = r["trades"]
        wins = [x["pnl"] for x in t if x["pnl"] > 0]
        losses = [x["pnl"] for x in t if x["pnl"] <= 0]
        lines.append(f"| {name} | ${r['start']:,.0f} | ${r['equity']:,.0f} | {(r['equity']/r['start']-1)*100:+.1f}% | "
                     f"{len(t)} | {len(wins)/len(t)*100 if t else 0:.0f}% | ${sum(wins)/len(wins) if wins else 0:,.2f} | "
                     f"${sum(losses)/len(losses) if losses else 0:,.2f} | {max_drawdown(r['curve'], r['start'])*100:.1f}% |")
    lines += ["", "Read it like this: a strategy is only worth running with real money if YOUR SETUP beats just "
              "holding SPY, with a drawdown you could live through. The CHECK row should look strong if the "
              "backtest matches the published research; if it doesn't, the edge may have faded."]
    text = "\n".join(lines)
    print("\n" + text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")
    with open("backtest_trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["version", "day", "symbol", "side", "qty", "entry", "exit", "pnl", "reason"])
        for name, r in results.items():
            for x in r["trades"]:
                w.writerow([name, x["day"], x["symbol"], x["side"], x["qty"], f"{x['entry']:.2f}",
                            f"{x['exit']:.2f}", f"{x['pnl']:.2f}", x["reason"]])
    log("Wrote backtest_trades.csv")


if __name__ == "__main__":
    main()
