"""What a strategy sees at decision time. Built the same way live and in backtests."""
import math
from dataclasses import dataclass, field

from .config import LABELS, is_index


@dataclass
class Snapshot:
    asset: str
    ticker: str
    close_ts: int
    decision_ts: int
    strike: float
    up_ask: float
    up_bid: float
    down_ask: float
    down_bid: float
    closes: list                      # 1-minute spot closes, oldest first, ending at decision time
    session_closes: list = field(default_factory=list)  # closes since the session opened
    features: dict = field(default_factory=dict)

    @property
    def mins_left(self):
        return (self.close_ts - self.decision_ts) / 60

    @property
    def spot(self):
        return self.closes[-1]

    @property
    def market_prob_up(self):
        """Mid-price probability the market itself assigns to UP."""
        if self.up_ask is None or self.up_bid is None:
            return None
        return (self.up_ask + self.up_bid) / 2


def build(asset, ticker, close_ts, decision_ts, strike, q, candles):
    """Return a Snapshot, or None if the data is too thin to decide on."""
    # Stock indices only trade 9:30-4:00, so the first hour has a short history.
    min_candles = 10 if is_index(asset) else 30
    if strike is None or not q or q.get("up_ask") is None or len(candles) < min_candles:
        return None
    closes = [c[4] for c in candles]
    open_ts = close_ts - (60 if is_index(asset) else 15) * 60
    session = [c[4] for c in candles if c[0] >= open_ts]
    snap = Snapshot(asset=asset, ticker=ticker, close_ts=close_ts, decision_ts=decision_ts,
                    strike=strike, closes=closes, session_closes=session, **q)
    snap.features = compute_features(snap)
    return snap


def compute_features(s):
    closes, price = s.closes, s.closes[-1]

    def pct(n):
        return (price / closes[-1 - n] - 1) * 100 if len(closes) > n else None

    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    recent = rets[-60:]
    mean = sum(recent) / len(recent)
    vol = math.sqrt(sum((r - mean) ** 2 for r in recent) / max(1, len(recent) - 1))
    mins = max(s.mins_left, 1e-6)
    z = math.log(price / s.strike) / (vol * math.sqrt(mins)) if vol > 0 else 0.0
    baseline = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return {
        "change_1m": pct(1), "change_5m": pct(5), "change_15m": pct(15), "change_60m": pct(60),
        "vol_1m": vol * 100,
        "dist_pct": (price / s.strike - 1) * 100,
        "z": z,
        "baseline_up": baseline,
        "market_up": s.market_prob_up,
    }


def prompt(s, instructions):
    """The decision prompt. No dates or tickers, so a backtest can't lean on remembered prices."""
    if is_index(s.asset):
        return index_prompt(s, instructions)
    f = s.features

    def n(x, d=3):
        return "n/a" if x is None else f"{x:.{d}f}"

    closes = ", ".join(f"{c:g}" for c in s.closes[-15:])
    return f"""Kalshi 15-minute {s.asset} market.
Question: will {s.asset}'s 60-second average price at close be at or above the strike?
Strike (average price at session open): {s.strike:g}
Minutes until close: {s.mins_left:.1f}

KALSHI PRICES (a contract pays $1 if right; price = implied probability)
UP   ask {n(s.up_ask, 2)}  bid {n(s.up_bid, 2)}
DOWN ask {n(s.down_ask, 2)}  bid {n(s.down_bid, 2)}

{s.asset}-USD SPOT
Price {s.spot:g} ({n(f['dist_pct'])}% vs strike, {n(f['z'], 2)} sigma to close)
Change: 1m {n(f['change_1m'])}% | 5m {n(f['change_5m'])}% | 15m {n(f['change_15m'])}% | 60m {n(f['change_60m'])}%
1-minute volatility {n(f['vol_1m'], 4)}%
Last 15 one-minute closes, oldest first: {closes}
Random-walk baseline P(UP): {n(f['baseline_up'])}

RULES
- Buying UP costs the UP ask, buying DOWN costs the DOWN ask; fees add about 1-2 cents per contract near 50c.
- Pick UP or DOWN only if your probability for that side beats its ask by a clear margin after fees; otherwise SKIP.
- The market price is a strong prior set by fast traders; you need a concrete reason to disagree with it.
- probability_up is your honest estimate that the market resolves UP, even when you SKIP.

STRATEGY NOTES
{instructions.strip() or '(none)'}"""


def index_prompt(s, instructions):
    f = s.features
    name = LABELS[s.asset]

    def n(x, d=3):
        return "n/a" if x is None else f"{x:.{d}f}"

    closes = ", ".join(f"{c:.2f}" for c in s.closes[-15:])
    return f"""Kalshi hourly {name} market (one strike from a ladder of strikes for this hour).
Question: will the {name} index be above {s.strike:g} at the top of the hour when this market closes?
Strike: {s.strike:g} (a fixed level, chosen as the strike nearest the index at decision time)
Minutes until close: {s.mins_left:.1f}

KALSHI PRICES (a contract pays $1 if right; price = implied probability)
UP   ask {n(s.up_ask, 2)}  bid {n(s.up_bid, 2)}
DOWN ask {n(s.down_ask, 2)}  bid {n(s.down_bid, 2)}

{name} INDEX
Level {s.spot:.2f} ({n(f['dist_pct'])}% vs strike, {n(f['z'], 2)} sigma to close)
Change: 1m {n(f['change_1m'])}% | 5m {n(f['change_5m'])}% | 15m {n(f['change_15m'])}% | 60m {n(f['change_60m'])}%
1-minute volatility {n(f['vol_1m'], 4)}%
Last 15 one-minute closes, oldest first: {closes}
Random-walk baseline P(UP): {n(f['baseline_up'])}

RULES
- Buying UP costs the UP ask, buying DOWN costs the DOWN ask; fees add about 1-2 cents per contract near 50c.
- Pick UP or DOWN only if your probability for that side beats its ask by a clear margin after fees; otherwise SKIP.
- The market price is a strong prior set by fast traders; you need a concrete reason to disagree with it.
- probability_up is your honest estimate that the market resolves UP, even when you SKIP.

STRATEGY NOTES
{instructions.strip() or '(none)'}"""
