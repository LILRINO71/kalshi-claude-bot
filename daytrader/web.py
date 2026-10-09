"""Dashboard routes for the day trader (registered on the Kalshi desk's Flask app)."""
import re
import threading
import time
from collections import deque

from flask import Blueprint, jsonify, request, send_file

from . import backtest, config, store
from .memory import Memory

bp = Blueprint("daytrader", __name__)
RUN_ID = re.compile(r"^(\d{8}-\d{6}-[0-9a-f]{4}|live-[a-z0-9_-]{1,40})$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROFILE = re.compile(r"^[a-z0-9_-]{1,40}$")

_job = {"thread": None, "stop": False, "lines": deque(maxlen=200), "run_id": None, "started": None, "error": ""}


def _log(msg):
    _job["lines"].append(f"{time.strftime('%H:%M:%S')}  {msg}")


def _summary_row(st):
    s = st.get("summary") or {}
    c = st.get("config") or {}
    return {"id": st.get("id"), "name": st.get("name"), "status": st.get("status"), "start": st.get("start"),
            "end": st.get("end"), "brain": c.get("brain"), "model": c.get("model"), "effort": c.get("effort"),
            "learn": c.get("learn"), "leaky": st.get("leaky"), "days": s.get("days", len(st.get("days") or [])),
            "trades": s.get("trades"), "net_pnl": s.get("net_pnl"), "return_pct": s.get("return_pct"),
            "spy_pct": s.get("benchmark_spy_pct"), "avg_r": s.get("avg_r"), "win_rate": s.get("win_rate"),
            "verdict": s.get("verdict"), "created": st.get("created")}


@bp.get("/api/daytrader/runs")
def runs():
    out = []
    for path in sorted(store.RUNS.glob("*/run.json"), reverse=True)[:50]:
        st = store.read_json(path)
        if st:
            out.append(_summary_row(st))
    running = bool(_job["thread"] and _job["thread"].is_alive())
    return jsonify({"runs": out, "job": {"running": running, "run_id": _job["run_id"], "lines": list(_job["lines"])[-40:],
                                         "error": _job["error"]}, "defaults": config.load()})


@bp.post("/api/daytrader/backtest")
def start_backtest():
    if _job["thread"] and _job["thread"].is_alive():
        return jsonify({"error": "A day-trader backtest is already running."}), 409
    body = request.get_json(force=True) or {}
    start, end = str(body.get("start", "")), str(body.get("end", ""))
    if not DAY.match(start) or not DAY.match(end) or start > end:
        return jsonify({"error": "start and end must be dates, start first"}), 400
    save_profile = str(body.get("save_profile") or "").strip().lower() or None
    if save_profile and not PROFILE.match(save_profile):
        return jsonify({"error": "profile names are lowercase letters, digits, - and _"}), 400
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
        return jsonify({"error": str(e)}), 400

    _job.update({"stop": False, "run_id": None, "started": time.time(), "error": ""})
    _job["lines"].clear()

    def work():
        try:
            backtest.run(cfg, start, end, name=str(body.get("name") or "")[:80], save_profile=save_profile,
                         allow_leaky=bool(body.get("allow_leaky")), log=_log, should_stop=lambda: _job["stop"],
                         on_start=lambda rid: _job.update(run_id=rid))
        except Exception as e:  # shown in the dashboard; the run's own file records the details
            _job["error"] = str(e)
            _log(f"Error: {e}")

    _job["thread"] = threading.Thread(target=work, daemon=True)
    _job["thread"].start()
    return jsonify({"ok": True})


@bp.post("/api/daytrader/stop")
def stop_backtest():
    _job["stop"] = True
    return jsonify({"ok": True, "note": "stops after the current day"})


@bp.get("/daytrader/report/<run_id>")
def report_page(run_id):
    if not RUN_ID.match(run_id):
        return "bad id", 400
    path = store.LIVE / "report.html" if run_id.startswith("live-") else store.RUNS / run_id / "report.html"
    if not path.exists():
        return "No report yet", 404
    return send_file(path)


@bp.get("/api/daytrader/live")
def live_status():
    profile = request.args.get("profile", "live")
    if not PROFILE.match(profile):
        return jsonify({"error": "bad profile"}), 400
    folder = store.PROFILES / profile
    playbook = []
    if (folder / "memory.json").exists():
        m = Memory(folder)
        playbook = [{k: r[k] for k in ("id", "status", "uses", "sum_r", "helpful", "harmful", "text")}
                    for r in m.active_rules()]
    acct = store.read_json(store.LIVE / "account.json") or {}
    return jsonify({"status": store.read_json(store.LIVE / "status.json"), "playbook": playbook,
                    "equity": acct.get("equity"), "days": (acct.get("days") or [])[-20:],
                    "has_report": (store.LIVE / "report.html").exists()})
