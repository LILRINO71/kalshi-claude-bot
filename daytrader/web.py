"""Local dashboard: python -m daytrader dashboard, then open http://127.0.0.1:8050

Start and stop backtests, compare runs, watch the live paper account and its playbook, check Claude
usage, and change settings. Live trading itself runs in its own window (python -m daytrader live).
"""
import re
import shutil
import threading
import time
from collections import deque
from datetime import date, timedelta

from flask import Flask, jsonify, render_template, request, send_file

from . import alpaca, backtest, config, llm, metrics, store, usage
from .memory import SETUP_NAMES, Memory

app = Flask(__name__)
RUN_ID = re.compile(r"^(\d{8}-\d{6}-[0-9a-f]{4}|live-[a-z0-9_-]{1,40})$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROFILE = re.compile(r"^[a-z0-9_-]{1,40}$")

_job = {"thread": None, "stop": False, "lines": deque(maxlen=300), "run_id": None, "started": None, "error": ""}


def err(msg, code=400):
    return jsonify({"error": msg}), code


def _log(msg):
    _job["lines"].append(f"{time.strftime('%H:%M:%S')}  {msg}")


def _running():
    return bool(_job["thread"] and _job["thread"].is_alive())


def _summary_row(st):
    s = st.get("summary") or {}
    c = st.get("config") or {}
    return {"id": st.get("id"), "name": st.get("name"), "status": st.get("status"), "start": st.get("start"),
            "end": st.get("end"), "brain": c.get("brain"), "model": c.get("model"), "effort": c.get("effort"),
            "learn": c.get("learn"), "leaky": st.get("leaky"), "profile": st.get("profile"),
            "days": s.get("days", len(st.get("days") or [])), "trades": s.get("trades"), "net_pnl": s.get("net_pnl"),
            "return_pct": s.get("return_pct"), "spy_pct": s.get("benchmark_spy_pct"), "avg_r": s.get("avg_r"),
            "avg_r_ci": s.get("avg_r_ci"), "win_rate": s.get("win_rate"), "verdict": s.get("verdict"),
            "created": st.get("created")}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/meta")
def meta():
    cfg = config.load()
    clean = (date.fromisoformat(config.cutoff_for(cfg)) + timedelta(days=1)).isoformat()
    yahoo = (date.today() - timedelta(days=59)).isoformat()
    return jsonify({"models": config.MODELS, "efforts": config.EFFORTS, "brains": config.BRAINS,
                    "defaults": config.DEFAULTS, "claude_path": llm.find_claude(), "setups": SETUP_NAMES,
                    "alpaca": alpaca.configured(), "clean_start": clean, "yahoo_oldest": yahoo,
                    "history_start": clean if alpaca.configured() else max(clean, yahoo)})


@app.get("/api/settings")
def get_settings():
    return jsonify(config.load())


@app.post("/api/settings")
def post_settings():
    try:
        return jsonify(config.save({**config.load(), **(request.get_json(force=True) or {})}))
    except ValueError as e:
        return err(str(e))


@app.get("/api/usage")
def get_usage():
    return jsonify(usage.summary())


@app.post("/api/alpaca")
def set_alpaca():
    """Save a market-data key (kept in data/, never sent anywhere but Alpaca) and check it with one day of SPY."""
    body = request.get_json(force=True) or {}
    kid, secret = str(body.get("key_id") or "").strip(), str(body.get("secret_key") or "").strip()
    if not kid or not secret:
        return err("enter both the key ID and the secret key")
    alpaca.save_keys(kid, secret)
    day = (date.today() - timedelta(days=100)).isoformat()
    while date.fromisoformat(day).weekday() > 4:
        day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    try:
        got = alpaca.fetch(["SPY"], day, day)
    except alpaca.AlpacaError as e:
        return err(f"Saved, but the check failed: {e}")
    return jsonify({"ok": True, "check": f"{len((got.get('SPY') or {}).get(day, []))} SPY bars for {day}"})


@app.get("/api/runs")
def runs():
    out = []
    for path in sorted(store.RUNS.glob("*/run.json"), reverse=True)[:60]:
        st = store.read_json(path)
        if st:
            out.append(_summary_row(st))
    profiles = sorted(p.name for p in store.PROFILES.iterdir() if (p / "memory.json").exists()) \
        if store.PROFILES.exists() else []
    return jsonify({"runs": out, "profiles": profiles,
                    "job": {"running": _running(), "run_id": _job["run_id"], "lines": list(_job["lines"])[-60:],
                            "error": _job["error"]}})


@app.post("/api/backtest")
def start_backtest():
    if _running():
        return err("A backtest is already running.", 409)
    body = request.get_json(force=True) or {}
    start, end = str(body.get("start", "")), str(body.get("end", ""))
    if not DAY.match(start) or not DAY.match(end) or start > end:
        return err("start and end must be dates, start first")
    save_profile = str(body.get("save_profile") or "").strip().lower() or None
    profile = str(body.get("profile") or "").strip().lower() or None
    for p in (save_profile, profile):
        if p and not PROFILE.match(p):
            return err("profile names are lowercase letters, digits, - and _")
    cfg = config.load()
    for k in ("brain", "model", "effort"):
        if body.get(k):
            cfg[k] = body[k]
    cfg["learn"] = bool(body.get("learn", True))
    try:
        cfg = config.validate(cfg)
        if cfg["brain"] == "claude" and start <= config.cutoff_for(cfg) and not body.get("allow_leaky"):
            raise ValueError(f"{cfg['model']} may remember days up to {config.cutoff_for(cfg)}; start after that.")
    except ValueError as e:
        return err(str(e))

    _job.update({"stop": False, "run_id": None, "started": time.time(), "error": ""})
    _job["lines"].clear()

    def work():
        try:
            backtest.run(cfg, start, end, name=str(body.get("name") or "")[:80], profile=profile,
                         save_profile=save_profile, allow_leaky=bool(body.get("allow_leaky")), log=_log,
                         should_stop=lambda: _job["stop"], on_start=lambda rid: _job.update(run_id=rid))
        except Exception as e:  # shown in the dashboard; the run's own file records the details
            _job["error"] = str(e)
            _log(f"Error: {e}")

    _job["thread"] = threading.Thread(target=work, daemon=True)
    _job["thread"].start()
    return jsonify({"ok": True})


@app.post("/api/stop")
def stop_backtest():
    _job["stop"] = True
    return jsonify({"ok": True, "note": "stops after the current day"})


@app.delete("/api/runs/<run_id>")
def delete_run(run_id):
    if not RUN_ID.match(run_id) or run_id.startswith("live-"):
        return err("bad id")
    if _running() and _job["run_id"] == run_id:
        return err("that run is still going", 409)
    folder = store.RUNS / run_id
    if not (folder / "run.json").exists() or folder.parent != store.RUNS:
        return err("not found", 404)
    shutil.rmtree(folder)
    return jsonify({"ok": True})


@app.get("/api/compare")
def compare_runs():
    ids = [i for i in str(request.args.get("ids", "")).split(",") if i]  # "a+b" joins runs on different days
    parts = [[p for p in i.split("+")] for i in ids]
    if len(ids) < 2 or any(not RUN_ID.match(p) or p.startswith("live-") for ps in parts for p in ps):
        return err("pick two or more runs")
    loaded = [[store.read_json(store.RUNS / p / "run.json") for p in ps] for ps in parts]
    if any(s is None for ss in loaded for s in ss):
        return err("run not found", 404)
    try:
        return jsonify(metrics.compare([metrics.merge_runs(ss) for ss in loaded]))
    except ValueError as e:
        return err(str(e))


@app.get("/report/<run_id>")
def report_page(run_id):
    if not RUN_ID.match(run_id):
        return "bad id", 400
    path = store.LIVE / "report.html" if run_id.startswith("live-") else store.RUNS / run_id / "report.html"
    if not path.exists():
        return "No report yet", 404
    return send_file(path)


@app.get("/api/live")
def live_status():
    profile = request.args.get("profile", "live")
    if not PROFILE.match(profile):
        return err("bad profile")
    folder = store.PROFILES / profile
    playbook, ledgers = [], {}
    if (folder / "memory.json").exists():
        m = Memory(folder)
        playbook = [{k: r[k] for k in ("id", "status", "uses", "sum_r", "helpful", "harmful", "text")}
                    for r in m.active_rules()]
        ledgers = {"track": m.track_text(), "mgmt": m.mgmt_text(), "base_rates": m.base_rates_text(),
                   "lessons": len(m.lessons), "version": m.state["version"]}
    acct = store.read_json(store.LIVE / "account.json") or {}
    return jsonify({"status": store.read_json(store.LIVE / "status.json"), "playbook": playbook, "ledgers": ledgers,
                    "equity": acct.get("equity"), "days": (acct.get("days") or [])[-20:],
                    "has_report": (store.LIVE / "report.html").exists()})


def serve(host="127.0.0.1", port=8050):
    store.ensure_dirs()
    print(f"Dashboard running at http://{host}:{port}  (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=False, threaded=True)
