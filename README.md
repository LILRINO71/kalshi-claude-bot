# Kalshi Claude Desk

A paper-trading research desk for Kalshi's 15-minute crypto markets, where the trading decisions come from Claude
through the Claude Code CLI: **no API key, no paid trading app, no real money.** It started as a free rebuild of
ProcessOverProfit's [Kalshi AI trading bot](https://code.processoverprofit.blog/docs/youtube-code/video-15-kalshi-ai-trading-bot/)
(which needs the paid Nightshark app) and grew into a full backtesting and live paper-trading system with a dashboard.

![stack](https://img.shields.io/badge/python-3.10%2B-blue) ![mode](https://img.shields.io/badge/mode-paper%20only-green)

## One-click start (Windows)

1. On GitHub, click **Code → Download ZIP** and unzip it (or `git clone` it).
2. Double-click **`Start Kalshi Desk.bat`**. It installs the requirements the first time, starts the dashboard and
   opens http://127.0.0.1:8050 in your browser. Keep that window open; close it to stop.
3. First time only: log the Claude Code CLI in to your Claude subscription (`claude`, then `/login`).

Tip: right-click the .bat → *Send to → Desktop (create shortcut)* for a one-click icon. Double-clicking again while
it's running just reopens the page.

## What it does

- **Backtests on real history.** Replays settled Kalshi markets (back to July 2026) using the exact bid/ask that existed
  at the decision minute, from Kalshi's per-minute candles, plus Coinbase 1-minute spot candles. Nothing after the
  decision time is visible to the strategy, and prompts contain no dates or tickers.
- **Realistic fills.** Entries fill at the ask *after* the model's real response time, plus configurable slippage and
  Kalshi's taker fee (7% × C × P × (1−P), rounded up). Live mode walks the real order book.
- **A $1,000 paper bankroll** with fixed, percent-of-equity, or fractional-Kelly sizing, a per-trade cap, a max price,
  and daily profit/loss stops.
- **Claude vs. free baselines.** A random-walk fair-value model, 5-minute momentum, "buy the favorite," and a coin flip
  run instantly with no AI, so you can see whether Claude adds anything.
- **Honest scoring.** Win rate with a 95% Wilson interval, bootstrap CI on profit per trade, max drawdown, a Brier score
  compared against the market's own implied probability ("skill vs market"), a calibration chart, and a plain-English
  verdict on whether the result is distinguishable from luck.
- **Usage tracking.** Every Claude call logs tokens, thinking tokens, latency and errors; the dashboard shows usage by
  day, by model and effort, and warns when you hit a plan limit.
- **Lean CLI calls.** Calls run with a custom system prompt and no tools, MCP servers or settings: about **1.3k tokens
  of overhead instead of ~72k**, which is what makes backtesting on a subscription practical. Decisions are cached by
  (model, effort, prompt), so re-running a backtest is free.

## Setup (Windows)

```bash
pip install -r requirements.txt
```

Log the Claude Code CLI into your Claude subscription once: run `claude`, type `/login`, and pick your Claude account
(not an API key). The bot finds the CLI on `PATH`, in `%APPDATA%\Claude\claude-code\`, or in the Microsoft Store package
folder. Set `CLAUDE_BIN` if yours lives elsewhere.

## Run

```bash
python bot.py
```

That opens the dashboard at http://127.0.0.1:8050. It has four tabs:

| Tab | What you do there |
|---|---|
| **Backtest** | Pick a date range, number of markets and sampling; see an estimate of Claude calls, time and tokens; run it; compare up to 3 equity curves; read every decision with Claude's reasoning. |
| **Live paper** | Start/stop paper trading on the markets open right now; watch equity, positions and an activity feed. |
| **Usage** | Calls and tokens today / 5 h / 7 d, per model and effort, recent calls, limit warnings. |
| **Settings** | Bankroll, sizing, max price, slippage, fees, daily stops, decision timing, assets, and the strategy notes sent to Claude. |

Switch **strategy, model (haiku / sonnet / opus / fable) and effort (low → max)** from the top bar at any time. Live
trading picks up the change at its next decision.

Terminal versions:

```bash
python bot.py backtest --strategy random_walk --count 200     # free baseline
python bot.py backtest --model sonnet --effort medium --count 50
python bot.py live                                             # live paper trading, no dashboard
python bot.py test                                             # one Claude decision on the live market
python bot.py usage
```

## Markets it trades

| Market | Kalshi series | Question | Spot data | Backtest history |
|---|---|---|---|---|
| BTC, ETH, SOL, XRP, DOGE, HYPE, BNB | `KX{COIN}15M` | Will the 60-second average at close be at or above the average at open? (every 15 min, 24/7) | Coinbase 1-minute candles | back to July 19, 2026 |
| S&P 500 | `KXINXU` | Will the index be above strike K at the top of the hour? (hourly, market hours) | Yahoo `^GSPC` 1-minute | last ~29 days |
| Nasdaq-100 | `KXNASDAQ100U` | Same, for the Nasdaq-100 | Yahoo `^NDX` 1-minute | last ~29 days |

Kalshi has no recurring price markets on individual stocks (Apple, Tesla and similar only appear as one-off
earnings/KPI questions), so the stock side trades the index ladders. Each hour has about 60 strikes; the bot trades the
strike nearest the index level at decision time, which is the most balanced and usually the most liquid. Its 15-minute
S&P and Nasdaq series currently list no markets. The hourly Dow (`KXDJI`) is left out because it settles on a US30
CFD price, not the index Yahoo publishes.

## How a decision works

At `time_delay` minutes before close (default 10 for crypto; `index_time_delay`, default 15, for stocks), the strategy gets the strike, the Kalshi bid/ask for UP and DOWN,
spot price vs. strike, 1/5/15/60-minute moves, 1-minute volatility, the last 15 closes, and a random-walk probability.
Claude returns `UP`, `DOWN` or `SKIP` plus its own P(up) and a reason, as structured JSON. Positions are held to
Kalshi's official settlement.

## Project layout

```
kalshibot/
  markets.py     Kalshi + Coinbase public data, disk cache, order-book walking
  snapshot.py    what a strategy sees at decision time, features, the prompt
  claude_cli.py  lean `claude -p` runner, usage log, decision cache
  strategies.py  Claude and the no-AI baselines
  portfolio.py   paper account, sizing, fees, daily stops
  backtest.py    load → decide (parallel) → simulate pipeline
  live.py        live paper trader
  metrics.py     Wilson / bootstrap CIs, Brier, calibration, drawdown
  usage.py       usage aggregation
  web/           Flask dashboard (Chart.js)
tests/           offline unit tests (pytest)
data/            settings, caches, backtest results, usage log (git-ignored)
```

## Honest expectations

These markets are priced by fast professional traders. In testing, Kalshi's own prices score a Brier of about 0.165,
far better than a coin flip's 0.250, so the bar is high. A strategy only has an edge if its "skill vs market" is
positive **and** the profit-per-trade confidence interval sits above zero over a few hundred trades, confirmed on a date
range you didn't tune on.

Kalshi accounts require users to be 18+ and identity-verified. This project is for research and paper trading only and
contains no code that can place a real order.
