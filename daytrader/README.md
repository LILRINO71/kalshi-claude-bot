# AI Day Trader (US stocks, paper only)

A self-learning day trader whose brain is Claude, called through the Claude Code CLI on your Claude
subscription: **no API key, no broker account, no real money.** It doesn't follow a fixed strategy. It
keeps a playbook of its own rules and a memory of lessons, and rewrites both after every session based
on how its trades actually turned out.

## A trading day

| Time (New York) | Step | What happens |
|---|---|---|
| 09:00 | **Scan** | Code ranks ~110 liquid stocks by pre-market gap relative to each stock's ATR, pre-market activity and liquidity, and counts each name's mentions in the overnight headlines. |
| 09:00 | **Morning brief** | Claude reads the overnight and pre-market news, SPY/QQQ/IWM, the scanner, its playbook, the lessons it recalls and its last sessions. It picks up to 4 stocks to focus on (zero is allowed) and states the market regime. |
| 09:00 | **Research** | For each focus stock, Claude gets the previous session, ATR, 20-day range, the pre-market bars, the stock's own news and the lessons it has about that stock or setup. It argues a bull case and a bear case, then writes at most one **trade plan** per stock: entry type and price, stop, target, valid window, optional time exit and breakeven move, conviction-scaled size, the setup, and the playbook rules it relies on. |
| 09:35–15:55 | **Trading** | The paper broker works the plans bar by bar. Code sizes every position (0.5% of equity at risk by default), caps it, and enforces a daily loss stop; Claude never sets share counts. |
| 10:00, 11:30, 13:30, 15:00 | **Check-ins** | Claude sees each stock in play: price, VWAP, opening range, 30-minute bars, its position, working orders and news since the open. It can hold, move a stop or target, cancel, exit, or enter a new plan. Its answer takes effect only after its real response time. |
| 15:55 | **Flatten** | Every position is closed. Day trading only. |
| 16:05 | **Reflection** | Claude reviews each decision, separating bad luck from bad process. It checks what the scanner's top names actually did, so it learns from what it missed. It writes lessons, judges each rule it used, and proposes new rules, edits and retirements. |
| every 5 days | **Curation** | Small add, edit, merge and retire operations keep the playbook sharp and under 25 rules. It is never rewritten wholesale. |

That is about 7 Claude calls a day: brief, research, up to 4 check-ins (skipped when nothing is in play) and reflection.

## How it learns, and why it doesn't just chase noise

- **Rules are judged on results, not on opinions.** Every trade credits its R-multiple to the rules it cited: half to the
  primary rule, half split among the rest. Stats are shrunk toward zero (`R~ = total R / (trades + 5)`), so two lucky
  trades can't make a rule. Thresholds:

  | Change | Condition |
  |---|---|
  | trial → active | 5+ trades, shrunk R above +0.05 |
  | active → trial | 8+ trades, shrunk R below 0 |
  | retired | 6+ trades, shrunk R below −0.15 |
  | dormant | unused for 15 days |

  Claude's own helpful/harmful verdicts can retire only rules that have no trades yet.
- **Memory has two layers.** Short-term lessons have a 3-day half-life and long-term ones 60 days. Recall scores recency +
  importance + BM25 relevance. Lessons and queries carry "situation fingerprint" tokens (gap size, pre-market activity,
  volatility, trend, sector, market gap, regime), so it recalls lessons from similar situations.
- **A graveyard.** Retired rules, with the reason, are shown to the reflection and curation steps so they aren't reinvented.
- **Safety rails stay in code.** Position sizing, caps, the daily loss stop, the forced flatten, and price sanity checks:
  plans more than 15% from the price, or with a stop outside 0.1–8%, are rejected.

The design borrows from TradingAgents, ai-hedge-fund, FinMem, ACE and others; see [CREDITS.md](../CREDITS.md).

## Backtests you can trust (and their limits)

- **Nothing from the future.** Every read goes through a `View` that hides bars that hadn't finished and headlines not yet
  published at the simulated moment.
- **News timing.** There are three sources, each used according to how well it is timestamped:
  - **SEC EDGAR filings** have exact acceptance times, taken from each filing's header because the JSON index gets the
    time zone wrong. These include earnings 8-Ks, other 8-K events, 10-Qs, stock offerings and activist stakes. A 6:59
    earnings release is visible at 9:00; one filed at 16:10 is not visible until then. Filings also boost the scanner.
  - **GDELT DOC API** headlines have "seen" times exact to about 15 minutes and are held back one more 15-minute cycle.
    GDELT covers about the last 3 months and often rate-limits shared or office networks. The run reports when that
    happens; set `use_gdelt` to false to skip it.
  - **Google News search** gives past results a date but no time, so a headline dated day D could have come out at 23:59
    on D. Each one is held back until D is over. Without that rule, a 9:00 brief would read "stocks close higher"
    stories from the same day.
- **Conservative fills.** Orders become eligible only on the first bar after Claude's real answer time. Stop entries fill
  at the trigger, or at the open if the bar gapped past it, plus slippage. Limits must trade through by a tick. A bar that
  touches both the stop and the target is counted as a stop. The target is never credited on the entry bar. Gaps through
  stops fill at the open.
- **No days the model may remember.** A model can't be tested on days it read about in training. Claude backtests are
  refused before the model's training cutoff (June 2026 for Opus 5.5; set others in `knowledge_cutoff`) unless you pass
  `--allow-leaky`, and such runs are flagged in the report.
- **The data window is short.** Yahoo serves 5-minute bars with pre-market for about the last 60 days. Every completed day
  is cached in `data/daytrader/cache`, so history grows the longer you use it. Yahoo reports no pre-market volume, so
  pre-market activity is measured as the share of 5-minute bars that traded.
- **Controls.** `--brain gap_and_go` (a fixed-rule pre-market-range breakout, no AI) and `--brain none` run free. Run them
  on the same dates. Run Claude with `--no-learn` to see whether the learning loop itself adds anything.

## Run it

Log the Claude Code CLI into your subscription once (`claude`, then `/login`), then from the repo folder:

```bash
pip install -r requirements.txt

# Free controls first (no Claude calls)
python -m daytrader backtest --brain gap_and_go --start 2026-08-11 --end 2026-10-07

# Claude, learning as it goes (about 7 calls per trading day)
python -m daytrader backtest --model opus --effort medium --start 2026-08-11 --end 2026-10-07 --save-profile live
python -m daytrader backtest --model opus --effort medium --start 2026-08-11 --end 2026-10-07 --no-learn   # ablation

# What would it trade today? (morning brief + plans, printed, no trading)
python -m daytrader premarket

# Live paper trading in real time, carrying its playbook from day to day
python -m daytrader live                 # leave it running; --once for a single day

python -m daytrader runs                 # list backtests
python -m daytrader playbook --profile live
python -m daytrader settings --set risk_per_trade_pct=0.25 focus_max=3
```

On Windows you can also double-click **`Start Day Trader.bat`**. It installs the requirements and gives you a menu:
morning brief, live paper trading, or a backtest.

Each backtest writes `data/daytrader/runs/<id>/report.html` with:
- the equity curve against SPY;
- every day's brief, review and lessons;
- every trade with its R and the rules it cited;
- the playbook with its evidence, and a log of every change to it.

Runs save after every day; `--resume <id>` continues an interrupted one. `--profile` starts a backtest from a saved
memory; `--save-profile` keeps what a run learned (the `live` profile is what live trading uses).

## Settings (`data/daytrader/settings.json`)

| Key | Default | Meaning |
|---|---|---|
| `model` / `effort` | sonnet / medium | Claude model and effort (`haiku`, `sonnet`, `opus`, `fable`) |
| `bankroll` | 25000 | Starting paper equity |
| `risk_per_trade_pct` | 0.5 | Equity lost if a full-size trade hits its stop |
| `max_position_pct` / `max_leverage` / `max_positions` | 25 / 1.0 / 3 | Exposure caps |
| `max_daily_loss_pct` | 2.0 | Flatten and stop for the day |
| `allow_short` | true | Allow short plans |
| `focus_max` / `scan_top` | 4 / 25 | Stocks focused per day / scanner candidates shown |
| `checkins` / `flatten_time` | 10:00, 11:30, 13:30, 15:00 / 15:55 | Intraday schedule |
| `slippage_bps` / `commission_per_share` | 2 / 0 | Costs |
| `learn` / `curate_every` / `max_rules` | true / 5 / 25 | Learning loop |
| `universe` | all of `universe.py` | Tickers to scan |
| `use_sec` / `use_gdelt` | true / true | News sources (Google News is always on) |
| `sec_user_agent` | generic | SEC asks automated tools to send "Your Name you@example.com"; set yours here |
| `instructions` | "" | Your own notes, included in every prompt |

## Honest expectations

Intraday stock trading is dominated by professionals with better data and faster execution. About 40 trading days of
post-cutoff data is not enough to prove an edge: the report's verdict says so until there are 100+ trades. Treat the
backtest as a check that the machinery works, compare it with the controls, and use live paper trading as the real
out-of-sample test. This code never places real orders.
