# Kalshi Claude Paper Bot

A free, paper-only rebuild of ProcessOverProfit's [Kalshi AI trading bot](https://code.processoverprofit.blog/docs/youtube-code/video-15-kalshi-ai-trading-bot/).
The original needs the paid Nightshark app and its AI backend. This version is one Python file:

| Piece | Original | This repo |
|---|---|---|
| Runtime | Nightshark (AutoHotkey) | Python 3 + `requests` |
| Kalshi market data | Kalshi public API | Kalshi public API (no key) |
| Spot price context | Nightshark feed | Coinbase public candles (no key) |
| AI decision | Nightshark AI Mode (paid) | `claude -p` from Claude Code, on your Claude subscription (**no API key**) |
| Trading | paper or live | **paper only**: no order code exists |

## How it works

Every 15 minutes Kalshi opens a market per coin: *will the price at close be at or above the price at open?*
When `time_delay` minutes remain, the bot sends Claude the strike, the Kalshi bid/ask for each side, recent 1-minute
price moves, volatility, and a random-walk baseline probability. Claude returns `UP`, `DOWN`, or `SKIP` with its own
probability. The bot then:

1. re-quotes the market (Claude takes a few seconds),
2. simulates buying `order_size` contracts by walking the real order book, and charges Kalshi's taker fee,
3. holds to resolution and books the $1-or-$0 payout from Kalshi's official result,
4. stops opening trades for the day once settled PnL hits `max_daily_profit` or `max_daily_loss`.

It also logs whether Claude's call was right on **every** decision, including skips, so you can judge the model before
trusting it with anything.

## Setup (Windows)

```bash
pip install requests
```

Log the Claude Code CLI into your Claude account once. The Claude desktop app ships the CLI at
`%APPDATA%\Claude\claude-code\<version>\claude.exe` (Microsoft Store installs:
`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code\<version>\claude.exe`), and the bot finds it automatically. Run it, type `/login`, and pick
your Claude subscription (not an API key). If you use a different copy, set `CLAUDE_BIN` to its path.

## Use

```bash
python bot.py test      # ask Claude about the live BTC market once, no trade
python bot.py           # run the paper bot (Ctrl+C to stop; restart-safe)
python bot.py report    # PnL, win rate, Claude's hit rate, Brier score
```

Settings live in `config.json`:

| Key | Meaning |
|---|---|
| `assets` | any of BTC, ETH, SOL, XRP, DOGE, HYPE, BNB |
| `order_size` | simulated contracts per trade (each pays $1) |
| `time_delay` | minutes left in the session when Claude is asked (15 = at open) |
| `obey_model` | `false` trades the opposite of Claude's call |
| `max_daily_profit` / `max_daily_loss` | stop new trades after settled PnL crosses these; 0 disables |
| `model` | `haiku`, `sonnet`, or `opus` (haiku uses the least of your plan's limits) |
| `simulate_fees`, `walk_order_book` | realism switches for paper fills |

Edit `instructions.md` to change Claude's strategy notes (the equivalent of Nightshark's AI Mode instructions).

Logs go to `data/`: `trades.jsonl` (opens and settlements), `decisions.jsonl` (every decision plus its outcome), `state.json`.

## Honest expectations

These markets are priced by fast professional traders, and a 15-minute crypto move is close to random. Expect the
paper results to hover around zero minus fees. The `report` Brier score tells you whether Claude's probabilities beat a
coin flip (0.250); if they don't after a few hundred decisions, the strategy has no edge.

Kalshi accounts require users to be 18+ and identity-verified. This project is for learning and paper trading.
