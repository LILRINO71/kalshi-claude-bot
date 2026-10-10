"""python -m daytrader [dashboard|backtest|compare|premarket|live|runs|playbook|report|settings]"""
import argparse
import json
import sys
from datetime import date, timedelta

from . import alpaca, backtest, brains, clock, config, live, metrics, report, store
from .data import LiveData
from .memory import Memory
from .session import DaySession


def _cfg(args):
    cfg = config.load()
    for k in ("brain", "model", "effort", "interval"):
        if getattr(args, k, None):
            cfg[k] = getattr(args, k)
    if getattr(args, "universe", None):
        cfg["universe"] = args.universe
    if getattr(args, "no_learn", False):
        cfg["learn"] = False
    if getattr(args, "focus", None):
        cfg["focus_max"] = args.focus
    if getattr(args, "checkins", None) is not None:
        cfg["checkins"] = [c for c in args.checkins.split(",") if c]
    return config.validate(cfg)


def cmd_dashboard(args):
    import socket
    import threading
    import webbrowser
    from .web import serve
    url = f"http://127.0.0.1:{args.port}"
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", args.port)) == 0:
            print(f"The dashboard is already running at {url}; opening it.")
            if not args.no_browser:
                webbrowser.open(url)
            return
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    serve(port=args.port)


def cmd_backtest(args):
    cfg = _cfg(args)
    end = args.end or clock.today_et()
    # Default to the longest clean window: from the day after the model's cutoff when older bars can be had
    # (an Alpaca key), else Yahoo's ~60 days of 5-minute bars.
    clean = (date.fromisoformat(config.cutoff_for(cfg)) + timedelta(days=1)).isoformat()
    start = args.start or (clean if alpaca.configured() else (date.fromisoformat(end) - timedelta(days=55)).isoformat())
    state = backtest.run(cfg, start, end, name=args.name, profile=args.profile, save_profile=args.save_profile,
                         allow_leaky=args.allow_leaky, resume=args.resume, max_days=args.days)
    s = state.get("summary") or {}
    print(json.dumps({k: v for k, v in s.items() if k not in ("by_setup", "by_exit", "learning_curve")},
                     indent=1, default=str))


def cmd_regimes(args):
    """How volatile each month (and the last few weeks) was, to pick test windows."""
    from . import regime
    from .data import MarketData
    data = MarketData()
    g = regime.gauges(data)
    cutoff = config.cutoff_for(config.load())
    print("month     days  VIX   SPY range  movers 5%+  calm/normal/volatile")
    for r in regime.monthly(g)[-args.months:]:
        flag = "  (model may remember)" if r["month"] <= cutoff[:7] else ""
        print(f"{r['month']}   {r['days']:>3}  {r['vix'] or 0:5.1f}  {r['spy_range']:6.2f}%   {r['movers']:6.1f}%     "
              f"{r['calm']:>2}/{r['normal']:>2}/{r['volatile']:>2}{flag}")
    print(f"\nYahoo 5-minute bars start {data.yahoo_oldest()}; "
          + ("an Alpaca key is set, so earlier days come from Alpaca." if alpaca.configured()
             else "a free Alpaca key reaches further back (python -m daytrader alpaca KEY SECRET)."))
    vol = [d for d, x in sorted(g.items()) if x["label"] == "volatile" and d > cutoff][-15:]
    print("Most recent volatile days after the model's cutoff:", ", ".join(vol) or "none")


def cmd_alpaca(args):
    """Save an Alpaca market-data key and check it with a one-day fetch."""
    alpaca.save_keys(args.key_id, args.secret_key)
    day = clock.prev_weekday(clock.today_et(), 70)
    try:
        got = alpaca.fetch(["SPY"], day, day)
    except alpaca.AlpacaError as e:
        sys.exit(f"Saved, but the check failed: {e}")
    bars = (got.get("SPY") or {}).get(day, [])
    print(f"Saved to {alpaca.key_file()}. Check: {len(bars)} five-minute SPY bars for {day}"
          + (" (no bars: that day may have been a holiday)" if not bars else ""))


def cmd_compare(args):
    states = []
    for arg in args.runs:  # "A+B" joins two runs that cover different days into one
        parts = []
        for rid in arg.split("+"):
            st = store.read_json(store.RUNS / rid / "run.json")
            if not st:
                sys.exit(f"No run {rid}")
            parts.append(st)
        states.append(metrics.merge_runs(parts))
    c = metrics.compare(states)
    print(f"{len(c['days'])} shared trading days" + (f": {c['days'][0]} to {c['days'][-1]}" if c["days"] else ""))
    for i, r in enumerate(c["runs"]):
        avg = "n/a" if r["avg_r"] is None else f"{r['avg_r']:+.2f}R"
        line = f"{r['id']}  {r['engine']:<22} trades {r['trades']:>4}  avg {avg:>7}  pnl {r['pnl']:>+10.2f}"
        v = r.get("vs_baseline")
        if v:
            lo, hi = v["ci"]
            ci = f" (95% CI {lo:+.3f} to {hi:+.3f})" if lo is not None else ""
            line += f"  vs baseline {v['mean_daily_pct_diff']:+.3f}%/day{ci}: {v['verdict']}"
        else:
            line += "  [baseline]"
        print(line)


def cmd_premarket(args):
    """Run today's morning brief and research now and print the plans. No trading."""
    cfg = _cfg(args)
    data = LiveData(cfg["interval"], cfg)
    folder = store.PROFILES / args.profile
    folder.mkdir(parents=True, exist_ok=True)
    memory = Memory(folder, seed=True)
    day = args.day or clock.today_et()
    now = clock.now_ts() if day == clock.today_et() else clock.at(day, cfg["premarket_time"])
    s = DaySession(day, cfg, data, brains.make(cfg, "premarket"), memory, cfg["bankroll"])
    s.premarket(now)
    print(json.dumps({"brief": s.brief, "plans": s.plans}, indent=1, default=str))


def cmd_live(args):
    live.run(_cfg(args), profile=args.profile, once=args.once)


def cmd_runs(args):
    for path in sorted(store.RUNS.glob("*/run.json"), reverse=True)[:args.limit]:
        st = store.read_json(path) or {}
        s = st.get("summary") or {}
        c = st.get("config") or {}
        eng = f"{c.get('model')}/{c.get('effort')}" if c.get("brain") == "claude" else c.get("brain")
        print(f"{st.get('id')}  {st.get('status'):<11} {eng:<16} {st.get('start')}..{st.get('end')}  "
              f"days {s.get('days', 0):>3}  trades {s.get('trades', 0):>4}  pnl {s.get('net_pnl', 0):>+10.2f}  "
              f"{st.get('name') or ''}")


def cmd_playbook(args):
    folder = store.RUNS / args.run / "memory" if args.run else store.PROFILES / args.profile
    if not (folder / "memory.json").exists():
        sys.exit(f"No memory at {folder}")
    m = Memory(folder)
    print(m.playbook_text())
    print(f"\n{len(m.lessons)} lessons in memory; playbook version {m.state['version']}")


def cmd_report(args):
    st = store.read_json(store.RUNS / args.run / "run.json", None, strict=True)
    if not st:
        sys.exit(f"No run {args.run}")
    print(report.write(st, Memory(store.RUNS / args.run / "memory")))


def cmd_settings(args):
    cfg = config.load()
    if args.set:
        for kv in args.set:
            k, _, v = kv.partition("=")
            if k not in config.DEFAULTS:
                sys.exit(f"unknown setting {k}")
            cur = config.DEFAULTS[k]
            cfg[k] = json.loads(v) if isinstance(cur, (list, dict, bool)) else v
        cfg = config.save(cfg)
    print(json.dumps(cfg, indent=1))


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    store.ensure_dirs()
    ap = argparse.ArgumentParser(prog="daytrader", description="Self-learning AI day trader (paper only) run by Claude.")
    sub = ap.add_subparsers(dest="cmd")

    d = sub.add_parser("dashboard", help="open the web dashboard (the default)")
    d.add_argument("--port", type=int, default=8050)
    d.add_argument("--no-browser", action="store_true")

    def engine(p):
        p.add_argument("--brain", choices=list(config.BRAINS))
        p.add_argument("--model", choices=list(config.MODELS))
        p.add_argument("--effort", choices=config.EFFORTS)
        p.add_argument("--interval", choices=["1m", "2m", "5m", "15m"])
        p.add_argument("--universe", nargs="+", help="only scan these tickers")
        p.add_argument("--focus", type=int, help="max stocks to focus on per day")
        p.add_argument("--checkins", help="comma-separated HH:MM check-in times, '' for none")
        p.add_argument("--no-learn", action="store_true", help="freeze the playbook (ablation)")

    b = sub.add_parser("backtest", help="walk-forward backtest over past days")
    engine(b)
    b.add_argument("--start")
    b.add_argument("--end")
    b.add_argument("--days", type=int, help="stop after this many days")
    b.add_argument("--name", default="")
    b.add_argument("--profile", help="start from this profile's memory instead of a fresh one")
    b.add_argument("--save-profile", help="save what the run learned into this profile")
    b.add_argument("--allow-leaky", action="store_true", help="allow dates before the model's training cutoff")
    b.add_argument("--resume", help="continue an interrupted run by id")

    p = sub.add_parser("premarket", help="today's morning brief and plans, printed (no trading)")
    engine(p)
    p.add_argument("--profile", default="live")
    p.add_argument("--day")

    lv = sub.add_parser("live", help="live paper trading in real time")
    engine(lv)
    lv.add_argument("--profile", default="live")
    lv.add_argument("--once", action="store_true", help="trade today only, then exit")

    c = sub.add_parser("compare", help="compare runs day by day on the days they share (first = baseline; "
                                       "A+B joins two runs that cover different days)")
    c.add_argument("runs", nargs="+")
    rg = sub.add_parser("regimes", help="how volatile each month was, to pick backtest windows")
    rg.add_argument("--months", type=int, default=24)
    ak = sub.add_parser("alpaca", help="save a free Alpaca market-data key for history older than ~60 days")
    ak.add_argument("key_id")
    ak.add_argument("secret_key")

    r = sub.add_parser("runs", help="list backtest runs")
    r.add_argument("--limit", type=int, default=20)
    pb = sub.add_parser("playbook", help="print a playbook")
    pb.add_argument("--profile", default="live")
    pb.add_argument("--run")
    rp = sub.add_parser("report", help="re-render a run's HTML report")
    rp.add_argument("run")
    st = sub.add_parser("settings", help="show or change settings: --set key=value")
    st.add_argument("--set", nargs="+")

    args = ap.parse_args(argv)
    if args.cmd is None:
        args = ap.parse_args(["dashboard"])
    {"dashboard": cmd_dashboard, "backtest": cmd_backtest, "compare": cmd_compare, "regimes": cmd_regimes,
     "alpaca": cmd_alpaca, "premarket": cmd_premarket, "live": cmd_live, "runs": cmd_runs, "playbook": cmd_playbook,
     "report": cmd_report, "settings": cmd_settings}[args.cmd](args)


if __name__ == "__main__":
    main()
