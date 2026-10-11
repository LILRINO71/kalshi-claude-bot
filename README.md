# AI Day Trader

A self-learning day trader for US stocks whose brain is Claude, called through the Claude Code CLI on your
Claude subscription: **no API key, no broker account, no real money.** It doesn't follow a fixed strategy. It
keeps a playbook of its own rules and a memory of lessons, and rewrites both after every session based on how its
trades, and the trades it didn't take, actually turned out.

![stack](https://img.shields.io/badge/python-3.10%2B-blue) ![mode](https://img.shields.io/badge/mode-paper%20only-green)

## One-click start (Windows)

1. Install [Python 3.10+](https://www.python.org/downloads/) and tick **Add python.exe to PATH**.
2. Log the Claude Code CLI in to your Claude subscription once: run `claude`, type `/login`, pick your Claude account.
3. Double-click **`Start Day Trader.bat`**. It installs the requirements the first time and gives you a menu:
   dashboard, live paper trading, morning brief, or a backtest.

Or from a terminal in this folder:

```bash
pip install -r requirements.txt
python -m daytrader                      # dashboard at http://127.0.0.1:8050
```

## A trading day

| Time (New York) | Step | What happens |
|---|---|---|
| 09:00 | **Scan** | Code ranks ~110 liquid stocks by pre-market gap relative to each stock's ATR, pre-market activity, liquidity, headline mentions and SEC filings. |
| 09:00 | **Morning brief** | Claude reads the overnight news, SPY/QQQ/IWM, the scanner, its playbook, recalled lessons, its **track record** and the **base rates** (below). It picks up to 4 stocks to focus on (zero is allowed) and states the regime. |
| 09:00 | **Research** | For each focus stock: the previous session, ATR, 20-day range, pre-market bars, its own news, lessons about it, and how simple setups have recently played out on it. Claude argues a bull and a bear case, then writes at most one **trade plan** per stock: entry type and price, stop, target, window, optional time exit and breakeven move, size, setup and the rules it relies on. |
| 09:35–15:55 | **Trading** | The paper broker works the plans bar by bar. Code sizes every position (0.5% of equity at risk by default), caps it, and enforces a daily loss stop. Claude never sets share counts. Profit-taking is code too: at +0.25R three quarters of the position is sold and the stop on the rest moves to the entry (`scale_out_r`, `scale_out_frac`). |
| 10:00, 11:30, 13:30, 15:00 | **Check-ins** | Claude sees each stock in play (price, VWAP, opening range, 30-minute bars, position, working orders, news since the open) plus its **priced intervention record**, and can hold, adjust, cancel, exit or enter. Its answer takes effect only after its real response time. |
| 15:55 | **Flatten** | Every position is closed. Day trading only. |
| 16:05 | **Evidence** | Code settles every plan (filled, expired, cancelled), prices every check-in change against the same plan left alone, and replays four mechanical setups on the scanner's top names. |
| 16:05 | **Reflection** | Claude reviews each decision, separating bad luck from bad process, using all of the above. It writes lessons, judges each rule it used, and proposes at most two new rules with their evidence, plus edits and retirements. |
| every 5 days | **Curation** | Small add, edit, merge, retire and revive operations keep the playbook sharp and under 25 rules. Never a wholesale rewrite. |

That is about 7 Claude calls a day: brief, research, up to 4 check-ins (skipped when nothing is in play) and reflection.

## How it learns

The learning loop is built so that numbers decide, not stories:

- **Rules are judged on results.** A rule's record is the R-multiples of the trades that cited it. Stats are shrunk
  toward zero (`R~ = total R / (trades + 5)`), so two lucky trades can't make a rule:

  | Change | Condition |
  |---|---|
  | trial → active | 5+ trades, shrunk R above +0.05 |
  | active → trial | 8+ trades, shrunk R below 0 |
  | retired | 6+ trades, shrunk R below −0.15 |
  | dormant | unused for 15 days |

  Claude's own helpful/harmful verdicts can retire only rules that have no trades yet.
- **Unproven ideas trade small.** A plan whose cited rules are all still on trial is capped at half size
  (`trial_size_cap`) until one of them earns promotion. New ideas are cheap to try and are judged by their trades.
- **A track record, by the numbers.** Code keeps every closed trade and shows Claude its results split by side, time
  of entry, pre-market plan vs check-in entry, exit type, confidence, and profit capture (how far trades ran in its
  favor vs where they finished). Systematic mistakes, like entering in the first 25 minutes or exiting winners early,
  show up as numbers instead of anecdotes.
- **Check-in interventions are priced.** Every early exit, stop/target change, cancel or replacement is compared with
  what the original plan would have done if left alone (same bars, same fill rules). Claude sees the running total per
  kind of intervention, so it learns whether its intraday meddling helps or hurts.
- **Base rates from the environment.** After each close, code replays four simple setups on the scanner's top ~15 names:
  opening-range breakout with the gap, against the gap, pre-market high/low break, and a 10:30 trend entry. That is
  a few dozen extra trades per day, far more than the account's own two or three, so the trader can see what the
  current market is rewarding ("breakouts with the gap are paying this month, fades are not").
- **Two memory layers.** Short-term lessons have a 3-day half-life and long-term ones 60 days. Recall scores recency,
  importance and BM25 relevance, with "situation fingerprint" tokens (gap size, pre-market activity, volatility, trend,
  sector, market gap, regime). A lesson that repeats an existing one reinforces it instead of piling up as a copy.
- **No re-inventing.** A proposed rule that repeats a live rule, or one retired for losing, is dropped; retired rules are
  shown to the reflection and curation steps as a graveyard.
- **Safety rails stay in code.** Sizing, caps, the daily loss stop, the forced flatten and price sanity checks (plans
  more than 15% from the price, or with a stop outside 0.1–8%, are rejected).

The design borrows from TradingAgents, ai-hedge-fund, FinMem, ACE and others; see [CREDITS.md](CREDITS.md).

## How it's tested

1. **Walk-forward backtests.** The trader lives through each past day in order and learns as it goes. Every read goes
   through a `View` that hides bars that hadn't finished and headlines not yet published at the simulated moment, and
   the evidence from each day is only used on later days.
2. **Controls on the same days.** `--brain gap_and_go` (a fixed-rule pre-market-range breakout, no AI) and `--brain none`
   run free. `--no-learn` runs Claude with a frozen playbook, which isolates what the learning loop adds.
3. **Paired comparison.** `python -m daytrader compare <control> <claude>` (or select runs in the dashboard and press
   Compare) lines the runs up on their shared days and gives a bootstrap interval for the daily P&L difference.
4. **Learning curve.** Each report shows average R per block of five days and the priced interventions, so you can see
   whether later days beat earlier ones.
5. **Calm vs volatile days.** Each report labels every day calm, normal or volatile (VIX, SPY's range, and how many of
   the scanner's stocks moved 5%+) and splits the results, so you can see how the trader and the mechanical setups
   did in each kind of market. `python -m daytrader regimes` shows the same gauges month by month, to pick windows.
6. **Live paper trading is the real test.** Anything tuned on a fixed backtest window can overfit it. Live paper trading
   runs on days that didn't exist when anything was tuned. The verdict asks for 100+ trades with the average-R interval
   above zero, and estimates how many trades it would take at the current average and spread.

### Longer backtests

Two limits decide how far back a fair backtest can go:

- **Bars.** Yahoo serves 5-minute bars for only ~60 days. A free [Alpaca](https://alpaca.markets) account (sign up,
  open the paper-trading dashboard, generate an API key; no money involved) gives bars back to 2016. Paste the key in the
  dashboard's Settings tab or run `python -m daytrader alpaca KEY_ID SECRET`. Older days are then fetched from Alpaca,
  normalized to look like Yahoo's, and cached.
- **The model's memory.** Claude may remember markets before its training cutoff (June 2026 for the current models), so
  Claude backtests start at the cutoff at the earliest. With an Alpaca key, `python -m daytrader backtest` and the
  dashboard default to that window: July 1, 2026 to today. Earlier, more volatile periods (for example March 2026)
  can still be run with `--allow-leaky` as a flagged stress test, but their results are inflated by memory.

Backtest fills are conservative: orders become eligible only on the first bar after Claude's real answer time; stop
entries fill at the trigger or the gapped open, plus slippage; limits must trade through by a tick; a bar that touches
both stop and target counts as a stop; the target is never credited on the entry bar; gaps through stops fill at the
open; an entry that gaps past its target or more than 1R past its trigger is cancelled.

News timing: **SEC EDGAR** filings carry exact acceptance times (taken from each filing's header). **GDELT** headlines
are exact to about 15 minutes and held back one more cycle; GDELT covers about 3 months and often rate-limits shared
networks (set `use_gdelt` to false to skip it). **Google News** results are dated to the day only, so each is held back
until its day is over. With an Alpaca key, **Alpaca's news service** (Benzinga) adds headlines with exact times for
past days as well as today: economic releases and market stories for the morning brief, the latest headlines on every
scanner name, and each focus stock's news. A story edited after it came out counts only from its last edit. Without
it, a backtest's morning has almost no same-day headlines (GDELT is usually rate-limited), while a live morning does,
so backtests would be tested on thinner news than live trading gets.

**No days the model may remember.** Claude backtests before the model's training cutoff (June 2026 for the current
models; override per model in `knowledge_cutoff`) are refused unless you pass `--allow-leaky`, and such runs are flagged.

## Commands

```bash
python -m daytrader                       # dashboard (backtests, compare, live account, usage, settings)

# Free controls first (no Claude calls)
python -m daytrader backtest --brain gap_and_go --start 2026-08-11 --end 2026-10-08

# Claude, learning as it goes (about 7 calls per trading day); save what it learns for live trading
python -m daytrader backtest --start 2026-08-11 --end 2026-10-08 --save-profile live
python -m daytrader backtest --start 2026-08-11 --end 2026-10-08 --no-learn        # ablation
python -m daytrader compare <control-run-id> <claude-run-id> <frozen-run-id>

python -m daytrader premarket             # what would it trade today? (brief + plans, printed, no trading)
python -m daytrader live                  # live paper trading in real time; --once for a single day

python -m daytrader regimes               # how volatile each month was (VIX, SPY range, big movers)
python -m daytrader alpaca KEY SECRET     # optional: free Alpaca key for 5-minute history older than ~60 days
python -m daytrader runs                  # list backtests
python -m daytrader playbook --profile live
python -m daytrader settings --set risk_per_trade_pct=0.25 focus_max=3
```

Pick the model with `--model sonnet|opus|haiku|fable` and `--effort low|medium|high|xhigh|max`, or from the dashboard's
top bar. The default is Sonnet 5.5 at medium effort; Opus 5.5 is stronger and heavier on your plan's usage limits.

Each backtest writes `data/daytrader/runs/<id>/report.html` with the equity curve against SPY, the learning curve,
priced interventions, base rates, every day's brief, review and lessons, every trade with its R and rules, and the
playbook with its evidence and change log. Runs save after every day; `--resume <id>` continues an interrupted one.
`--profile` starts a backtest from a saved memory; `--save-profile` keeps what a run learned (the `live` profile is what
live trading uses, and an existing profile is never overwritten).

## Settings (`data/daytrader/settings.json`, or the dashboard's Settings tab)

| Key | Default | Meaning |
|---|---|---|
| `model` / `effort` | sonnet / medium | Claude model and effort |
| `bankroll` | 25000 | Starting paper equity |
| `risk_per_trade_pct` | 0.5 | Equity lost if a full-size trade hits its stop |
| `max_position_pct` / `max_leverage` / `max_positions` | 25 / 1.0 / 3 | Exposure caps |
| `max_daily_loss_pct` | 2.0 | Flatten and stop for the day |
| `allow_short` | true | Allow short plans |
| `focus_max` / `scan_top` | 4 / 25 | Stocks focused per day / scanner candidates shown |
| `checkins` / `flatten_time` | 10:00, 11:30, 13:30, 15:00 / 15:55 | Intraday schedule |
| `slippage_bps` / `commission_per_share` | 2 / 0 | Costs |
| `learn` / `curate_every` / `max_rules` | true / 5 / 25 | Learning loop |
| `trial_size_cap` | 0.5 | Largest size for a plan that relies only on unproven rules |
| `baserate_names` | 15 | Scanner names the mechanical setups are replayed on each day |
| `universe` | all of `universe.py` | Tickers to scan |
| `use_sec` / `use_gdelt` / `use_alpaca_news` | true / true / true | News sources (Google News is always on; Alpaca's needs a saved key) |
| `sec_user_agent` | generic | SEC asks automated tools to send "Your Name you@example.com"; set yours here |
| `instructions` | "" | Your own notes, included in every prompt |

## Project layout

```
daytrader/
  session.py     one trading day: scan, brief, research, check-ins, flatten, evidence, reflection
  prompts.py     every prompt and JSON schema Claude answers
  memory.py      playbook (outcome-scored rules), layered lessons, track record, base rates, intervention ledger
  replay.py      counterfactuals: plans left alone, mechanical setups on the scanner's names
  broker.py      conservative paper broker (stops, targets, slippage, caps)
  data.py        Yahoo bars, SEC filings, Google News + GDELT headlines, as-of views
  alpaca.py      optional older 5-minute bars from Alpaca's free market-data API
  regime.py      calm / normal / volatile day labels from VIX, SPY range and big movers
  backtest.py    walk-forward backtest; live.py: real-time paper trading
  metrics.py     verdicts, confidence intervals, learning curve, run comparison
  llm.py         lean `claude -p` runner (exact model IDs, cached answers, usage log)
  web.py         dashboard (Flask); templates/ and static/ hold the page
tests/           offline unit tests (pytest)
data/            settings, caches, runs, profiles, usage log (git-ignored)
```

## Honest expectations

Intraday stock trading is dominated by professionals with better data and faster execution, and most simple intraday
setups lose after costs. This project makes the trader learn quickly and measure itself honestly; it cannot promise an
edge. Treat backtests as a check that the machinery works and a way to compare versions, and use live paper trading as
the real out-of-sample test. This code never places real orders.
