"""
The trading rules, in one place. Used by BOTH the live trader and the backtester,
so what gets tested is exactly what trades.

STRATEGY: Opening Range Breakout on "Stocks in Play"
(based on Zarattini, Barbon & Aziz, "A Profitable Day Trading Strategy for the
U.S. Equity Market", 2024)

1. Before the open, list the liquid stocks: price >= $5, average daily volume
   >= 1M shares, average daily range (ATR) >= $0.50.
2. After the first 5 minutes, rank them by "relative volume": how much more they
   traded than usual in those 5 minutes. The top ones are today's Stocks in Play.
3. Watch the strongest few. Green first candle -> buy when price breaks above its
   high. Red first candle -> short below its low (accounts of $2,000+ only).
4. Tight stop-loss: 10% of the stock's normal daily range from the entry.
5. Everything is closed before the bell. Nothing is held overnight.

GROWS WITH THE ACCOUNT ("auto" settings)
  under $2,000  : 1 trade at a time using the whole account, long only, no borrowing
  $2,000+       : 2 at a time, shorts on, up to 2x buying power
  $10,000+      : 4 at a time
  Each trade can still lose at most risk_pct of the account.
"""

import math

PARAMS = {
    "or_minutes": 5,            # opening range length
    "top_n": 20,                # stocks in play ranked
    "watch_top": 6,             # of those, how many we actually watch for a breakout
    "min_rvol": 1.0,            # opening volume must be at least normal
    "stop_atr": 0.10,           # stop distance as a fraction of ATR (research: 10%)
    "risk_pct": 0.015,          # max loss per trade as a fraction of the account
    "max_positions": "auto",
    "max_leverage": "auto",
    "allow_short": "auto",
    "max_trades_per_day": 5,
    "daily_loss_limit": 0.05,   # no new trades after losing 5% in a day
    "min_fill": 0.6,            # skip setups where whole shares would use < 60% of the slot
    "breakeven_at_r": 0,        # 0 = off; e.g. 1.0 moves the stop to entry once up 1x the risk
    "min_price": 5.0,
    "min_avg_volume": 1_000_000,
    "min_atr": 0.50,
    "market_filter": False,     # skip longs when SPY's first candle is red
    "last_entry_minute": 330,   # entries until 3:00 PM ET
}


def resolve(p, equity):
    """Turn 'auto' settings into numbers for the current account size."""
    q = dict(p)
    if q["max_positions"] == "auto":
        q["max_positions"] = 1 if equity < 2000 else 2 if equity < 10000 else 4
    if q["max_leverage"] == "auto":
        q["max_leverage"] = 1.0 if equity < 2000 else 2.0
    if q["allow_short"] == "auto":
        q["allow_short"] = equity >= 2000
    q["max_positions"] = int(q["max_positions"])
    q["max_leverage"] = float(q["max_leverage"])
    q["allow_short"] = q["allow_short"] in (True, "yes", "true", "1")
    return q


def daily_stats(bars, lookback=14):
    """bars: daily bars oldest->newest [{o,h,l,c,v}]. Returns stats or None."""
    if len(bars) < lookback + 1:
        return None
    recent = bars[-lookback:]
    trs = [max(b["h"] - b["l"], abs(b["h"] - prev["c"]), abs(b["l"] - prev["c"]))
           for prev, b in zip(bars[-lookback - 1:-1], recent)]
    return {"atr": sum(trs) / lookback,
            "avg_volume": sum(b["v"] for b in recent) / lookback,
            "close": bars[-1]["c"]}


def is_liquid(stats, p=PARAMS):
    return (stats is not None and stats["close"] >= p["min_price"]
            and stats["avg_volume"] >= p["min_avg_volume"] and stats["atr"] >= p["min_atr"])


def opening_range(bars_5m, or_minutes):
    """First or_minutes of 5-minute bars -> {o,h,l,c,v} or None."""
    k = max(1, or_minutes // 5)
    if len(bars_5m) < k:
        return None
    first = bars_5m[:k]
    return {"o": first[0]["o"], "h": max(b["h"] for b in first), "l": min(b["l"] for b in first),
            "c": first[-1]["c"], "v": sum(b["v"] for b in first)}


def rank_in_play(today_or, hist_or_volumes, stats, p=PARAMS, min_or_volume=0):
    """Candidates sorted by relative volume (highest first)."""
    out = []
    for sym, rng in today_or.items():
        hist = [v for v in hist_or_volumes.get(sym, []) if v > 0]
        st = stats.get(sym)
        if len(hist) < 5 or not is_liquid(st, p) or rng["o"] < p["min_price"] or rng["v"] < min_or_volume:
            continue
        rvol = rng["v"] / (sum(hist) / len(hist))
        if rvol < p["min_rvol"] or rng["c"] == rng["o"]:
            continue
        out.append({"symbol": sym, "rvol": rvol, "direction": "long" if rng["c"] > rng["o"] else "short",
                    "or_high": rng["h"], "or_low": rng["l"], "atr": st["atr"]})
    out.sort(key=lambda c: c["rvol"], reverse=True)
    return out[:p["top_n"]]


def plan_trades(candidates, equity, cash, p, open_symbols=()):
    """
    p must be resolved (see resolve). Considers the top `watch_top` candidates that
    are tradable for this account, sizes each one, and returns the plans in rank order.
    Each plan gets 1/max_positions of the buying budget and risks at most risk_pct.
    """
    budget = min(cash, equity * p["max_leverage"])
    slot_cash = budget / max(p["max_positions"], 1)
    plans = []
    for c in candidates:
        if len(plans) >= p["watch_top"]:
            break
        if c["symbol"] in open_symbols or (c["direction"] == "short" and not p["allow_short"]):
            continue
        risk_ps = max(round(p["stop_atr"] * c["atr"], 2), 0.01)
        long = c["direction"] == "long"
        trigger = round(c["or_high"] + 0.01 if long else c["or_low"] - 0.01, 2)
        stop = round(trigger - risk_ps if long else trigger + risk_ps, 2)
        by_risk = math.floor(equity * p["risk_pct"] / risk_ps)
        by_cash = math.floor(slot_cash * 0.98 / trigger)
        qty = min(by_risk, by_cash)
        if qty < 1:
            continue
        if by_cash <= by_risk and qty * trigger < p["min_fill"] * slot_cash:
            continue  # whole shares would leave most of the money idle
        plans.append({"symbol": c["symbol"], "side": "buy" if long else "sell", "trigger": trigger,
                      "stop": stop, "qty": qty, "risk_per_share": risk_ps, "rvol": c["rvol"]})
    return plans


def simulate_trade(plan, bars, slippage=0.0008, entry_slippage=0.0015, last_entry_index=None,
                   exit_index=None, breakeven_at_r=0):
    """
    Backtest one planned trade on the 5-minute bars AFTER the opening range.
    Conservative: a bar that hits both entry and stop counts as a loss; stop fills
    at the worse of the stop price and the bar's open.
    """
    long = plan["side"] == "buy"
    trig, stop, qty, risk = plan["trigger"], plan["stop"], plan["qty"], plan["risk_per_share"]
    last_entry_index = len(bars) - 1 if last_entry_index is None else last_entry_index
    exit_index = len(bars) - 1 if exit_index is None else exit_index
    entry = entry_i = None
    for i, b in enumerate(bars[:exit_index + 1]):
        if entry is None:
            if i > last_entry_index:
                return None
            if not (b["h"] >= trig if long else b["l"] <= trig):
                continue
            fill = max(trig, b["o"]) if long else min(trig, b["o"])
            entry = fill * (1 + entry_slippage) if long else fill * (1 - entry_slippage)
            entry_i = i
            if (b["l"] <= stop) if long else (b["h"] >= stop):
                return _result(entry, _out(stop, long, slippage), qty, long, "stopped (same bar)", i, i)
            continue
        if (b["l"] <= stop) if long else (b["h"] >= stop):
            px = min(stop, b["o"]) if long else max(stop, b["o"])
            why = "stopped" if stop != trig else "stopped at breakeven"
            return _result(entry, _out(px, long, slippage), qty, long, why, entry_i, i)
        if breakeven_at_r and ((b["h"] >= trig + breakeven_at_r * risk) if long
                               else (b["l"] <= trig - breakeven_at_r * risk)):
            stop = max(stop, trig) if long else min(stop, trig)
    if entry is None:
        return None
    return _result(entry, _out(bars[exit_index]["c"], long, slippage), qty, long,
                   "closed at end of day", entry_i, exit_index)


def _out(px, long, slippage):
    return px * (1 - slippage) if long else px * (1 + slippage)


def _result(entry, out, qty, long, reason, entry_i, exit_i):
    pnl = (out - entry) * qty if long else (entry - out) * qty
    return {"entry": entry, "exit": out, "pnl": pnl, "reason": reason,
            "entry_index": entry_i, "exit_index": exit_i}


def take_trades(results, p, equity):
    """
    Same rules as live: breakouts are taken in the order they happen, up to
    max_positions at once and max_trades_per_day, and no new trades once the
    day's closed losses reach daily_loss_limit.
    """
    taken = []
    for r in sorted(results, key=lambda r: r["entry_index"]):
        t = r["entry_index"]
        active = [x for x in taken if x["entry_index"] <= t < x["exit_index"] or x["entry_index"] == t]
        realized = sum(x["pnl"] for x in taken if x["exit_index"] < t)
        if (len(active) < p["max_positions"] and len(taken) < p["max_trades_per_day"]
                and realized > -p["daily_loss_limit"] * equity):
            taken.append(r)
    return taken
