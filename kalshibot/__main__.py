"""Command line entry: python -m kalshibot [dashboard|backtest|live|test|usage]"""
import argparse
import json
import sys
import threading
import time
import webbrowser

from . import backtest, config, storage, usage


def cmd_dashboard(args):
    import socket
    from .web.app import serve
    url = f"http://127.0.0.1:{args.port}"
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", args.port)) == 0:
            print(f"Dashboard is already running at {url}; opening it.")
            if not args.no_browser:
                webbrowser.open(url)
            return
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    serve(port=args.port)


def cmd_backtest(args):
    params = {"count": args.count, "sample": args.sample, "start": args.start, "end": args.end, "seed": args.seed,
              "name": args.name}
    for key in ("strategy", "model", "effort"):
        if getattr(args, key):
            params[key] = getattr(args, key)
    if args.assets:
        params["assets"] = args.assets
    est = backtest.estimate(params)
    print(json.dumps(est, indent=1))
    run = backtest.start(params)
    last = ""
    while run.status in ("queued", "loading", "deciding", "simulating"):
        line = f"[{run.status}] {run.message}"
        if line != last:
            print(line, flush=True)
            last = line
        time.sleep(1)
    print(f"[{run.status}] {run.message}")
    data = backtest.load_saved(run.id)
    if data:
        s = {k: v for k, v in data["summary"].items() if k != "calibration"}
        print(json.dumps(s, indent=1, default=str))
        print(f"Saved data/backtests/{run.id}.json")


def cmd_live(args):
    from .live import get_trader
    t = get_trader()
    t.start()
    try:
        while t.running:
            time.sleep(1)
    except KeyboardInterrupt:
        t.stop()
        t.thread.join(timeout=10)


def cmd_test(args):
    from . import markets, snapshot, strategies
    cfg = config.load()
    asset = (args.asset or cfg["assets"][0]).upper()
    m = markets.open_market(asset)
    if not m:
        sys.exit(f"No open {asset} market right now.")
    now = int(time.time())
    s = snapshot.build(asset, m["ticker"], markets.ts(m["close_time"]), now, markets.fnum(m.get("floor_strike")),
                       markets.quote(m), markets.spot_candles(asset, now, 90))
    if not s:
        sys.exit("Not enough data yet (strike or spot candles missing). Try again in a minute.")
    print(snapshot.prompt(s, cfg["instructions"]))
    print(f"\n--- {cfg['model']} / {cfg['effort']} ---")
    print(json.dumps(strategies.claude(s, cfg, "test"), indent=1))


def cmd_usage(args):
    print(json.dumps({k: v for k, v in usage.summary().items() if k not in ("recent", "per_day")}, indent=1))


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    storage.ensure_dirs()
    ap = argparse.ArgumentParser(prog="kalshibot", description="Kalshi paper-trading bot driven by Claude Code.")
    sub = ap.add_subparsers(dest="cmd")

    d = sub.add_parser("dashboard", help="open the web dashboard (default)")
    d.add_argument("--port", type=int, default=8050)
    d.add_argument("--no-browser", action="store_true")

    b = sub.add_parser("backtest", help="run a backtest from the terminal")
    b.add_argument("--count", type=int, default=50)
    b.add_argument("--sample", default="random", choices=["random", "latest", "all"])
    b.add_argument("--start", default=None)
    b.add_argument("--end", default=None)
    b.add_argument("--seed", type=int, default=1)
    b.add_argument("--strategy", choices=list(config.STRATEGIES))
    b.add_argument("--model", choices=list(config.MODELS))
    b.add_argument("--effort", choices=config.EFFORTS)
    b.add_argument("--assets", nargs="+")
    b.add_argument("--name", default="")

    sub.add_parser("live", help="live paper trading without the dashboard")
    t = sub.add_parser("test", help="ask Claude about the current market once")
    t.add_argument("--asset")
    sub.add_parser("usage", help="print Claude usage totals")

    args = ap.parse_args(argv)
    if args.cmd is None:
        args = ap.parse_args(["dashboard"] + (argv or sys.argv[1:]))
    {"dashboard": cmd_dashboard, "backtest": cmd_backtest, "live": cmd_live,
     "test": cmd_test, "usage": cmd_usage}[args.cmd](args)


if __name__ == "__main__":
    main()
