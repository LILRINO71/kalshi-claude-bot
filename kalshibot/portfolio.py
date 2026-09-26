"""Paper account shared by backtests and live trading."""
import math
from datetime import datetime


def kalshi_fee(price, contracts):
    """Kalshi taker fee: 7% x contracts x P x (1 - P), rounded up to the cent."""
    if contracts <= 0:
        return 0.0
    return math.ceil(0.07 * contracts * price * (1 - price) * 100 - 1e-9) / 100


def local_date(ts):
    return datetime.fromtimestamp(ts).date().isoformat()


class Account:
    def __init__(self, bankroll, state=None):
        self.bankroll = float(bankroll)
        self.cash = float(bankroll)
        self.positions = {}     # ticker -> open position
        self.closed = []        # settled trades
        self.equity_curve = []  # [ts, equity] after each settlement
        if state:
            self.__dict__.update(state)

    def to_state(self):
        return {"bankroll": self.bankroll, "cash": self.cash, "positions": self.positions,
                "closed": self.closed, "equity_curve": self.equity_curve}

    # ------------------------------------------------------------ valuation

    def equity(self):
        """Cash plus open positions at cost (they settle to $0 or $1, so no mark-to-market)."""
        return self.cash + sum(p["cost"] + p["fees"] for p in self.positions.values())

    def realized_pnl(self):
        return sum(t["pnl"] for t in self.closed)

    def day_pnl(self, day):
        return sum(t["pnl"] for t in self.closed if t["local_date"] == day)

    def daily_block(self, ts, cfg):
        day = local_date(ts)
        pnl = self.day_pnl(day)
        hi, lo = cfg["max_daily_profit"], cfg["max_daily_loss"]
        if hi > 0 and pnl >= hi:
            return f"daily profit target reached ({pnl:+.2f})"
        if lo < 0 and pnl <= lo:
            return f"daily loss limit reached ({pnl:+.2f})"
        return None

    # ------------------------------------------------------------ sizing

    def stake_for(self, cfg, price, p_side):
        """Dollars to put on a trade, or (0, reason)."""
        eq = self.equity()
        mode = cfg["sizing"]
        if mode == "fixed":
            stake = cfg["fixed_stake"]
        elif mode == "percent":
            stake = eq * cfg["percent_stake"] / 100
        else:
            # Binary contract bought at `price` paying $1: Kelly fraction = (p - price) / (1 - price).
            if p_side is None:
                return 0.0, "no probability for Kelly sizing"
            f = (p_side - price) / (1 - price)
            if f <= 0:
                return 0.0, f"model probability {p_side:.2f} does not beat price {price:.2f}"
            stake = eq * cfg["kelly_fraction"] * f
        stake = min(stake, eq * cfg["max_stake_pct"] / 100, self.cash)
        if stake < price:
            return 0.0, "stake too small for one contract"
        return stake, ""

    def contracts_for(self, stake, price, simulate_fees):
        n = int(stake // price)
        while n > 0:
            fees = kalshi_fee(price, n) if simulate_fees else 0.0
            if n * price + fees <= stake + 1e-9:
                return n, fees
            n -= 1
        return 0, 0.0

    # ------------------------------------------------------------ lifecycle

    def open(self, ticker, asset, side, price, contracts, fees, close_ts, opened_ts, meta=None):
        cost = round(price * contracts, 4)
        self.cash -= cost + fees
        pos = {"ticker": ticker, "asset": asset, "side": side, "price": round(price, 4),
               "contracts": contracts, "cost": cost, "fees": fees, "close_ts": close_ts,
               "opened_ts": opened_ts, **(meta or {})}
        self.positions[ticker] = pos
        return pos

    def settle(self, ticker, result, settled_ts):
        """result is 'yes' or 'no'. Returns the closed trade."""
        pos = self.positions.pop(ticker)
        won = (result == "yes") == (pos["side"] == "UP")
        payout = float(pos["contracts"]) if won else 0.0
        pnl = payout - pos["cost"] - pos["fees"]
        self.cash += payout
        trade = {**pos, "result": result, "won": won, "payout": payout, "pnl": round(pnl, 4),
                 "settled_ts": settled_ts, "local_date": local_date(settled_ts)}
        self.closed.append(trade)
        self.equity_curve.append([settled_ts, round(self.equity(), 4)])
        return trade
