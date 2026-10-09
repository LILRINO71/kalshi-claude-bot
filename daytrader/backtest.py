"""Walk-forward backtest: the trader lives through each past day in order and learns as it goes.

Days run strictly in sequence because each day's reflection changes the next day's playbook.
Every run gets its own memory folder (a copy of a profile, or a fresh seeded one), saves after
every day so a crash loses nothing, and can be resumed. Runs on days before the model's training
cutoff are refused: a model that read about those days is not being tested.
"""
import shutil
import time
import traceback
import uuid
from datetime import datetime

from . import brains, clock, config, metrics, report, store
from .data import MarketData
from .memory import Memory
from .session import DaySession, StopRun


def _run_dir(run_id):
    return store.RUNS / run_id


def plan_days(data, start, end):
    days = data.trading_days(start, end)
    return [d for d in days if d < clock.today_et() or clock.now_ts() > clock.at(d, "16:10")]


def run(cfg, start, end, name="", profile=None, save_profile=None, allow_leaky=False, resume=None,
        max_days=None, log=print, should_stop=None, on_start=None):
    """Simulate each trading day in [start, end]. `should_stop()` is checked between days;
    `on_start(run_id)` is called once the run has an id."""
    store.ensure_dirs()
    cfg = config.validate(cfg)
    cutoff = config.cutoff_for(cfg)
    if cfg["brain"] == "claude" and start <= cutoff and not allow_leaky:
        raise ValueError(f"{cfg['model']}'s training data runs to about {cutoff}: it may already know what happened "
                         f"on days before then. Start after {cutoff}, or pass --allow-leaky to run anyway (the "
                         f"results will be flagged).")
    data = MarketData(cfg["interval"], cfg)
    if resume:
        run_id = resume
        state = store.read_json(_run_dir(run_id) / "run.json", None, strict=True)
        if not state:
            raise ValueError(f"no run {run_id}")
        cfg, start, end = state["config"], state["start"], state["end"]
    else:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        state = {"id": run_id, "name": name, "config": cfg, "start": start, "end": end, "status": "running",
                 "created": time.time(), "leaky": cfg["brain"] == "claude" and start <= cutoff, "cutoff": cutoff,
                 "profile": profile, "days": [], "trades": [], "equity_curve": [], "errors": []}
    folder = _run_dir(run_id)
    if on_start:
        on_start(run_id)
    mem_dir = folder / "memory"
    if not resume:
        mem_dir.mkdir(parents=True, exist_ok=True)
        if profile and (store.PROFILES / profile / "memory.json").exists():
            for f in (store.PROFILES / profile).iterdir():
                if f.is_file():
                    shutil.copy2(f, mem_dir / f.name)
    memory = Memory(mem_dir, seed=True)
    brain = brains.make(cfg, "backtest")

    days = plan_days(data, start, end)
    done = {d["day"] for d in state["days"]}
    todo = [d for d in days if d not in done][: max_days or None]
    log(f"Run {run_id}: {len(todo)} trading days to simulate ({start} to {end}), brain {cfg['brain']}"
        + (f" {cfg['model']}/{cfg['effort']}" if cfg["brain"] == "claude" else ""))
    if not todo:
        log("Nothing to do (no trading days with data in that range).")
    universe = cfg["universe"] or None
    data.prefetch(universe, todo, log=log)

    equity = state["days"][-1]["end_equity"] if state["days"] else cfg["bankroll"]
    if not state["equity_curve"]:
        state["equity_curve"].append([clock.at(todo[0], "09:30") if todo else 0, equity])
    store.write_json(folder / "run.json", state)
    try:
        for day in todo:
            if should_stop and should_stop():
                raise KeyboardInterrupt
            t0 = time.time()
            failures = dict(data.news_failures)
            s = DaySession(day, cfg, data, brain, memory, equity, log=log)
            ok = s.premarket(clock.at(day, cfg["premarket_time"]))
            if ok:
                for hhmm in cfg["checkins"]:
                    t = clock.at(day, hhmm)
                    if t < s.flatten_ts:
                        s.checkin(t)
                s.close()
            row = s.reflect(clock.at(day, "16:05"), learn=cfg["learn"])
            equity = row["end_equity"]
            failed = {h: n - failures.get(h, 0) for h, n in data.news_failures.items() if n > failures.get(h, 0)}
            if failed:
                row["news_failures"] = failed
                log(f"{day}: news requests failed ({', '.join(f'{h} x{n}' for h, n in failed.items())}); "
                    f"prompts had less news (GDELT often rate-limits shared networks; set use_gdelt false to skip it)")
            row.update({"seconds": round(time.time() - t0, 1), "brief": s.brief, "plans_detail": s.plans,
                        "checkins": s.checkins, "reflection": s.reflection, "events": s.broker.events,
                        "notes": s.notes, "scan": [{k: r[k] for k in ("ticker", "score", "news_mentions")} |
                                                   {"gap_pct": (r["pm"] or {}).get("gap_pct")} for r in s.scan],
                        "market_news": s.market_news[:20], "playbook": memory.playbook_text()})
            state["days"].append(row)
            state["trades"].extend({**t, "day": day} for t in s.broker.trades)
            state["equity_curve"].append([clock.at(day, "16:00"), equity])
            state["errors"].extend({"day": day, **n} for n in s.notes)
            store.write_json(folder / "run.json", state)
            log(f"{day}: {row['pnl']:+.2f} on {row['trades']} trades, equity {equity:,.2f}"
                f" | rules: {', '.join(row['rule_changes']) or 'no change'} ({row['seconds']}s)")
        state["status"] = "done"
    except StopRun as e:
        state["status"], state["message"] = "stopped", str(e)
        log(f"Stopped: {e}")
    except KeyboardInterrupt:
        state["status"], state["message"] = "interrupted", "interrupted; resume with --resume " + run_id
        log(state["message"])
    except Exception as e:
        state["status"], state["message"] = "error", repr(e)
        traceback.print_exc()
    finally:
        state["summary"] = metrics.summarize(state, data)
        state["playbook"] = memory.snapshot()
        state["finished"] = time.time()
        store.write_json(folder / "run.json", state)
        if save_profile:
            dest = store.PROFILES / save_profile
            dest.mkdir(parents=True, exist_ok=True)
            for f in mem_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, dest / f.name)
            log(f"Saved what this run learned to profile '{save_profile}'")
        path = report.write(state, memory)
        log(f"Report: {path}")
    return state
