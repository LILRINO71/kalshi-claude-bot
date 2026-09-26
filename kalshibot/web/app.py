"""Local dashboard: python -m kalshibot dashboard, then open http://127.0.0.1:8050"""
import re

from flask import Flask, jsonify, render_template, request

from .. import backtest, claude_cli, config, storage, usage
from ..live import get_trader

app = Flask(__name__)
RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")


def err(msg, code=400):
    return jsonify({"error": msg}), code


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/meta")
def meta():
    return jsonify({
        "models": config.MODELS, "efforts": config.EFFORTS, "strategies": config.STRATEGIES,
        "sizing": config.SIZING, "assets": config.ASSETS, "defaults": config.DEFAULTS,
        "labels": config.LABELS, "indices": list(config.INDICES),
        "claude_path": claude_cli.find_claude(),
    })


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


@app.post("/api/backtests/estimate")
def bt_estimate():
    try:
        return jsonify(backtest.estimate(request.get_json(force=True) or {}))
    except ValueError as e:
        return err(str(e))


@app.post("/api/backtests")
def bt_start():
    try:
        run = backtest.start(request.get_json(force=True) or {})
        return jsonify(run.info())
    except (ValueError, RuntimeError) as e:
        return err(str(e), 409 if isinstance(e, RuntimeError) else 400)


@app.get("/api/backtests")
def bt_list():
    return jsonify(backtest.list_saved())


@app.get("/api/backtests/<run_id>")
def bt_get(run_id):
    if not RUN_ID.match(run_id):
        return err("bad id")
    live = backtest.status(run_id)
    if live and live["status"] in ("queued", "loading", "deciding", "simulating"):
        return jsonify(live)
    data = backtest.load_saved(run_id)
    return jsonify(data) if data else (jsonify(live) if live else err("not found", 404))


@app.post("/api/backtests/<run_id>/cancel")
def bt_cancel(run_id):
    return jsonify({"ok": backtest.cancel(run_id)})


@app.delete("/api/backtests/<run_id>")
def bt_delete(run_id):
    if not RUN_ID.match(run_id):
        return err("bad id")
    return jsonify({"ok": backtest.delete_saved(run_id)})


@app.get("/api/live")
def live_status():
    return jsonify(get_trader().status())


@app.post("/api/live/<action>")
def live_action(action):
    t = get_trader()
    try:
        if action == "start":
            return jsonify({"ok": t.start()})
        if action == "stop":
            return jsonify({"ok": t.stop()})
        if action == "reset":
            t.reset()
            return jsonify({"ok": True})
    except RuntimeError as e:
        return err(str(e), 409)
    return err("unknown action", 404)


def serve(host="127.0.0.1", port=8050):
    storage.ensure_dirs()
    print(f"Dashboard running at http://{host}:{port}  (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=False, threaded=True)
