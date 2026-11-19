"""
LIVE day trader. Runs all session long (a "shift" lasts up to ~5h50m; the next
shift picks up where the last one ended).

Each day:
  9:15   Builds the list of liquid stocks and their normal opening volume.
         Sells anything left over from a previous day (backstop; shouldn't happen).
  9:35   Ranks today's Stocks in Play and picks the strongest few to watch.
  9:35 to 3:00  Watches them (every 4 seconds in the first hour, then every 15).
         The moment one breaks out, it enters with a stop-loss attached (GTC).
  3:45   Sells everything. Writes a plain-English report of the day.

Safety switches:
  - Account down 30% from its peak, or below LOSS_FLOOR: sells everything, stops for good.
  - Down 5% today: no new trades today.  Down 10% this week: none this week.
  - At most 5 trades a day.
  - Fed announcement days: sells everything at 1:50 PM, before the 2:00 PM statement.
"""

import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, time as dtime

import common as c
import strategy as s
from common import ET, log


def env(name, default):
    v = os.environ.get(name, "").strip()
    return v if v else default


P = dict(s.PARAMS)
P["max_positions"] = env("MAX_POSITIONS", "auto")
P["max_leverage"] = env("MAX_LEVERAGE", "auto")
P["allow_short"] = env("ALLOW_SHORT", "auto")
P["risk_pct"] = float(env("RISK_PCT", P["risk_pct"]))
P["watch_top"] = int(env("WATCH_TOP", P["watch_top"]))
P["breakeven_at_r"] = float(env("BREAKEVEN_AT_R", P["breakeven_at_r"]))
P["market_filter"] = env("MARKET_FILTER", "no").lower() == "yes"
DRY_RUN = env("DRY_RUN", "no").lower() == "yes"
LOSS_FLOOR = float(env("LOSS_FLOOR", "0"))

FAST_LOOP, SLOW_LOOP = 4, 15
MAX_RUNTIME = timedelta(hours=5, minutes=50)
FLATTEN_MIN_BEFORE_CLOSE = 15
MAX_CHASE = 0.004            # pay at most 0.4% above the breakout price
ENTRY_TIMEOUT_SEC = 45       # cancel an entry that hasn't filled within 45s
MIN_IEX_OR_VOLUME = 3000     # ignore stocks with too little opening data to judge
WEEKLY_LOSS_LIMIT = 0.10
PEAK_DRAWDOWN_STOP = 0.30
# Fed decision days (2:00 PM ET statement). Check federalreserve.gov each year.
FOMC_DAYS = {"2026-12-09", "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
             "2027-07-28", "2027-09-22", "2027-10-27", "2027-12-08"}


def now_et():
    return datetime.now(ET)


def trade(method, path, body=None):
    if DRY_RUN and method != "GET":
        log(f"[dry run] {method} {path} {body or ''}")
        return {}
    return c.trade_api(method, path, body)


def is_orb(o):
    return (o.get("client_order_id") or "").startswith("orb-")


class Day:
    def __init__(self, today):
        self.today = today
        self.stats, self.hist_or, self.shortable = {}, {}, {}
        self.p = None
        self.candidates = None
        self.prepared = False
        self.cleaned = False
        self.halted_reason = None
        self.breakeven_done = set()
        self.history = (None, 0.0)   # (portfolio history, fetched at)
        self.reported = False


def prepare(day):
    log("Preparing today's stock universe...")
    stocks = c.tradable_stocks()
    day.shortable = {k: v["shortable"] for k, v in stocks.items()}
    start = datetime.combine(day.today - timedelta(days=45), dtime(0), ET)
    end = datetime.combine(day.today, dtime(0), ET)
    daily = c.bars(list(stocks) + ["SPY"], "1Day", start, end, feed="sip")
    day.stats = {sym: s.daily_stats(bs) for sym, bs in daily.items() if sym != "SPY"}
    liquid = [sym for sym, st in day.stats.items() if s.is_liquid(st, P)]
    trading_days = sorted({c.bar_time_et(b).date() for b in daily.get("SPY", [])})[-14:]
    log(f"{len(stocks)} stocks -> {len(liquid)} liquid. Loading normal opening volume ({len(trading_days)} days)...")
    hist = {sym: [] for sym in liquid}
    for d in trading_days:
        o = datetime.combine(d, dtime(9, 30), ET)
        got = c.bars(liquid, "5Min", o, o + timedelta(minutes=P["or_minutes"]), feed="iex")
        for sym in liquid:
            hist[sym].append(sum(b["v"] for b in c.opening_bars(got.get(sym, []), d, P["or_minutes"])))
    day.hist_or = hist
    day.prepared = True
    log("Universe ready.")


def buying_cash(account):
    bp = float(account["buying_power"])
    cash = float(account["cash"])
    return min(bp, cash) if cash > 0 else bp


def select(day, account, positions):
    o = datetime.combine(day.today, dtime(9, 30), ET)
    liquid = list(day.hist_or)
    got = c.bars(liquid + ["SPY"], "5Min", o, o + timedelta(minutes=P["or_minutes"]), feed="iex")
    today_or = {}
    for sym in liquid:
        rng = s.opening_range(c.opening_bars(got.get(sym, []), day.today, P["or_minutes"]), P["or_minutes"])
        if rng:
            today_or[sym] = rng
    equity = float(account["equity"])
    day.p = s.resolve(P, equity)
    cands = s.rank_in_play(today_or, day.hist_or, day.stats, day.p, min_or_volume=MIN_IEX_OR_VOLUME)
    if day.p["allow_short"]:
        cands = [x for x in cands if x["direction"] == "long" or day.shortable.get(x["symbol"])]
    if day.p["market_filter"]:
        spy = s.opening_range(c.opening_bars(got.get("SPY", []), day.today, P["or_minutes"]), P["or_minutes"])
        if spy and spy["c"] < spy["o"]:
            cands = [x for x in cands if x["direction"] == "short"]
            log("Market filter: SPY opened red, skipping longs.")
    log(f"Account ${equity:,.2f}: {day.p['max_positions']} trade(s) at a time, "
        f"{day.p['max_leverage']:.0f}x buying power, shorts {'on' if day.p['allow_short'] else 'off'}.")
    log("Stocks in Play: " + (", ".join(f"{x['symbol']} {x['rvol']:.1f}x {x['direction']}" for x in cands[:10]) or "none today"))
    use_bp = min(float(account["buying_power"]), equity * day.p["max_leverage"]) if day.p["max_leverage"] > 1 else buying_cash(account)
    day.candidates = s.plan_trades(cands, equity, use_bp, day.p, open_symbols={x["symbol"] for x in positions})
    for t in day.candidates:
        log(f"  watching {t['symbol']}: {'buy above' if t['side'] == 'buy' else 'short below'} ${t['trigger']:.2f}, "
            f"stop ${t['stop']:.2f}, {t['qty']} sh (~${t['qty'] * t['trigger']:,.0f}, max loss ${t['qty'] * t['risk_per_share']:.2f}), "
            f"volume {t['rvol']:.1f}x normal")


def latest_prices(symbols):
    if not symbols:
        return {}
    q = urllib.parse.urlencode({"symbols": ",".join(symbols), "feed": "iex"})
    res = c.api("GET", f"{c.DATA_URL}/stocks/trades/latest?{q}") or {}
    return {k: v["p"] for k, v in (res.get("trades") or {}).items()}


def todays_orders(day):
    after = datetime.combine(day.today, dtime(0), ET).astimezone(c.UTC).isoformat()
    return c.trade_api("GET", f"/orders?status=all&limit=500&nested=true&after={urllib.parse.quote(after)}") or []


def open_order_list():
    """All working orders incl. stop-loss legs; asks flat and nested and merges."""
    seen, out = set(), []
    flat = c.trade_api("GET", "/orders?status=open&limit=500&nested=false") or []
    nested = c.trade_api("GET", "/orders?status=open&limit=500&nested=true") or []
    for o in flat + nested + [leg for o in nested for leg in (o.get("legs") or [])]:
        if o["id"] not in seen:
            seen.add(o["id"])
            out.append(o)
    return out


def flatten(reason):
    log(f"SELLING EVERYTHING: {reason}")
    trade("DELETE", "/orders")
    time.sleep(1)
    trade("DELETE", "/positions")


def risk_checks(day, account):
    """Returns (halt_everything, no_new_trades, reason)."""
    equity, last = float(account["equity"]), float(account["last_equity"])
    hist, fetched = day.history
    if hist is None or time.time() - fetched > 60:
        try:
            hist = c.trade_api("GET", "/account/portfolio/history?period=1A&timeframe=1D") or {}
        except RuntimeError:
            hist = hist or {}
        day.history = (hist, time.time())
    eq = [e for e in (hist.get("equity") or []) if e]
    peak = max(eq + [equity])
    if LOSS_FLOOR and equity < LOSS_FLOOR:
        return True, True, f"account ${equity:,.2f} is below your floor ${LOSS_FLOOR:,.2f}"
    if equity < peak * (1 - PEAK_DRAWDOWN_STOP):
        return True, True, f"account is down {100 * (1 - equity / peak):.0f}% from its peak (${peak:,.2f})"
    limit = (day.p or s.resolve(P, equity))["daily_loss_limit"]
    if equity - last <= -limit * last:
        return False, True, f"down {100 * (last - equity) / last:.1f}% today (limit {limit:.0%})"
    monday = day.today - timedelta(days=day.today.weekday())
    week_base = None
    for t_, e in zip(hist.get("timestamp") or [], hist.get("equity") or []):
        if e and datetime.fromtimestamp(t_, ET).date() < monday:
            week_base = e
    if week_base and equity - week_base <= -WEEKLY_LOSS_LIMIT * week_base:
        return False, True, f"down {100 * (week_base - equity) / week_base:.1f}% this week (limit {WEEKLY_LOSS_LIMIT:.0%})"
    return False, False, ""


def stored_risk(sym, day_orders):
    for o in day_orders:
        if o["symbol"] == sym and is_orb(o):
            parts = o["client_order_id"].split("-")
            if len(parts) >= 5:
                return int(parts[3]) / 100
    return None


def stop_orders_for(sym, side, open_orders):
    return [o for o in open_orders if o["symbol"] == sym and o["side"] == side and o["type"] in ("stop", "stop_limit")
            and o["status"] in ("new", "accepted", "held", "partially_filled")]


def protect(day, positions, open_orders, day_orders):
    """Every position always has a stop at Alpaca; optionally move it to breakeven."""
    for pos in positions:
        sym = pos["symbol"]
        qty = abs(int(float(pos["qty"])))
        long = pos["side"] == "long"
        stop_side = "sell" if long else "buy"
        stops = stop_orders_for(sym, stop_side, open_orders)
        covered = sum(int(float(o["qty"])) - int(float(o.get("filled_qty") or 0)) for o in stops)
        entry = float(pos["avg_entry_price"])
        risk = stored_risk(sym, day_orders) or entry * 0.01
        price = float(pos.get("current_price") or entry)

        be = (day.p or {}).get("breakeven_at_r") or 0
        if be and sym not in day.breakeven_done and stops:
            up = (price - entry) if long else (entry - price)
            current = float(stops[0]["stop_price"])
            needs = current < entry if long else current > entry
            if up >= be * risk and needs:
                log(f"{sym}: up {up / risk:.1f}x its risk. Moving the stop to break-even (${entry:.2f}).")
                for o in stops:
                    trade("DELETE", f"/orders/{o['id']}")
                time.sleep(0.5)
                trade("POST", "/orders", {"symbol": sym, "qty": str(qty), "side": stop_side, "type": "stop",
                                          "stop_price": f"{entry:.2f}", "time_in_force": "gtc"})
                day.breakeven_done.add(sym)
                continue
        if covered >= qty:
            continue
        stop = round(entry - risk if long else entry + risk, 2)
        if sym in day.breakeven_done:
            stop = round(entry, 2)
        log(f"{sym}: {qty - covered} shares unprotected, placing stop at ${stop:.2f}")
        trade("POST", "/orders", {"symbol": sym, "qty": str(qty - covered), "side": stop_side, "type": "stop",
                                  "stop_price": f"{stop:.2f}", "time_in_force": "gtc"})


def try_entries(day, account, positions, open_orders, day_orders):
    held = {p["symbol"] for p in positions}
    pending = [o for o in open_orders if is_orb(o) and o["status"] in ("new", "accepted", "partially_filled", "pending_new")]
    for o in pending:
        if (now_et() - c.parse_ts(o["submitted_at"])).total_seconds() > ENTRY_TIMEOUT_SEC:
            log(f"{o['symbol']}: entry didn't fill within {ENTRY_TIMEOUT_SEC}s (price ran), cancelling.")
            trade("DELETE", f"/orders/{o['id']}")
    pending_syms = {o["symbol"] for o in pending}
    filled_today = {o["symbol"] for o in day_orders if is_orb(o) and float(o.get("filled_qty") or 0) > 0}
    if len(filled_today) >= day.p["max_trades_per_day"]:
        if day.candidates:
            log(f"{day.p['max_trades_per_day']} trades today. That's the daily maximum.")
            day.candidates = []
        return
    slots = day.p["max_positions"] - len(held) - len(pending_syms)
    if slots <= 0:
        return
    watch = [t for t in day.candidates if t["symbol"] not in held | pending_syms | filled_today]
    prices = latest_prices([t["symbol"] for t in watch])
    cash = buying_cash(account) if day.p["max_leverage"] <= 1 else float(account["buying_power"])
    for t in watch:
        if slots <= 0:
            break
        px = prices.get(t["symbol"])
        if px is None:
            continue
        long = t["side"] == "buy"
        if not (px >= t["trigger"] if long else px <= t["trigger"]):
            continue
        if (px > t["trigger"] * (1 + MAX_CHASE)) if long else (px < t["trigger"] * (1 - MAX_CHASE)):
            continue  # ran too far; it may come back toward the breakout
        limit = round(t["trigger"] * (1 + MAX_CHASE) if long else t["trigger"] * (1 - MAX_CHASE), 2)
        qty = min(t["qty"], int(cash * 0.98 / limit))
        if qty < 1:
            continue
        cid = f"orb-{day.today:%Y%m%d}-{t['symbol']}-{int(round(t['risk_per_share'] * 100))}-{int(time.time())}"
        log(f"BREAKOUT {t['symbol']} at ${px:.2f}: {'BUY' if long else 'SHORT'} {qty} @ limit ${limit:.2f}, "
            f"stop ${t['stop']:.2f} (max loss ${qty * t['risk_per_share']:.2f}, volume {t['rvol']:.1f}x normal)")
        try:
            trade("POST", "/orders", {
                "symbol": t["symbol"], "qty": str(qty), "side": t["side"], "type": "limit",
                "limit_price": f"{limit:.2f}", "time_in_force": "gtc", "client_order_id": cid,
                "order_class": "oto", "stop_loss": {"stop_price": f"{t['stop']:.2f}"}})
            slots -= 1
            cash -= qty * limit
        except RuntimeError as e:
            log(f"{t['symbol']}: order rejected ({e}). Skipping it today.")
            day.candidates = [x for x in day.candidates if x["symbol"] != t["symbol"]]


def daily_report(day, account):
    """Plain-English summary of the day, shown on the GitHub run page."""
    orders = sorted([o for o in todays_orders(day) if o.get("filled_at")], key=lambda o: o["filled_at"])
    flat = []
    for o in orders:
        flat.append(o)
        flat.extend(l for l in (o.get("legs") or []) if l.get("filled_at"))
    book, trips = {}, []
    for o in sorted(flat, key=lambda o: o["filled_at"]):
        sym, q, px = o["symbol"], float(o["filled_qty"]), float(o["filled_avg_price"])
        sign = 1 if o["side"] == "buy" else -1
        pq, pp = book.get(sym, (0.0, 0.0))
        if pq == 0 or (pq > 0) == (sign > 0):
            nq = pq + sign * q
            book[sym] = (nq, (abs(pq) * pp + q * px) / abs(nq))
        else:
            closed = min(q, abs(pq))
            trips.append((sym, (px - pp) * closed * (1 if pq > 0 else -1)))
            nq = pq + sign * q
            book[sym] = (nq, pp) if nq else (0.0, 0.0)
    equity, last = float(account["equity"]), float(account["last_equity"])
    lines = [f"## {day.today:%A %b %d}: {'+' if equity >= last else ''}${equity - last:,.2f} "
             f"({(equity / last - 1) * 100:+.2f}%)", f"Account: **${equity:,.2f}**", ""]
    if trips:
        lines.append("| Stock | Result |")
        lines.append("|---|---|")
        lines += [f"| {sym} | {'+' if p >= 0 else ''}${p:,.2f} |" for sym, p in trips]
    else:
        lines.append("No trades today" + (f" ({day.halted_reason})" if day.halted_reason else "") + ".")
    text = "\n".join(lines)
    log(text.replace("\n", " | "))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


def main():
    started = now_et()
    log(f"Mode: {'LIVE (real money)' if c.LIVE else 'PAPER (fake money)'}{' / DRY RUN' if DRY_RUN else ''}")
    if not c.KEY or not c.SECRET:
        sys.exit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")
    clock = c.trade_api("GET", "/clock")
    if not clock["is_open"] and (c.parse_ts(clock["next_open"]) - now_et()) > timedelta(minutes=25):
        log(f"Market opens {c.parse_ts(clock['next_open']).astimezone(ET):%a %H:%M} ET. Nothing to do now.")
        return
    day = Day(now_et().date() if clock["is_open"] else c.parse_ts(clock["next_open"]).astimezone(ET).date())
    close = c.parse_ts(clock["next_close"]).astimezone(ET)
    open_time = datetime.combine(day.today, dtime(9, 30), ET)
    or_done = open_time + timedelta(minutes=P["or_minutes"], seconds=20)
    last_entry = open_time + timedelta(minutes=P["last_entry_minute"])
    fast_until = open_time + timedelta(minutes=70)
    fomc = day.today.isoformat() in FOMC_DAYS
    flatten_at = close - timedelta(minutes=FLATTEN_MIN_BEFORE_CLOSE)
    if fomc:
        flatten_at = min(flatten_at, datetime.combine(day.today, dtime(13, 50), ET))
        last_entry = min(last_entry, datetime.combine(day.today, dtime(13, 0), ET))
        log("Fed announcement day: flat by 1:50 PM ET.")

    while True:
        now = now_et()
        if now - started > MAX_RUNTIME:
            log("Shift over; the next shift takes it from here.")
            return
        if now > close + timedelta(minutes=2):
            log("Market closed. Done for today.")
            return
        loop = FAST_LOOP if or_done <= now < fast_until else SLOW_LOOP
        try:
            account = c.trade_api("GET", "/account")
            positions = c.trade_api("GET", "/positions") or []
            open_orders = open_order_list()

            # 1. Leftovers from a previous day
            if not day.cleaned and now >= open_time:
                old = [o for o in open_orders if c.parse_ts(o["submitted_at"]).astimezone(ET).date() < day.today]
                today_syms = {o["symbol"] for o in todays_orders(day) if o.get("filled_at")}
                stale = [p for p in positions if p["symbol"] not in today_syms]
                if old or stale:
                    log(f"Cleaning up {len(old)} old order(s) and {len(stale)} leftover position(s).")
                    stale_syms = {p["symbol"] for p in stale}
                    for o in open_orders:
                        if o in old or o["symbol"] in stale_syms:
                            trade("DELETE", f"/orders/{o['id']}")
                    time.sleep(1)
                    for p in stale:
                        trade("DELETE", f"/positions/{p['symbol']}")
                day.cleaned = True
                continue

            # 2. Safety switches
            halt, no_new, why = risk_checks(day, account)
            if halt:
                flatten(why + ". Trading stopped. Review, then re-enable the workflow.")
                daily_report(day, c.trade_api("GET", "/account"))
                sys.exit(1)
            if no_new and not day.halted_reason:
                day.halted_reason = why
                log(f"No new trades: {why}.")

            # 3. End of day
            if now >= flatten_at:
                if positions or open_orders:
                    flatten("Fed announcement at 2:00 PM" if fomc and now < close - timedelta(hours=1)
                            else "end of day, never hold overnight")
                elif not day.reported and now >= close - timedelta(minutes=5):
                    daily_report(day, account)
                    day.reported = True
                time.sleep(SLOW_LOOP)
                continue

            # 4. Protect every position
            day_orders = todays_orders(day) if (positions or now >= or_done) else []
            protect(day, positions, open_orders, day_orders)

            # 5. Prepare and pick today's Stocks in Play
            if not day.prepared and now < last_entry and now >= open_time - timedelta(minutes=20):
                if any(is_orb(o) for o in day_orders) or now > open_time + timedelta(hours=1):
                    day.prepared, day.candidates = True, []
                    day.p = s.resolve(P, float(account["equity"]))
                    log("Selection already handled earlier today; managing open trades only.")
                else:
                    prepare(day)
            if day.prepared and day.candidates is None and now >= or_done:
                select(day, account, positions)

            # 6. Enter breakouts
            if day.candidates and now < last_entry and not day.halted_reason:
                try_entries(day, account, positions, open_orders, day_orders)
            elif day.candidates and now >= last_entry:
                for o in open_orders:
                    if is_orb(o) and o["status"] in ("new", "accepted"):
                        trade("DELETE", f"/orders/{o['id']}")
                day.candidates = []
                log("Entry window closed for today. Managing open trades only.")
        except RuntimeError as e:
            log(f"Problem talking to Alpaca: {e}. Retrying.")
        time.sleep(loop)


if __name__ == "__main__":
    main()
