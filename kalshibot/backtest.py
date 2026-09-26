"""Replay settled Kalshi markets with the quotes that existed at decision time.

Phase 1 loads data using only what was known at the decision minute (Kalshi per-minute
bid/ask, Coinbase 1-minute candles). Phase 2 gets decisions (parallel Claude calls, cached).
Phase 3 replays trades in time order through a paper account: fills use the ask after the
decision's real latency plus slippage, Kalshi fees, sizing, and daily limits.
"""
import random
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

from . import claude_cli, config, markets, metrics, snapshot, storage, strategies
from .portfolio import Account

_runs = {}
_runs_lock = threading.Lock()
MAX_MARKETS = 3000


def _utc_day(iso):
    return iso[:10]


def select_markets(assets, start, end, count, sample, seed, progress=None):
    pool = []
    for asset in assets:
        for m in markets.settled_markets(asset, progress):
            if start <= _utc_day(m["close_time"]) <= end and m.get("floor_strike"):
                pool.append({**m, "asset": asset})
    pool.sort(key=lambda m: m["close_time"])
    if sample == "random" and len(pool) > count:
        pool = random.Random(seed).sample(pool, count)
    elif sample == "latest":
        pool = pool[-count:]
    pool = pool[:MAX_MARKETS]
    pool.sort(key=lambda m: m["close_time"])
    return pool


def usage_averages(model, effort):
    """Mean latency and tokens of past successful calls for this model/effort, if any."""
    rows = [r for r in storage.read_jsonl(storage.USAGE_FILE)
            if r.get("ok") and r.get("model") == model and r.get("effort") == effort]
    if not rows:
        return {"latency_s": 12.0, "tokens": 2500, "api_equiv_usd": 0.01, "samples": 0}
    n = len(rows)
    return {
        "latency_s": sum(r["latency_s"] for r in rows) / n,
        "tokens": sum(r.get("input_tokens", 0) + r.get("cache_creation_tokens", 0) + r.get("cache_read_tokens", 0)
                      + r.get("output_tokens", 0) for r in rows) / n,
        "api_equiv_usd": sum(r.get("api_equiv_usd", 0) for r in rows) / n,
        "samples": n,
    }


def normalize_params(p):
    cfg = config.validate({**config.load(), **{k: v for k, v in p.items() if k in config.DEFAULTS}})
    today = date.today().isoformat()
    params = {
        "start": p.get("start") or "2000-01-01",
        "end": p.get("end") or today,
        "count": int(min(MAX_MARKETS, max(1, int(p.get("count") or 50)))),
        "sample": p.get("sample") if p.get("sample") in ("latest", "random", "all") else "random",
        "seed": int(p.get("seed") or 1),
        "name": str(p.get("name") or "")[:80],
    }
    return cfg, params


def estimate(p):
    cfg, params = normalize_params(p)
    pool = select_markets(cfg["assets"], params["start"], params["end"], params["count"],
                          params["sample"], params["seed"])
    out = {"markets": len(pool), "claude_calls_max": 0}
    if pool:
        out["first"], out["last"] = pool[0]["close_time"], pool[-1]["close_time"]
    if strategies.uses_claude(cfg):
        avg = usage_averages(cfg["model"], cfg["effort"])
        out.update({
            "claude_calls_max": len(pool),
            "est_minutes": round(len(pool) * avg["latency_s"] / cfg["parallel_calls"] / 60, 1),
            "est_tokens": int(len(pool) * avg["tokens"]),
            "est_api_equiv_usd": round(len(pool) * avg["api_equiv_usd"], 2),
            "based_on_samples": avg["samples"],
        })
    return out


class Run:
    def __init__(self, cfg, params):
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.cfg, self.params = cfg, params
        self.status, self.message = "queued", ""
        self.done, self.total = 0, 0
        self.cancelled = False
        self.created = time.time()
        self.calls = {"made": 0, "cached": 0, "errors": 0}

    def info(self):
        return {"id": self.id, "status": self.status, "message": self.message, "done": self.done,
                "total": self.total, "calls": self.calls, "params": self.params,
                "strategy": self.cfg["strategy"], "model": self.cfg["model"], "effort": self.cfg["effort"],
                "created": self.created}

    def progress(self, msg):
        self.message = msg

    # ------------------------------------------------------------ phases

    def load(self, pool):
        self.status, self.total, self.done = "loading", len(pool), 0
        items = []
        delay = int(self.cfg["time_delay"] * 60)
        for m in pool:
            if self.cancelled:
                break
            self.done += 1
            self.message = f"Loading market data {self.done}/{self.total}"
            close_ts = markets.ts(m["close_time"])
            decision_ts = close_ts - delay
            candles = markets.market_candles(m["asset"], m["ticker"], m["open_time"], m["close_time"])
            if not candles:
                continue
            q = markets.historical_quote(candles, decision_ts)
            spot = markets.spot_candles(m["asset"], decision_ts, 90)
            s = snapshot.build(m["asset"], m["ticker"], close_ts, decision_ts, m["floor_strike"], q, spot)
            if s:
                items.append({"market": m, "snap": s, "candles": candles})
        return items

    def decide(self, items):
        self.status, self.total, self.done = "deciding", len(items), 0
        workers = self.cfg["parallel_calls"] if strategies.uses_claude(self.cfg) else 1
        stop_reason = None

        def one(item):
            if self.cancelled or stop_reason:
                return item, None, "cancelled"
            try:
                return item, strategies.decide(item["snap"], self.cfg, "backtest"), None
            except claude_cli.ClaudeError as e:
                return item, None, e

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one, it) for it in items]
            for fut in as_completed(futures):
                item, d, err = fut.result()
                self.done += 1
                if d:
                    item["decision"] = d
                    self.calls["cached" if d.get("cached") else "made"] += 1 if strategies.uses_claude(self.cfg) else 0
                elif isinstance(err, claude_cli.ClaudeError):
                    self.calls["errors"] += 1
                    item["error"] = str(err)
                    if err.kind in ("limit", "auth", "missing") and not stop_reason:
                        stop_reason = str(err)
                self.message = f"Deciding {self.done}/{self.total}"
        return stop_reason

    def simulate(self, items):
        self.status = "simulating"
        cfg = self.cfg
        acct = Account(cfg["bankroll"])
        decided = sorted((it for it in items if "decision" in it), key=lambda it: it["snap"].decision_ts)
        results = {it["market"]["ticker"]: it["market"]["result"] for it in decided}
        log = []

        def settle_until(t):
            for tk in sorted(acct.positions, key=lambda k: acct.positions[k]["close_ts"]):
                pos = acct.positions[tk]
                if pos["close_ts"] <= t:
                    acct.settle(tk, results[tk], pos["close_ts"])

        for it in decided:
            s, d, m = it["snap"], it["decision"], it["market"]
            settle_until(s.decision_ts)
            actual_up = m["result"] == "yes"
            row = {"ticker": s.ticker, "asset": s.asset, "decision_ts": s.decision_ts,
                   "direction": d["direction"], "probability_up": d.get("probability_up"),
                   "market_up": s.market_prob_up, "baseline_up": s.features["baseline_up"],
                   "actual_up": actual_up, "reason": d.get("reason", ""), "cached": d.get("cached", False),
                   "latency_s": d.get("latency_s", 0), "up_ask": s.up_ask, "down_ask": s.down_ask,
                   "spot": s.spot, "strike": s.strike, "trade": None, "skip_reason": ""}
            log.append(row)
            if d["direction"] == "SKIP":
                row["skip_reason"] = "model skipped"
                continue
            side = d["direction"] if cfg["obey_model"] else ("DOWN" if d["direction"] == "UP" else "UP")
            block = acct.daily_block(s.decision_ts, cfg)
            if block:
                row["skip_reason"] = block
                continue
            # Fill at the ask that existed once the model had answered.
            fill_ts = s.decision_ts + int(d.get("latency_s") or 0) + 1
            q = markets.historical_quote(it["candles"], fill_ts) or {}
            ask = q.get("up_ask" if side == "UP" else "down_ask")
            if ask is None or not 0 < ask < 1:
                row["skip_reason"] = "no ask at fill time"
                continue
            price = min(0.99, round(ask + cfg["slippage"], 4))
            if price > cfg["max_price"]:
                row["skip_reason"] = f"price {price:.2f} above max {cfg['max_price']:.2f}"
                continue
            p_up = d.get("probability_up")
            p_side = None if p_up is None else (p_up if side == "UP" else 1 - p_up)
            stake, why = acct.stake_for(cfg, price, p_side)
            if stake <= 0:
                row["skip_reason"] = why
                continue
            n, fees = acct.contracts_for(stake, price, cfg["simulate_fees"])
            if n <= 0:
                row["skip_reason"] = "stake too small after fees"
                continue
            acct.open(s.ticker, s.asset, side, price, n, fees, s.close_ts, fill_ts,
                      {"model_direction": d["direction"], "probability_up": p_up})
            row["trade"] = {"side": side, "price": price, "contracts": n, "fees": fees}
        settle_until(float("inf"))

        pnl_by_ticker = {t["ticker"]: t for t in acct.closed}
        for row in log:
            if row["trade"] and row["ticker"] in pnl_by_ticker:
                row["trade"]["pnl"] = pnl_by_ticker[row["ticker"]]["pnl"]
                row["trade"]["won"] = pnl_by_ticker[row["ticker"]]["won"]
        summary = metrics.summarize(log, acct.closed, acct.equity_curve, cfg["bankroll"])
        return log, acct, summary

    # ------------------------------------------------------------ driver

    def run(self):
        try:
            self.status = "loading"
            pool = select_markets(self.cfg["assets"], self.params["start"], self.params["end"],
                                  self.params["count"], self.params["sample"], self.params["seed"], self.progress)
            if not pool:
                raise ValueError("No settled markets in that date range.")
            items = self.load(pool)
            stop_reason = self.decide(items) if not self.cancelled else None
            log, acct, summary = self.simulate(items)
            skipped_errors = sum(1 for it in items if "error" in it)
            self.status = "cancelled" if self.cancelled else ("stopped" if stop_reason else "done")
            self.message = stop_reason or ("Cancelled; partial results saved." if self.cancelled else "Finished")
            result = {
                **self.info(),
                "config": self.cfg,
                "summary": summary,
                "decisions": log,
                "trades": acct.closed,
                "equity_curve": [[self.cfg_start_ts(pool), self.cfg["bankroll"]]] + acct.equity_curve,
                "errors": skipped_errors,
                "finished": time.time(),
            }
            storage.write_json(storage.BACKTESTS / f"{self.id}.json", result)
        except Exception as e:
            self.status, self.message = "error", f"{e}"
            traceback.print_exc()

    @staticmethod
    def cfg_start_ts(pool):
        return markets.ts(pool[0]["open_time"]) if pool else 0


def start(p):
    with _runs_lock:
        busy = [r for r in _runs.values() if r.status in ("queued", "loading", "deciding", "simulating")]
        if busy:
            raise RuntimeError(f"Backtest {busy[0].id} is still running.")
        cfg, params = normalize_params(p)
        run = Run(cfg, params)
        _runs[run.id] = run
    threading.Thread(target=run.run, daemon=True).start()
    return run


def active():
    with _runs_lock:
        return [r.info() for r in _runs.values() if r.status in ("queued", "loading", "deciding", "simulating")]


def status(run_id):
    r = _runs.get(run_id)
    return r.info() if r else None


def cancel(run_id):
    r = _runs.get(run_id)
    if r:
        r.cancelled = True
    return bool(r)


def list_saved():
    out = []
    for path in sorted(storage.BACKTESTS.glob("*.json"), reverse=True):
        data = storage.read_json(path)
        if not data:
            continue
        out.append({k: data.get(k) for k in ("id", "status", "message", "params", "strategy", "model",
                                             "effort", "created", "calls", "summary")})
        out[-1]["summary"] = {k: v for k, v in (data.get("summary") or {}).items() if k != "calibration"}
        out[-1]["assets"] = data.get("config", {}).get("assets")
        out[-1]["time_delay"] = data.get("config", {}).get("time_delay")
        out[-1]["sizing"] = data.get("config", {}).get("sizing")
    for info in active():
        out.insert(0, {**info, "summary": None})
    return out


def load_saved(run_id):
    return storage.read_json(storage.BACKTESTS / f"{run_id}.json")


def delete_saved(run_id):
    path = storage.BACKTESTS / f"{run_id}.json"
    if path.exists() and path.parent == storage.BACKTESTS:
        path.unlink()
        return True
    return False
