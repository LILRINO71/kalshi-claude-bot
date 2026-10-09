"""Paper broker for intraday stock trading. Never places a real order.

Bars are tuples (t, open, high, low, close, volume) where t is the bar's start time. The broker
only ever sees a bar after it has finished, and resolves every ambiguity against itself:

- an order placed at time T is only eligible from the first bar that starts at or after T;
- stop entries fill at the trigger or the open if the bar gapped past it, plus slippage;
- limit orders need the price to trade through the limit by a tick;
- if a bar touches both the stop and the target, the stop is assumed to have come first;
- on the bar a position is opened, the stop can be hit but the target is not credited;
- a gap through the stop fills at the open, not at the stop.
"""
import itertools
import math

TICK = 0.01
_ids = itertools.count(1)


def _r2(x):
    return round(x, 4)


class Broker:
    def __init__(self, cfg, equity, day_start_ts=0):
        self.cfg = cfg
        self.start_equity = float(equity)
        self.realized = 0.0
        self.orders = []        # working and finished entry orders (dicts)
        self.positions = {}     # ticker -> position dict
        self.trades = []        # closed trades
        self.events = []        # human-readable log rows
        self.last_price = {}    # ticker -> last close seen
        self.blocked = ""       # reason new entries are refused (daily loss stop)
        self.pending_exits = {}  # ticker -> earliest ts for a market exit
        self.day_start_ts = day_start_ts

    # ------------------------------------------------------------ accounting

    def slip(self, price):
        return max(TICK, price * self.cfg["slippage_bps"] / 1e4)

    def commission(self, shares):
        if shares <= 0:
            return 0.0
        return max(self.cfg["commission_min"], shares * self.cfg["commission_per_share"])

    def unrealized(self):
        out = 0.0
        for tk, p in self.positions.items():
            px = self.last_price.get(tk, p["entry"])
            out += (px - p["entry"]) * p["shares"] * (1 if p["side"] == "long" else -1)
        return out

    def equity(self):
        return self.start_equity + self.realized + self.unrealized()

    def gross_exposure(self):
        return sum(self.last_price.get(tk, p["entry"]) * p["shares"] for tk, p in self.positions.items())

    def log(self, ts, msg, kind="info"):
        self.events.append({"t": ts, "msg": msg, "kind": kind})

    # ------------------------------------------------------------ orders

    def submit(self, order, ts):
        """Queue an entry order. Returns (order, error)."""
        o = {
            "id": f"o{next(_ids)}", "ticker": order["ticker"].upper(), "side": order["side"],
            "kind": order.get("kind", "stop"), "price": order.get("price"), "stop": order.get("stop"),
            "target": order.get("target"), "active_from": max(ts, order.get("active_from") or ts),
            "expires": order.get("expires"), "time_exit": order.get("time_exit"),
            "breakeven_at": order.get("breakeven_at"), "size": min(1.0, max(0.25, float(order.get("size") or 1))),
            "plan_id": order.get("plan_id"), "rules": list(order.get("rules") or []),
            "setup": order.get("setup", ""), "confidence": order.get("confidence"), "status": "working",
            "placed": ts, "reason": "",
        }
        err = self._validate(o)
        if err:
            o["status"], o["reason"] = "rejected", err
            self.orders.append(o)
            self.log(ts, f"{o['ticker']} order rejected: {err}", "warn")
            return o, err
        # One working entry per ticker: a new plan replaces the old one.
        for old in self.working(o["ticker"]):
            old["status"], old["reason"] = "replaced", f"replaced by {o['id']}"
        self.orders.append(o)
        what = "market" if o["kind"] == "market" else f"{o['kind']} {o['price']:.2f}"
        self.log(ts, f"{o['ticker']} {o['side']} {what}, stop {o['stop']:.2f}"
                     + (f", target {o['target']:.2f}" if o["target"] else ""), "order")
        return o, None

    def _validate(self, o):
        if o["side"] not in ("long", "short"):
            return "side must be long or short"
        if o["side"] == "short" and not self.cfg["allow_short"]:
            return "shorting is disabled"
        if o["kind"] not in ("market", "stop", "limit"):
            return "kind must be market, stop or limit"
        if o["kind"] != "market" and not (o["price"] and o["price"] > 0):
            return "stop and limit orders need a price"
        if not (o["stop"] and o["stop"] > 0):
            return "every entry needs a protective stop"
        ref = o["price"] if o["kind"] != "market" else None
        if ref:
            if o["side"] == "long" and o["stop"] >= ref:
                return "long stop must be below the entry"
            if o["side"] == "short" and o["stop"] <= ref:
                return "short stop must be above the entry"
            if o["target"]:
                if o["side"] == "long" and o["target"] <= ref:
                    return "long target must be above the entry"
                if o["side"] == "short" and o["target"] >= ref:
                    return "short target must be below the entry"
        return None

    def working(self, ticker=None):
        return [o for o in self.orders if o["status"] == "working" and (ticker is None or o["ticker"] == ticker)]

    def cancel(self, ticker, ts, why="cancelled"):
        n = 0
        for o in self.working(ticker):
            o["status"], o["reason"] = "cancelled", why
            n += 1
        if n:
            self.log(ts, f"{ticker} working order cancelled: {why}")
        return n

    def modify(self, ticker, ts, stop=None, target=None, time_exit=None):
        p = self.positions.get(ticker)
        if not p:
            return "no open position"
        if stop:
            if (p["side"] == "long" and stop >= self.last_price.get(ticker, p["entry"])) or \
               (p["side"] == "short" and stop <= self.last_price.get(ticker, p["entry"])):
                return "new stop is on the wrong side of the price"
            p["stop"] = stop
        if target is not None:
            p["target"] = target or None
        if time_exit:
            p["time_exit"] = time_exit
        self.log(ts, f"{ticker} position updated: stop {p['stop']:.2f}"
                     + (f", target {p['target']:.2f}" if p["target"] else ""))
        return None

    def exit_at_next_bar(self, ticker, ts):
        if ticker in self.positions:
            self.pending_exits[ticker] = ts
            return None
        return "no open position"

    # ------------------------------------------------------------ fills

    def _size(self, fill, stop, size):
        cfg = self.cfg
        per_share = abs(fill - stop)
        if per_share <= 0:
            return 0
        risk = self.start_equity * cfg["risk_per_trade_pct"] / 100 * size
        shares = math.floor(risk / per_share)
        shares = min(shares, math.floor(self.start_equity * cfg["max_position_pct"] / 100 / fill))
        room = self.equity() * cfg["max_leverage"] - self.gross_exposure()
        shares = min(shares, math.floor(max(0.0, room) / fill))
        return max(0, shares)

    def _open(self, o, fill, ts):
        if self.blocked:
            o["status"], o["reason"] = "cancelled", self.blocked
            return None
        if len(self.positions) >= self.cfg["max_positions"]:
            o["status"], o["reason"] = "rejected", "max open positions reached"
            self.log(ts, f"{o['ticker']} entry skipped: max open positions", "warn")
            return None
        if (o["side"] == "long" and fill <= o["stop"]) or (o["side"] == "short" and fill >= o["stop"]):
            o["status"], o["reason"] = "cancelled", "price gapped past the stop before entry"
            self.log(ts, f"{o['ticker']} entry cancelled: gapped past the stop", "warn")
            return None
        sign = 1 if o["side"] == "long" else -1
        if o["target"] and (o["target"] - fill) * sign <= 0:
            o["status"], o["reason"] = "cancelled", "price gapped past the target before entry"
            self.log(ts, f"{o['ticker']} entry cancelled: gapped past the target", "warn")
            return None
        if o["kind"] == "stop" and (fill - o["price"]) * sign > abs(o["price"] - o["stop"]):
            # Acts like a stop-limit one R past the trigger: a gap that big has used up the trade's edge.
            o["status"], o["reason"] = "cancelled", "price gapped more than 1R past the entry"
            self.log(ts, f"{o['ticker']} entry cancelled: gapped more than 1R past the trigger", "warn")
            return None
        shares = self._size(fill, o["stop"], o["size"])
        if shares < 1:
            o["status"], o["reason"] = "rejected", "position size rounds to zero"
            return None
        fee = self.commission(shares)
        self.realized -= fee
        p = {"ticker": o["ticker"], "side": o["side"], "shares": shares, "entry": _r2(fill), "entry_ts": ts,
             "stop": o["stop"], "initial_stop": o["stop"], "target": o["target"], "time_exit": o["time_exit"],
             "breakeven_at": o["breakeven_at"], "order_id": o["id"], "plan_id": o["plan_id"], "rules": o["rules"],
             "setup": o["setup"], "confidence": o["confidence"], "fees": fee, "mfe": 0.0, "mae": 0.0}
        self.positions[o["ticker"]] = p
        o["status"], o["filled_ts"], o["fill"] = "filled", ts, _r2(fill)
        verb = "BUY" if o["side"] == "long" else "SHORT"
        self.log(ts, f"{o['ticker']} {verb} {shares} @ {fill:.2f} (stop {o['stop']:.2f})", "trade")
        return p

    def _close(self, tk, price, ts, reason):
        p = self.positions.pop(tk)
        self.pending_exits.pop(tk, None)
        fee = self.commission(p["shares"])
        sign = 1 if p["side"] == "long" else -1
        gross = (price - p["entry"]) * p["shares"] * sign
        self.realized += gross - fee
        risk = abs(p["entry"] - p["initial_stop"])
        trade = {**{k: p[k] for k in ("ticker", "side", "shares", "entry", "entry_ts", "initial_stop", "target",
                                      "plan_id", "rules", "setup", "confidence", "mfe", "mae")},
                 "exit": _r2(price), "exit_ts": ts, "exit_reason": reason, "fees": round(p["fees"] + fee, 4),
                 "pnl": round(gross - p["fees"] - fee, 2),
                 "r": round((price - p["entry"]) * sign / risk, 2) if risk else None}
        self.trades.append(trade)
        self.log(ts, f"{tk} exit {reason} @ {price:.2f}: {trade['pnl']:+.2f} ({trade['r']}R)",
                 "win" if trade["pnl"] > 0 else "loss")
        return trade

    def on_bar(self, tk, bar):
        """Process one finished bar for one ticker."""
        t, o, h, l, c = bar[:5]
        cfg = self.cfg
        # 1. Market exits requested earlier (check-in said "exit now").
        if tk in self.positions and tk in self.pending_exits and self.pending_exits[tk] <= t:
            p = self.positions[tk]
            self._close(tk, o - self.slip(o) if p["side"] == "long" else o + self.slip(o), t, "manual")
        # 2. Open positions: gap through stop, stop, target, time exit.
        p = self.positions.get(tk)
        if p:
            self._manage(tk, p, bar)
        # 3. Working entry orders.
        if tk not in self.positions:
            for od in self.working(tk):
                if od["active_from"] > t:
                    continue
                if od["expires"] and t >= od["expires"]:
                    od["status"], od["reason"] = "expired", "entry window closed"
                    self.log(t, f"{tk} order expired unfilled")
                    continue
                fill = self._trigger(od, o, h, l)
                if fill is None:
                    continue
                p = self._open(od, fill, t)
                if p:
                    # Same bar: the stop may already have been hit after the fill; the target is not credited.
                    if (p["side"] == "long" and l <= p["stop"]) or (p["side"] == "short" and h >= p["stop"]):
                        px = p["stop"] - self.slip(p["stop"]) if p["side"] == "long" else p["stop"] + self.slip(p["stop"])
                        self._close(tk, px, t, "stop (entry bar)")
                break
        self.last_price[tk] = c
        self._check_daily_stop(t)

    def _trigger(self, od, o, h, l):
        s = self.slip
        if od["kind"] == "market":
            return o + s(o) if od["side"] == "long" else o - s(o)
        px = od["price"]
        if od["kind"] == "stop":
            if od["side"] == "long" and h >= px:
                f = max(px, o)
                return f + s(f)
            if od["side"] == "short" and l <= px:
                f = min(px, o)
                return f - s(f)
            return None
        if od["side"] == "long" and l <= px - TICK:
            return min(px, o)
        if od["side"] == "short" and h >= px + TICK:
            return max(px, o)
        return None

    def _manage(self, tk, p, bar):
        t, o, h, l, c = bar[:5]
        long = p["side"] == "long"
        sl = self.slip
        if p["time_exit"] and t >= p["time_exit"]:
            self._close(tk, o - sl(o) if long else o + sl(o), t, "time")
            return
        if long:
            p["mfe"] = max(p["mfe"], h - p["entry"])
            p["mae"] = min(p["mae"], l - p["entry"])
            if o <= p["stop"]:
                self._close(tk, o - sl(o), t, "stop (gap)")
            elif l <= p["stop"]:
                self._close(tk, p["stop"] - sl(p["stop"]), t, "stop")
            elif p["target"] and o >= p["target"]:
                self._close(tk, o, t, "target (gap)")
            elif p["target"] and h >= p["target"] + TICK:
                self._close(tk, p["target"], t, "target")
            elif p["breakeven_at"] and h >= p["breakeven_at"] and p["stop"] < p["entry"]:
                p["stop"] = p["entry"]
                self.log(t, f"{tk} stop moved to breakeven {p['entry']:.2f}")
        else:
            p["mfe"] = max(p["mfe"], p["entry"] - l)
            p["mae"] = min(p["mae"], p["entry"] - h)
            if o >= p["stop"]:
                self._close(tk, o + sl(o), t, "stop (gap)")
            elif h >= p["stop"]:
                self._close(tk, p["stop"] + sl(p["stop"]), t, "stop")
            elif p["target"] and o <= p["target"]:
                self._close(tk, o, t, "target (gap)")
            elif p["target"] and l <= p["target"] - TICK:
                self._close(tk, p["target"], t, "target")
            elif p["breakeven_at"] and l <= p["breakeven_at"] and p["stop"] > p["entry"]:
                p["stop"] = p["entry"]
                self.log(t, f"{tk} stop moved to breakeven {p['entry']:.2f}")

    def _check_daily_stop(self, t):
        limit = self.cfg["max_daily_loss_pct"]
        if self.blocked or not limit:
            return
        if self.equity() - self.start_equity <= -self.start_equity * limit / 100:
            self.blocked = f"daily loss limit {limit}% reached"
            for od in self.working():
                od["status"], od["reason"] = "cancelled", self.blocked
            for tk in list(self.positions):
                self.pending_exits[tk] = t
            self.log(t, f"Daily loss limit hit: no new entries, flattening", "warn")

    def flatten(self, ts, prices, reason="close"):
        """Exit everything at the given prices (used at the end of the session)."""
        for o in self.working():
            o["status"], o["reason"] = "expired", "session over"
        for tk in list(self.positions):
            p = self.positions[tk]
            px = prices.get(tk, self.last_price.get(tk, p["entry"]))
            self._close(tk, px - self.slip(px) if p["side"] == "long" else px + self.slip(px), ts, reason)
