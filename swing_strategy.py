"""
SWING STRATEGIES: decided once a day from daily closing prices, traded at the
next morning's open. Shared by the backtest and the live trader, so what gets
tested is exactly what trades.

1. MOMENTUM ("momentum"): ride the market's strongest stocks.
   Every Monday, rank liquid stocks by their gain over the last ~6 months
   (ignoring the most recent week). Hold the top few, equal-weighted, and only
   stocks in their own uptrend (above the 200-day average). If the whole market
   (SPY) is below its 200-day average, go to cash.
   Risk: concentrated in a handful of hot stocks; they can drop fast.

2. PULLBACK ("pullback"): buy the dip in strong stocks.
   Buy liquid stocks in a long-term uptrend after a sharp 2-day drop
   (2-day RSI under 10). Sell when the price bounces back above its 5-day
   average, or after 10 days. Up to 3 positions.
   Risk: sometimes a dip keeps dipping.

3. LEVERAGED TREND ("trend"): 3x the Nasdaq-100 in uptrends, cash otherwise.
   Hold TQQQ (which moves about 3x the Nasdaq-100 each day) while QQQ is above
   its 200-day average; hold cash when it's below.
   Risk: the highest of the three. TQQQ can fall 20%+ in a few days.
"""

import numpy as np

PARAMS = {
    "momentum": {"top_k": 3, "lookback": 126, "skip": 5, "min_price": 10.0, "min_dollar_vol": 20e6},
    "pullback": {"slots": 3, "rsi_entry": 10.0, "max_hold": 10, "min_price": 20.0, "min_dollar_vol": 50e6},
    "trend": {"asset": "TQQQ", "signal": "QQQ", "sma": 200},
}
NEEDED_HISTORY = 260   # trading days of history the strategies need


def rolling_mean(X, n):
    """Mean of the last n rows (NaN unless all n are present). X: T x N."""
    X = np.asarray(X, dtype=float)
    valid = ~np.isnan(X)
    cs = np.cumsum(np.where(valid, X, 0.0), axis=0)
    cv = np.cumsum(valid, axis=0)
    out = np.full_like(X, np.nan)
    if X.shape[0] < n:
        return out
    s = cs[n - 1:].copy()
    k = cv[n - 1:].copy()
    s[1:] -= cs[:-n]
    k[1:] -= cv[:-n]
    out[n - 1:] = np.where(k == n, s / n, np.nan)
    return out


def rsi2(C):
    """2-day RSI (simple averages). C: T x N closes."""
    d = np.diff(C, axis=0, prepend=np.nan)
    g = rolling_mean(np.where(np.isnan(d), np.nan, np.maximum(d, 0)), 2)
    l = rolling_mean(np.where(np.isnan(d), np.nan, np.maximum(-d, 0)), 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = 100 - 100 / (1 + g / l)
    return np.where((l == 0) & ~np.isnan(g), 100.0, r)


class Indicators:
    """Everything the strategies look at, computed once for a T x N price matrix."""
    def __init__(self, C, V):
        self.C = C
        self.sma200 = rolling_mean(C, 200)
        self.sma5 = rolling_mean(C, 5)
        self.adv20 = rolling_mean(C * V, 20)
        self.rsi2 = rsi2(C)


def momentum_targets(t, ind, col, p=PARAMS["momentum"], is_rebalance_day=True, current=()):
    """Target weights for the next open. Rebalances weekly; holds otherwise."""
    C = ind.C
    spy = col["SPY"]
    if np.isnan(ind.sma200[t, spy]) or C[t, spy] < ind.sma200[t, spy]:
        return {}, "Market below its 200-day average: cash."
    if not is_rebalance_day and current:
        return {c: None for c in current}, "Holding until the weekly rebalance (Mondays)."
    lb, sk = p["lookback"], p["skip"]
    if t < lb + sk:
        return {}, "Not enough history."
    score = C[t - sk] / C[t - sk - lb] - 1
    ok = ((C[t] >= p["min_price"]) & (ind.adv20[t] >= p["min_dollar_vol"]) & (C[t] > ind.sma200[t])
          & ~np.isnan(score))
    for name in ("SPY", "QQQ", "TQQQ"):
        if name in col:
            ok[col[name]] = False
    idx = np.where(ok)[0]
    if len(idx) == 0:
        return {}, "No stocks qualify."
    best = idx[np.argsort(score[idx])[::-1][:p["top_k"]]]
    w = 1.0 / p["top_k"]
    return {int(c): w for c in best}, f"Top {p['top_k']} by 6-month momentum."


def pullback_targets(t, ind, col, held, p=PARAMS["pullback"]):
    """held: {col: entry_day_index}. Returns targets: None = keep, float = new weight."""
    C = ind.C
    targets, notes = {}, []
    for c, entry_t in held.items():
        if C[t, c] > ind.sma5[t, c]:
            notes.append(f"exit {c}: bounced above 5-day average")
        elif t - entry_t >= p["max_hold"]:
            notes.append(f"exit {c}: {p['max_hold']} days, time's up")
        else:
            targets[c] = None
    spy = col["SPY"]
    market_ok = not np.isnan(ind.sma200[t, spy]) and C[t, spy] > ind.sma200[t, spy]
    slots = p["slots"] - len(targets)
    if market_ok and slots > 0:
        ok = ((C[t] >= p["min_price"]) & (ind.adv20[t] >= p["min_dollar_vol"]) & (C[t] > ind.sma200[t])
              & (ind.rsi2[t] < p["rsi_entry"]))
        for name in ("SPY", "QQQ", "TQQQ"):
            if name in col:
                ok[col[name]] = False
        for c in list(held):
            ok[c] = False
        idx = np.where(ok)[0]
        for c in idx[np.argsort(ind.rsi2[t, idx])][:slots]:
            targets[int(c)] = 1.0 / p["slots"]
    return targets, "; ".join(notes) or ("Market below its 200-day average: no new buys." if not market_ok else "")


def trend_targets(t, ind, col, p=PARAMS["trend"]):
    s = col[p["signal"]]
    if np.isnan(ind.sma200[t, s]) or ind.C[t, s] < ind.sma200[t, s]:
        return {}, f"{p['signal']} below its 200-day average: cash."
    return {col[p["asset"]]: 1.0}, f"{p['signal']} above its 200-day average: hold {p['asset']}."
