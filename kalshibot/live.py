"""Live paper trading on the markets that are open right now. Never places real orders."""
import threading
import time
import traceback
from collections import deque
from datetime import datetime

from . import claude_cli, config, markets, snapshot, storage, strategies
from .portfolio import Account

STATE_FILE = storage.LIVE / "state.json"
DECISIONS_FILE = storage.LIVE / "decisions.jsonl"


class LiveTrader:
    def __init__(self):
        self.thread = None
        self.stop_flag = threading.Event()
        self.events = deque(maxlen=300)
        self.lock = threading.Lock()
        self.last_error = ""
        self.waiting = {}  # asset -> status line
        self._load()

    # ------------------------------------------------------------ persistence

    def _load(self):
        st = storage.read_json(STATE_FILE) or {}
        cfg = config.load()
        self.account = Account(cfg["bankroll"], st.get("account"))
        self.decided = st.get("decided", {})      # ticker -> decision row awaiting its outcome
        self.started = st.get("started") or time.time()
        self.events.extend(st.get("events", [])[-300:])

    def _save(self):
        storage.write_json(STATE_FILE, {"account": self.account.to_state(), "decided": self.decided,
                                        "started": self.started, "events": list(self.events)})

    def log(self, msg, kind="info"):
        line = {"t": time.time(), "msg": msg, "kind": kind}
        self.events.append(line)
        print(f"{datetime.now():%H:%M:%S}  {msg}", flush=True)

    # ------------------------------------------------------------ control

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self):
        if self.running:
            return False
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stop_flag.set()
        return True

    def reset(self):
        if self.running:
            raise RuntimeError("Stop live trading before resetting the account.")
        cfg = config.load()
        with self.lock:
            self.account = Account(cfg["bankroll"])
            self.decided, self.started = {}, time.time()
            self.events.clear()
            self.log(f"Account reset to ${cfg['bankroll']:,.2f}")
            self._save()

    # ------------------------------------------------------------ loop

    def _loop(self):
        cfg = config.load()
        self.log(f"Live paper trading started | {', '.join(cfg['assets'])} | {config.STRATEGIES[cfg['strategy']]}"
                 + (f" | {cfg['model']} / {cfg['effort']}" if strategies.uses_claude(cfg) else ""))
        while not self.stop_flag.is_set():
            try:
                cfg = config.load()  # dashboard changes apply on the next decision
                with self.lock:
                    self._settle()
                    for asset in cfg["assets"]:
                        self._tick(asset, cfg)
                    self._save()
                self.last_error = ""
            except Exception as e:
                self.last_error = repr(e)
                self.log(f"Loop error: {e!r}", "error")
                traceback.print_exc()
            self.stop_flag.wait(3)
        self.log("Live paper trading stopped")
        with self.lock:
            self._save()

    def _settle(self):
        now = time.time()
        for tk, row in list(self.decided.items()):
            if row["close_ts"] > now - 30:
                continue
            m = markets.market(tk)
            result = (m or {}).get("result", "")
            if result not in ("yes", "no"):
                continue
            row["actual_up"] = result == "yes"
            if tk in self.account.positions:
                t = self.account.settle(tk, result, int(now))
                row["trade"]["pnl"], row["trade"]["won"] = t["pnl"], t["won"]
                self.log(f"{row['asset']} settled {'UP' if row['actual_up'] else 'DOWN'} | {t['side']} "
                         f"{'WON' if t['won'] else 'LOST'} {t['pnl']:+.2f} | equity ${self.account.equity():,.2f}",
                         "win" if t["won"] else "loss")
            elif row["direction"] in ("UP", "DOWN", "SKIP"):
                verdict = "right" if row["direction"] == ("UP" if row["actual_up"] else "DOWN") else "wrong"
                self.log(f"{row['asset']} settled {'UP' if row['actual_up'] else 'DOWN'} | call was "
                         f"{row['direction']} ({verdict if row['direction'] != 'SKIP' else 'no trade'})")
            storage.append_jsonl(DECISIONS_FILE, row)
            del self.decided[tk]

    def _tick(self, asset, cfg):
        m = markets.open_market(asset)
        if not m:
            self.waiting[asset] = "waiting for market data"
            return
        tk = m["ticker"]
        close_ts = markets.ts(m["close_time"])
        mins_left = (close_ts - time.time()) / 60
        if tk in self.decided:
            row = self.decided[tk]
            self.waiting[asset] = (f"holding {row['trade']['side']} until close ({mins_left:.1f}m)" if row.get("trade")
                                   else f"{row['direction']} this session; next in {max(0, mins_left):.1f}m")
            return
        if mins_left > cfg["time_delay"]:
            self.waiting[asset] = f"deciding in {mins_left - cfg['time_delay']:.1f}m"
            return
        if mins_left < 0.5:
            self.waiting[asset] = "too late in this session"
            return

        now_ts = int(time.time())
        strike = markets.fnum(m.get("floor_strike"))
        spot = markets.spot_candles(asset, now_ts, 90)
        s = snapshot.build(asset, tk, close_ts, now_ts, strike, markets.quote(m), spot)
        if not s:
            self.waiting[asset] = "waiting for strike / spot data"
            return

        self.waiting[asset] = "asking model..."
        row = {"ticker": tk, "asset": asset, "close_ts": close_ts, "decision_ts": now_ts,
               "strategy": cfg["strategy"], "model": cfg["model"], "effort": cfg["effort"],
               "up_ask": s.up_ask, "down_ask": s.down_ask, "market_up": s.market_prob_up,
               "baseline_up": s.features["baseline_up"], "spot": s.spot, "strike": s.strike,
               "trade": None, "skip_reason": ""}
        try:
            d = strategies.decide(s, cfg, "live")
        except claude_cli.ClaudeError as e:
            row.update({"direction": "ERROR", "probability_up": None, "reason": str(e)})
            self.decided[tk] = row
            self.log(f"{asset} model call failed: {e}", "error")
            return
        row.update({"direction": d["direction"], "probability_up": d.get("probability_up"),
                    "reason": d.get("reason", ""), "latency_s": d.get("latency_s", 0)})
        self.decided[tk] = row
        who = f"{cfg['model']}/{cfg['effort']}" if strategies.uses_claude(cfg) else cfg["strategy"]
        self.log(f"{asset} {who} says {d['direction']} (P(up) {d.get('probability_up', 0.5):.2f}, "
                 f"{d.get('latency_s', 0):.0f}s): {d.get('reason', '')}", "decision")
        if d["direction"] == "SKIP":
            row["skip_reason"] = "model skipped"
            return
        self._enter(row, d, cfg)

    def _enter(self, row, d, cfg):
        acct, tk, asset = self.account, row["ticker"], row["asset"]
        side = d["direction"] if cfg["obey_model"] else ("DOWN" if d["direction"] == "UP" else "UP")
        block = acct.daily_block(time.time(), cfg)
        if block:
            row["skip_reason"] = block
            self.log(f"{asset} no trade: {block}")
            return
        fresh = markets.market(tk)  # re-quote: the model took a while
        if not fresh or fresh.get("status") not in ("active", "open"):
            row["skip_reason"] = "market closed before entry"
            return
        top = markets.quote(fresh)["up_ask" if side == "UP" else "down_ask"]
        if not top or not 0 < top < 1:
            row["skip_reason"] = "no ask"
            return
        if top > cfg["max_price"]:
            row["skip_reason"] = f"ask {top:.2f} above max {cfg['max_price']:.2f}"
            self.log(f"{asset} no trade: {row['skip_reason']}")
            return
        p_up = d.get("probability_up")
        p_side = None if p_up is None else (p_up if side == "UP" else 1 - p_up)
        stake, why = acct.stake_for(cfg, top, p_side)
        if stake <= 0:
            row["skip_reason"] = why
            self.log(f"{asset} no trade: {why}")
            return
        n, _ = acct.contracts_for(stake, top, cfg["simulate_fees"])
        book = markets.order_book(tk)
        price = markets.walk_book(book[side], n) if book and book.get(side) else top
        if price is None:
            row["skip_reason"] = "order book too thin"
            self.log(f"{asset} no trade: order book too thin for {n} contracts")
            return
        n, fees = acct.contracts_for(stake, price, cfg["simulate_fees"])
        if n <= 0:
            row["skip_reason"] = "stake too small"
            return
        acct.open(tk, asset, side, price, n, fees, row["close_ts"], int(time.time()),
                  {"model_direction": d["direction"], "probability_up": p_up})
        row["trade"] = {"side": side, "price": round(price, 4), "contracts": n, "fees": fees}
        self.log(f"{asset} PAPER BUY {side} x{n} @ {price:.3f} (${price * n:,.2f} + ${fees:.2f} fee) | "
                 f"cash ${acct.cash:,.2f}", "trade")

    # ------------------------------------------------------------ dashboard view

    def status(self):
        with self.lock:
            acct = self.account
            history = storage.read_jsonl(DECISIONS_FILE)
            since = [r for r in history if r.get("decision_ts", 0) >= self.started]
            return {
                "running": self.running,
                "started": self.started,
                "bankroll": acct.bankroll,
                "cash": round(acct.cash, 2),
                "equity": round(acct.equity(), 2),
                "realized_pnl": round(acct.realized_pnl(), 2),
                "positions": list(acct.positions.values()),
                "closed": acct.closed[-200:],
                "equity_curve": [[self.started, acct.bankroll]] + acct.equity_curve,
                "pending": list(self.decided.values()),
                "history": since[-500:],
                "waiting": self.waiting,
                "events": list(self.events)[-150:],
                "last_error": self.last_error,
            }


trader = None


def get_trader():
    global trader
    if trader is None:
        storage.ensure_dirs()
        trader = LiveTrader()
    return trader
