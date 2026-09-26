"""File locations and small JSON helpers. Everything lives under data/."""
import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
BACKTESTS = DATA / "backtests"
LIVE = DATA / "live"
SETTINGS_FILE = DATA / "settings.json"
USAGE_FILE = DATA / "usage.jsonl"
DECISION_CACHE_FILE = CACHE / "decisions.jsonl"

_write_lock = threading.Lock()


def ensure_dirs():
    for d in (DATA, CACHE, BACKTESTS, LIVE, CACHE / "kalshi", CACHE / "coinbase"):
        d.mkdir(parents=True, exist_ok=True)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _write_lock:
        tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
        tmp.replace(path)


def append_jsonl(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a half-written line from a crash
    return rows
