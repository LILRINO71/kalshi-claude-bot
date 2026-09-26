#!/usr/bin/env python3
"""Kalshi 15-minute crypto paper-trading bot.

Free rebuild of ProcessOverProfit's "Kalshi AI trading bot" (video 15).
Market data comes from Kalshi's public API and Coinbase's public candles,
and the UP / DOWN / SKIP decision comes from Claude through the Claude Code
CLI (`claude -p`), so it runs on a normal Claude subscription with no API key.

Paper mode only: nothing here can place a real order.
"""
import argparse
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE = "https://api.exchange.coinbase.com"
SERIES = {
    "BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M",
    "DOGE": "KXDOGE15M", "HYPE": "KXHYPE15M", "BNB": "KXBNB15M",
}

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_FILE = DATA / "state.json"
TRADES_FILE = DATA / "trades.jsonl"
DECISIONS_FILE = DATA / "decisions.jsonl"

DEFAULT_CONFIG = {
    "assets": ["BTC"],
    "order_size": 100,          # simulated contracts per trade ($1 payout each)
    "time_delay": 10,           # ask Claude when this many minutes remain (15 = session start)
    "obey_model": True,         # False trades the opposite of Claude's call
    "max_daily_profit": 400,    # stop new trades once today's settled PnL >= this; 0 disables
    "max_daily_loss": -200,     # stop new trades once today's settled PnL <= this; 0 disables
    "model": "sonnet",          # claude CLI model alias: haiku / sonnet / opus
    "simulate_fees": True,      # charge Kalshi's taker fee on paper fills
    "walk_order_book": True,    # fill against real book depth instead of top-of-book only
    "poll_seconds": 3,
}

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["UP", "DOWN", "SKIP"]},
        "probability_up": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["direction", "probability_up", "reason"],
}

session = requests.Session()
session.headers["User-Agent"] = "kalshi-claude-paper-bot/1.0"


# ---------------------------------------------------------------- utilities

def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')}  {msg}", flush=True)


def utc_now():
    return datetime.now(timezone.utc)


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fnum(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def get_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
        except requests.RequestException:
            pass
        time.sleep(1 + attempt)
    return None


def append_jsonl(path, row):
    DATA.mkdir(exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    cfg["assets"] = [a.strip().upper() for a in cfg["assets"]]
    bad = [a for a in cfg["assets"] if a not in SERIES]
    if bad:
        sys.exit(f"Unsupported asset(s) {bad}. Choose from {list(SERIES)}")
    if not 0 < float(cfg["time_delay"]) <= 15:
        sys.exit("time_delay must be > 0 and <= 15")
    if float(cfg["order_size"]) <= 0:
        sys.exit("order_size must be positive")
    if float(cfg["max_daily_profit"]) < 0 or float(cfg["max_daily_loss"]) > 0:
        sys.exit("max_daily_profit must be >= 0 and max_daily_loss must be <= 0")
    return cfg


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"positions": {}, "decided": {}}


def save_state(state):
    DATA.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- market data

def open_market(asset):
    """The currently trading 15-minute market for an asset, or None."""
    data = get_json(f"{KALSHI}/markets",
                    {"series_ticker": SERIES[asset], "status": "open", "limit": 5})
    if not data or not data.get("markets"):
        return None
    now = utc_now()
    live = [m for m in data["markets"] if parse_iso(m["close_time"]) > now]
    if not live:
        return None
    return min(live, key=lambda m: m["close_time"])


def market_by_ticker(ticker):
    data = get_json(f"{KALSHI}/markets/{ticker}")
    return data.get("market") if data else None


def order_book(ticker, depth=20):
    """Asks for each side as [(price, size)] cheapest first.

    Kalshi only publishes bids: a YES ask is the other side of a NO bid at 1 - price.
    """
    data = get_json(f"{KALSHI}/markets/{ticker}/orderbook", {"depth": depth})
    if not data:
        return None
    book = data.get("orderbook_fp") or {}
    yes_bids = [(fnum(p), fnum(s)) for p, s in book.get("yes_dollars") or []]
    no_bids = [(fnum(p), fnum(s)) for p, s in book.get("no_dollars") or []]
    up_asks = sorted((round(1 - p, 4), s) for p, s in no_bids)
    down_asks = sorted((round(1 - p, 4), s) for p, s in yes_bids)
    return {"UP": up_asks, "DOWN": down_asks}


def quote(market):
    return {
        "up_ask": fnum(market.get("yes_ask_dollars")),
        "up_bid": fnum(market.get("yes_bid_dollars")),
        "down_ask": fnum(market.get("no_ask_dollars")),
        "down_bid": fnum(market.get("no_bid_dollars")),
    }


def candles(asset, minutes=90):
    """Recent 1-minute candles from Coinbase, oldest first: (time, low, high, open, close, volume)."""
    data = get_json(f"{COINBASE}/products/{asset}-USD/candles", {"granularity": 60})
    if not isinstance(data, list) or not data:
        return []
    return sorted(data, key=lambda c: c[0])[-minutes:]


def price_context(asset, strike, mins_left):
    """Summary numbers for the prompt, plus a simple random-walk baseline probability."""
    cs = candles(asset)
    if len(cs) < 20:
        return None
    closes = [c[4] for c in cs]
    price = closes[-1]

    def pct_change(n):
        return (price / closes[-1 - n] - 1) * 100 if len(closes) > n else None

    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    recent = rets[-60:]
    mean = sum(recent) / len(recent)
    vol_1m = math.sqrt(sum((r - mean) ** 2 for r in recent) / max(1, len(recent) - 1))

    baseline_up = None
    if strike and vol_1m > 0 and mins_left > 0:
        # Driftless random walk: chance the price is still above the strike at close.
        z = math.log(price / strike) / (vol_1m * math.sqrt(mins_left))
        baseline_up = 0.5 * (1 + math.erf(z / math.sqrt(2)))

    return {
        "price": price,
        "change_1m_pct": pct_change(1),
        "change_5m_pct": pct_change(5),
        "change_15m_pct": pct_change(15),
        "change_60m_pct": pct_change(60),
        "vol_1m_pct": vol_1m * 100,
        "baseline_prob_up": baseline_up,
        "last_15_closes": closes[-15:],
    }


# ---------------------------------------------------------------- fills and PnL

def kalshi_fee(price, contracts):
    """Kalshi taker fee: 7% * C * P * (1 - P), rounded up to the cent."""
    return math.ceil(0.07 * contracts * price * (1 - price) * 100 - 1e-9) / 100


def simulate_fill(ticker, side, size, top_ask, cfg):
    """Average price for buying `size` contracts; None if the book can't cover it."""
    if not cfg["walk_order_book"]:
        return top_ask
    book = order_book(ticker)
    if not book or not book[side]:
        return top_ask
    remaining, cost = size, 0.0
    for price, avail in book[side]:
        take = min(remaining, avail)
        cost += take * price
        remaining -= take
        if remaining <= 1e-9:
            return cost / size
    return None


def todays_pnl():
    today = datetime.now().date().isoformat()
    return sum(t["pnl"] for t in read_jsonl(TRADES_FILE)
               if t.get("event") == "settle" and t.get("local_date") == today)


def daily_limit_hit(cfg):
    pnl = todays_pnl()
    hi, lo = float(cfg["max_daily_profit"]), float(cfg["max_daily_loss"])
    if hi > 0 and pnl >= hi:
        return f"daily profit target hit ({pnl:+.2f})"
    if lo < 0 and pnl <= lo:
        return f"daily loss limit hit ({pnl:+.2f})"
    return None


# ---------------------------------------------------------------- Claude

def find_claude():
    path = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if path:
        return path
    # Claude desktop app bundles the CLI here on Windows.
    appdata = os.environ.get("APPDATA", "")
    found = glob.glob(os.path.join(appdata, "Claude", "claude-code", "*", "claude.exe"))
    if found:
        def version_key(p):
            parts = Path(p).parent.name.split(".")
            return [int(x) if x.isdigit() else 0 for x in parts]
        return max(found, key=version_key)
    return None


def build_prompt(asset, market, q, ctx, mins_left, instructions):
    strike = fnum(market.get("floor_strike"))

    def f(x, nd=4):
        return "n/a" if x is None else f"{x:.{nd}f}"

    closes = ", ".join(f"{c:g}" for c in ctx["last_15_closes"])
    return f"""You are the decision engine for a PAPER-trading bot on Kalshi's 15-minute crypto markets.

MARKET: {market['ticker']}
Question: will {asset}'s 60-second average price at close be at or above the strike?
Strike (average price at session open): {strike}
Minutes until close: {mins_left:.2f}

KALSHI PRICES (each contract pays $1 if right; price = implied probability)
UP (yes)  ask {f(q['up_ask'], 2)}  bid {f(q['up_bid'], 2)}
DOWN (no) ask {f(q['down_ask'], 2)}  bid {f(q['down_bid'], 2)}

{asset}-USD SPOT (Coinbase)
Current price: {ctx['price']:g}  ({f((ctx['price'] / strike - 1) * 100 if strike else None, 3)}% vs strike)
Change: 1m {f(ctx['change_1m_pct'], 3)}% | 5m {f(ctx['change_5m_pct'], 3)}% | 15m {f(ctx['change_15m_pct'], 3)}% | 60m {f(ctx['change_60m_pct'], 3)}%
1-minute volatility: {f(ctx['vol_1m_pct'], 4)}%
Last 15 one-minute closes (oldest to newest): {closes}
Random-walk baseline P(UP): {f(ctx['baseline_prob_up'], 3)}

RULES
- Buying UP costs the UP ask; buying DOWN costs the DOWN ask. Taker fees add roughly 1-2 cents per contract near 50c.
- Only choose UP or DOWN if your probability for that side beats its ask by a clear margin after fees. Otherwise SKIP.
- The market price already reflects other traders; treat it as a strong prior, not something to ignore.
- probability_up is your own estimate that the market resolves UP.

{instructions}

Respond with the structured decision only."""


def ask_claude(prompt, cfg):
    claude = find_claude()
    if not claude:
        raise RuntimeError("Claude Code CLI not found. Install it or set CLAUDE_BIN.")
    cmd = [claude, "-p", "--model", cfg["model"], "--output-format", "json",
           "--json-schema", json.dumps(DECISION_SCHEMA),
           "--tools", "", "--no-session-persistence"]
    DATA.mkdir(exist_ok=True)
    # Run from data/ so no project CLAUDE.md or settings leak into the decision.
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          encoding="utf-8", timeout=240, cwd=DATA)
    out = (proc.stdout or "").strip()
    if "Not logged in" in out or "Not logged in" in (proc.stderr or ""):
        raise RuntimeError("Claude CLI is not logged in. Run `claude` once in a terminal and use /login.")
    try:
        envelope = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"Unreadable Claude output: {out[:300]} {proc.stderr[:300]}")
    if envelope.get("is_error"):
        raise RuntimeError(f"Claude error: {envelope.get('result') or envelope.get('terminal_reason')}")

    decision = envelope.get("structured_output")
    if not isinstance(decision, dict):
        text = envelope.get("result", "")
        start, end = text.find("{"), text.rfind("}")
        decision = json.loads(text[start:end + 1]) if start >= 0 else None
    if not isinstance(decision, dict) or decision.get("direction") not in ("UP", "DOWN", "SKIP"):
        raise RuntimeError(f"Invalid decision from Claude: {envelope.get('result', '')[:300]}")
    decision["cost_usd"] = envelope.get("total_cost_usd")
    return decision


def load_instructions():
    path = ROOT / "instructions.md"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


# ---------------------------------------------------------------- strategy

def decide_and_enter(asset, market, cfg, state):
    ticker = market["ticker"]
    close = parse_iso(market["close_time"])
    mins_left = (close - utc_now()).total_seconds() / 60
    strike = fnum(market.get("floor_strike"))
    if strike is None:
        return  # strike not published yet; try again next poll

    ctx = price_context(asset, strike, mins_left)
    if not ctx:
        log(f"{asset} no Coinbase candles yet, retrying")
        return

    q = quote(market)
    prompt = build_prompt(asset, market, q, ctx, mins_left, load_instructions())
    log(f"{asset} asking Claude ({cfg['model']}) with {mins_left:.1f} min left | spot {ctx['price']:g} vs strike {strike:g}")
    t0 = time.time()
    try:
        decision = ask_claude(prompt, cfg)
    except Exception as e:
        log(f"{asset} Claude failed: {e} -- skipping this session")
        state["decided"][ticker] = {"direction": "ERROR", "reason": str(e)[:200],
                                    "close_time": market["close_time"]}
        return
    log(f"{asset} Claude says {decision['direction']} (P(up)={decision['probability_up']:.2f}, {time.time() - t0:.0f}s): {decision['reason']}")

    record = {
        "event": "decision", "time": utc_now().isoformat(), "asset": asset, "ticker": ticker,
        "close_time": market["close_time"],
        "model": cfg["model"], "direction": decision["direction"],
        "probability_up": decision["probability_up"], "reason": decision["reason"],
        "mins_left": round(mins_left, 2), "spot": ctx["price"], "strike": strike,
        "baseline_prob_up": ctx["baseline_prob_up"], **q,
    }
    state["decided"][ticker] = record

    if decision["direction"] == "SKIP":
        append_jsonl(DECISIONS_FILE, record)
        return

    side = decision["direction"] if cfg["obey_model"] else ("DOWN" if decision["direction"] == "UP" else "UP")

    # Claude can take a while, so re-quote before filling.
    fresh = market_by_ticker(ticker)
    if not fresh or fresh.get("status") not in ("active", "open"):
        log(f"{asset} market closed before entry; no paper trade")
        append_jsonl(DECISIONS_FILE, record)
        return
    top_ask = quote(fresh)["up_ask" if side == "UP" else "down_ask"]
    if not top_ask or not 0 < top_ask < 1:
        log(f"{asset} no {side} ask available; no paper trade")
        append_jsonl(DECISIONS_FILE, record)
        return

    size = float(cfg["order_size"])
    fill = simulate_fill(ticker, side, size, top_ask, cfg)
    if fill is None:
        log(f"{asset} order book too thin for {size:g} contracts; no paper trade")
        append_jsonl(DECISIONS_FILE, record)
        return
    fees = kalshi_fee(fill, size) if cfg["simulate_fees"] else 0.0

    record["trade_side"] = side
    append_jsonl(DECISIONS_FILE, record)
    pos = {"asset": asset, "ticker": ticker, "side": side, "entry": round(fill, 4), "size": size,
           "fees": fees, "close_time": market["close_time"], "ai_direction": decision["direction"],
           "opened": utc_now().isoformat()}
    state["positions"][ticker] = pos
    append_jsonl(TRADES_FILE, {"event": "open", **pos})
    log(f"{asset} PAPER BUY {side} x{size:g} @ {fill:.4f} (top ask {top_ask:.2f}) | cost ${fill * size:.2f} + fee ${fees:.2f}")


def settle_positions(state):
    now = utc_now()
    for ticker, pos in list(state["positions"].items()):
        if parse_iso(pos["close_time"]) > now:
            continue
        m = market_by_ticker(ticker)
        result = (m or {}).get("result", "").lower()
        if result not in ("yes", "no"):
            continue  # Kalshi usually settles a few minutes after close
        won = (result == "yes") == (pos["side"] == "UP")
        payout = 1.0 if won else 0.0
        pnl = (payout - pos["entry"]) * pos["size"] - pos["fees"]
        append_jsonl(TRADES_FILE, {
            "event": "settle", "ticker": ticker, "asset": pos["asset"], "side": pos["side"],
            "result": result, "won": won, "entry": pos["entry"], "size": pos["size"],
            "fees": pos["fees"], "pnl": round(pnl, 4), "settled": now.isoformat(),
            "local_date": datetime.now().date().isoformat(),
        })
        del state["positions"][ticker]
        log(f"{pos['asset']} SETTLED {ticker}: market {result.upper()} | {pos['side']} {'WON' if won else 'LOST'} | PnL {pnl:+.2f} | today {todays_pnl():+.2f}")


def audit_decisions(state):
    """Once a decided market resolves, record whether Claude's call was right (even for SKIPs)."""
    audited = {r["ticker"] for r in read_jsonl(DECISIONS_FILE) if r.get("event") == "outcome"}
    now = utc_now()
    for ticker, rec in list(state["decided"].items()):
        # Keep the entry until the market closes so the loop never re-asks Claude for it.
        if parse_iso(rec["close_time"]) > now:
            continue
        if ticker in audited or rec.get("direction") == "ERROR":
            state["decided"].pop(ticker, None)
            continue
        m = market_by_ticker(ticker)
        if not m:
            continue
        result = (m.get("result") or "").lower()
        if result not in ("yes", "no"):
            continue
        actual = "UP" if result == "yes" else "DOWN"
        append_jsonl(DECISIONS_FILE, {
            "event": "outcome", "ticker": ticker, "asset": rec.get("asset"),
            "predicted": rec.get("direction"), "probability_up": rec.get("probability_up"),
            "actual": actual, "match": rec.get("direction") == actual,
        })
        state["decided"].pop(ticker, None)


def run(cfg):
    claude = find_claude()
    if not claude:
        sys.exit("Claude Code CLI not found. Install it or set CLAUDE_BIN to claude.exe")
    state = load_state()
    log(f"PAPER MODE | assets {cfg['assets']} | size {cfg['order_size']} | ask at {cfg['time_delay']} min left | "
        f"model {cfg['model']} | obey {cfg['obey_model']} | limits +{cfg['max_daily_profit']}/{cfg['max_daily_loss']}")
    log(f"Claude CLI: {claude}")
    waiting_logged = set()
    last_audit = 0
    while True:
        try:
            settle_positions(state)
            if time.time() - last_audit > 30:
                audit_decisions(state)
                last_audit = time.time()
            stop_reason = daily_limit_hit(cfg)

            for asset in cfg["assets"]:
                m = open_market(asset)
                if not m:
                    continue
                ticker = m["ticker"]
                if ticker in state["decided"] or ticker in state["positions"]:
                    continue
                mins_left = (parse_iso(m["close_time"]) - utc_now()).total_seconds() / 60
                if mins_left > float(cfg["time_delay"]):
                    if ticker not in waiting_logged:
                        waiting_logged.add(ticker)
                        log(f"{asset} new session {ticker} | waiting until {cfg['time_delay']} min left ({mins_left:.1f} now)")
                    continue
                if stop_reason:
                    if f"stop:{ticker}" not in waiting_logged:
                        waiting_logged.add(f"stop:{ticker}")
                        log(f"{asset} no new trades: {stop_reason}")
                    continue
                if mins_left < 0.5:
                    continue  # too late to act on a decision
                decide_and_enter(asset, m, cfg, state)
            save_state(state)
        except KeyboardInterrupt:
            save_state(state)
            log("Stopped.")
            return
        except Exception as e:
            log(f"loop error: {e!r}")
        time.sleep(cfg["poll_seconds"])


def report():
    trades = [t for t in read_jsonl(TRADES_FILE) if t.get("event") == "settle"]
    outcomes = [d for d in read_jsonl(DECISIONS_FILE) if d.get("event") == "outcome"]
    state = load_state()
    print("=== Paper trading report ===")
    if trades:
        pnl = sum(t["pnl"] for t in trades)
        wins = sum(t["won"] for t in trades)
        staked = sum(t["entry"] * t["size"] for t in trades)
        fees = sum(t["fees"] for t in trades)
        print(f"Settled trades: {len(trades)} | wins {wins} ({wins / len(trades):.0%})")
        print(f"Net PnL: ${pnl:+.2f} | staked ${staked:.2f} | return {pnl / staked:+.1%} | fees ${fees:.2f}")
        by_day = {}
        for t in trades:
            by_day[t["local_date"]] = by_day.get(t["local_date"], 0) + t["pnl"]
        for day, p in sorted(by_day.items()):
            print(f"  {day}: {p:+.2f}")
    else:
        print("No settled trades yet.")
    calls = [o for o in outcomes if o["predicted"] in ("UP", "DOWN")]
    skips = [o for o in outcomes if o["predicted"] == "SKIP"]
    if calls:
        hits = sum(o["match"] for o in calls)
        print(f"Claude direction calls: {len(calls)} | correct {hits} ({hits / len(calls):.0%})")
    if skips:
        print(f"Skipped sessions: {len(skips)}")
    scored = [o for o in outcomes if o.get("probability_up") is not None]
    if scored:
        brier = sum((o["probability_up"] - (o["actual"] == "UP")) ** 2 for o in scored) / len(scored)
        print(f"Brier score of P(up) over {len(scored)} decisions: {brier:.3f} (0.250 = coin flip, lower is better)")
    if state["positions"]:
        print(f"Open positions: {', '.join(state['positions'])}")


def test_decision(cfg, asset):
    """Ask Claude about the current market right now, without trading."""
    m = open_market(asset)
    if not m:
        sys.exit(f"No open {asset} market right now.")
    mins_left = (parse_iso(m["close_time"]) - utc_now()).total_seconds() / 60
    strike = fnum(m.get("floor_strike"))
    ctx = price_context(asset, strike, mins_left)
    prompt = build_prompt(asset, m, quote(m), ctx, mins_left, load_instructions())
    print(prompt)
    print("\n--- asking Claude ---")
    t0 = time.time()
    print(json.dumps(ask_claude(prompt, cfg), indent=2))
    print(f"({time.time() - t0:.0f}s)")


def main():
    ap = argparse.ArgumentParser(description="Kalshi 15-minute crypto paper-trading bot driven by Claude Code.")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "report", "test"])
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--asset", default=None, help="asset for `test` (defaults to the first configured asset)")
    ap.add_argument("--model", default=None, help="override the model from config.json")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config(Path(args.config))
    if args.model:
        cfg["model"] = args.model

    if args.command == "report":
        report()
    elif args.command == "test":
        test_decision(cfg, (args.asset or cfg["assets"][0]).upper())
    else:
        run(cfg)


if __name__ == "__main__":
    main()
