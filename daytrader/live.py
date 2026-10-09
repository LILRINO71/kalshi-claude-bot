"""Live paper trading in real time: the same DaySession as the backtest, driven by the wall clock.

Run it before the open (or leave it running): it waits for the pre-market time, does the morning
brief and research, manages plans through the session, flattens before the close, reflects, and
carries what it learned into the next day. Never places a real order.
"""
import time

from . import brains, clock, config, report, store
from .data import LiveData
from .memory import Memory
from .session import DaySession, StopRun

ACCOUNT = store.LIVE / "account.json"
STATUS = store.LIVE / "status.json"

# NYSE full-day holidays; trading days in backtests come from real bars instead.
HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def is_trading_day(day):
    from datetime import date
    return date.fromisoformat(day).weekday() < 5 and day not in HOLIDAYS


def _sleep_until(ts):
    while clock.now_ts() < ts:
        time.sleep(min(30, max(1, ts - clock.now_ts())))


def _status(s, phase):
    b = s.broker
    store.write_json(STATUS, {"day": s.day, "phase": phase, "updated": clock.now_ts(), "focus": s.focus,
                              "equity": round(b.equity(), 2), "realized": round(b.realized, 2),
                              "positions": list(b.positions.values()), "orders": b.working(),
                              "events": b.events[-80:], "brief": s.brief, "plans": s.plans})


def run(cfg, profile="live", once=False, log=print):
    store.ensure_dirs()
    cfg = config.validate(cfg)
    data = LiveData(cfg["interval"], cfg)
    folder = store.PROFILES / profile
    folder.mkdir(parents=True, exist_ok=True)
    memory = Memory(folder, seed=True)
    brain = brains.make(cfg, "live")
    acct = store.read_json(ACCOUNT, None, strict=True) or {"equity": cfg["bankroll"], "days": [], "trades": []}
    log(f"Live paper trading | profile '{profile}' | equity ${acct['equity']:,.2f} | brain {cfg['brain']}"
        + (f" {cfg['model']}/{cfg['effort']}" if cfg["brain"] == "claude" else ""))
    while True:
        day = clock.today_et()
        done = {d["day"] for d in acct["days"]}
        if not is_trading_day(day) or day in done or clock.now_ts() > clock.at(day, "15:30"):
            if once:
                log(f"{day}: market closed, already traded, or too late to start; nothing to do.")
                return acct
            log(f"{day}: waiting for the next trading day")
            _sleep_until(clock.at(day, "23:59") + 120)
            continue
        _sleep_until(clock.at(day, cfg["premarket_time"]))
        s = DaySession(day, cfg, data, brain, memory, acct["equity"], log=log)
        try:
            s.premarket(clock.now_ts())
            _status(s, "pre-market")
            pending = [clock.at(day, h) for h in cfg["checkins"]]
            while clock.now_ts() < s.flatten_ts:
                now = clock.now_ts()
                s.advance_to(now)
                due = [t for t in pending if t <= now]
                if due:
                    pending = [t for t in pending if t > now]
                    s.checkin(now)
                _status(s, "trading")
                time.sleep(20)
            s.close()
            _status(s, "closed")
            _sleep_until(clock.at(day, "16:05"))
            row = s.reflect(clock.now_ts(), learn=cfg["learn"])
        except StopRun as e:
            log(f"Stopped: {e}. Flattening.")
            s.close()
            row = s.reflect(clock.now_ts(), learn=False)
        except Exception as e:  # keep the account consistent and carry on tomorrow
            log(f"Error during the session: {e!r}. Flattening.")
            s.close()
            row = s.reflect(clock.now_ts(), learn=False)
        acct["equity"] = row["end_equity"]
        acct["days"].append({k: row[k] for k in ("day", "pnl", "trades", "start_equity", "end_equity", "focus",
                                                 "summary", "rule_changes")})
        acct["trades"].extend({**t, "day": day} for t in s.broker.trades)
        store.write_json(ACCOUNT, acct)
        _status(s, "done")
        state = {"id": f"live-{profile}", "name": f"Live paper account ({profile})", "config": cfg,
                 "start": acct["days"][0]["day"], "end": day, "status": "live", "days": acct["days"],
                 "trades": acct["trades"], "equity_curve": [[clock.at(d["day"], "16:00"), d["end_equity"]]
                                                            for d in acct["days"]]}
        from . import metrics
        state["summary"] = metrics.summarize(state, data)
        log(f"{day}: {row['pnl']:+.2f}, equity ${acct['equity']:,.2f}. Report: "
            f"{report.write(state, memory, store.LIVE / 'report.html')}")
        if once:
            return acct
