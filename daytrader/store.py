"""File locations and JSON helpers for the day trader. Everything lives under data/daytrader/."""
import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "daytrader"
CACHE = DATA / "cache"
RUNS = DATA / "runs"
PROFILES = DATA / "profiles"
LIVE = DATA / "live"
SETTINGS_FILE = DATA / "settings.json"
LLM_CACHE = CACHE / "llm.jsonl"
USAGE_FILE = DATA / "usage.jsonl"  # one row per Claude call: tokens, latency, errors

_lock = threading.Lock()


def ensure_dirs():
    for d in (DATA, CACHE, RUNS, PROFILES, LIVE):
        d.mkdir(parents=True, exist_ok=True)


def _retry(fn, attempts):
    # Windows refuses opens and swaps while another process holds the file; elsewhere it is permanent.
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if os.name != "nt" or i == attempts - 1:
                raise
            time.sleep(0.05)


def read_json(path, default=None, strict=False):
    """Parsed JSON, or `default` if the file is missing.

    strict=True raises on an unreadable or corrupt file instead of returning `default`, for state
    (memory, accounts) that a caller would otherwise overwrite with a fresh copy.
    """
    path = Path(path)
    try:
        text = _retry(lambda: path.read_text(encoding="utf-8"), 20)
    except FileNotFoundError:
        return default
    except OSError:
        if strict:
            raise
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if strict:
            raise
        return default


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    with _lock:
        tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
        try:
            _retry(lambda: tmp.replace(path), 40)
        except PermissionError:
            tmp.unlink(missing_ok=True)
            raise


def append_jsonl(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")


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
