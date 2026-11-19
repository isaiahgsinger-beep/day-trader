# Day trader: Opening Range Breakout on Stocks in Play (final version)

A fully automatic day trader for Alpaca. No approvals. It runs all session,
checking prices every 4 seconds in the first hour, and **sells everything
before the close every day**.

## The strategy, in plain English
Based on the most-cited study of this approach (Zarattini, Barbon & Aziz, 2024:
7,000+ stocks, 2016–2023). The profits came from stocks with unusual activity
that day, usually because of news.

1. **Before the open:** list every liquid stock (price $5+, 1M+ shares a day, moves at least $0.50 a day).
2. **9:35 AM:** rank today's **Stocks in Play** by how much more than normal they traded in the first 5 minutes. Watch the **top 6**.
3. **Green first candle:** buy the instant the price breaks above that candle's high. **Red first candle:** short below its low ($2,000+ accounts only).
4. **Tight stop-loss** at 10% of the stock's normal daily range, sent with the buy. It never expires.
5. **3:45 PM:** sell everything. A report of the day's results appears on GitHub.

Expect many small losses and some big winners. Winning fewer than half the trades is normal for this strategy.

## It grows with your account (automatic)
| Account | Trades at once | Buying power | Shorts |
|---|---|---|---|
| Under $2,000 | 1, using the whole account | 1x (no borrowing) | Off (Alpaca requires $2,000) |
| $2,000+ | 2 | Up to 2x | On |
| $10,000+ | 4 | Up to 2x | On |

No single trade can lose more than 1.5% of the account if the stop fills at its price.
A stock that jumps past its stop (sudden news, trading halt) can lose more.

## Safety switches (automatic)
- **Down 30% from its peak, or below `LOSS_FLOOR`:** sells everything and stops for good. GitHub emails you about the failed run.
- **Down 5% today:** no new trades today. **Down 10% this week:** none this week. **5 trades a day maximum.**
- **Fed announcement days:** flat by 1:50 PM, before the 2:00 PM statement. Fed dates are listed in `live.py`; check federalreserve.gov each year.
- **Every position always has a stop at Alpaca.** If one is ever missing, the bot adds it within seconds.
- **Leftover positions** from a previous day are sold at the open.
- **Keep-alive job:** stops GitHub from switching the trader off after 60 days.

## Setup (about 20 minutes, free)
**1. Alpaca:** sign up at https://alpaca.markets. With **Paper** selected, generate **API keys** and copy the Key and the Secret.

**2. GitHub:** sign up at https://github.com and create a new **public** repository (public = unlimited free run time; your keys stay hidden).

**3. Upload the code:** Add file → Upload files → drag in `strategy.py`, `common.py`, `live.py`, `backtest.py`, `README.md` → Commit.
Then for each workflow: Add file → Create new file → name it exactly `.github/workflows/trade.yml`
(then `.github/workflows/backtest.yml` and `.github/workflows/keepalive.yml`) → paste its contents → Commit.

**4. Keys:** Settings → Secrets and variables → Actions → **New repository secret**: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`.

**5. Permissions:** Settings → Actions → General → Workflow permissions → **Read and write** → Save.

## The plan: backtest, then paper, then real money
**1. Backtest first.** Actions → **Backtest** → Run workflow (12 months). It takes 20–40 minutes.
The results table compares your setup with each alternative: watching all 20 stocks,
a break-even stop, 2 at a time, a 15-minute range, a wider stop, a market filter,
starting at $2,000, and a **CHECK** row that copies the research setup.
- If **YOUR SETUP** beats just holding SPY with a drawdown you can live with, go on to paper trading.
- If an alternative clearly beats your setup, switch to it with the matching variable below, then re-run the backtest.
- If nothing beats SPY, don't trade it.

**2. Paper trading** starts automatically on weekdays. Give it 4–6 weeks (ideally 100+ trades).
Read each day's report on the run's Summary page (Actions → Day trader → a run).

**3. Real money:** fund your live Alpaca account, make **live** keys, and replace the two secrets.
Then add the variables `ALPACA_LIVE` = `yes` and `LOSS_FLOOR` (for example `450`).

## Settings (Settings → Secrets and variables → Actions → Variables)
| Variable | Default | What it does |
|---|---|---|
| `MAX_POSITIONS` | auto | Trades at once (see table above) |
| `MAX_LEVERAGE` | auto | 1 under $2k, 2 above |
| `ALLOW_SHORT` | auto | On at $2k+ |
| `RISK_PCT` | 0.015 | Max loss per trade (1.5% of account) |
| `WATCH_TOP` | 6 | How many Stocks in Play to watch (20 = research) |
| `BREAKEVEN_AT_R` | 0 (off) | `1` = move the stop to entry once up 1x the risk |
| `MARKET_FILTER` | no | `yes` = skip longs when the market opens red |
| `LOSS_FLOOR` | off | Sell everything and stop below this amount |
| `DRY_RUN` | no | `yes` = log only, no orders |
| `ALPACA_LIVE` | no | `yes` = real money |

## Emergency stop
Actions → Day trader → ⋯ → **Disable workflow**, then close positions in the Alpaca dashboard.

## Honest limits
- Under $2,000, each trade typically risks only a few dollars, because the stop is tight and you can't borrow. The biggest lever for dollar results is account size; settings can't change that.
- The live bot's volume readings use the free IEX feed, which is rougher than the full-market data the backtest uses.
- The backtest uses today's stock list (delisted companies are missing), and real fills can be worse than modeled.
- Strategies can stop working. The CHECK row shows whether this one still holds up.
