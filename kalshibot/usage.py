"""Aggregate data/usage.jsonl for the dashboard."""
import time
from collections import defaultdict
from datetime import datetime, timedelta

from . import storage


def _tokens(r):
    return (r.get("input_tokens", 0) + r.get("cache_creation_tokens", 0)
            + r.get("cache_read_tokens", 0) + r.get("output_tokens", 0))


def _block(rows):
    ok = [r for r in rows if r.get("ok")]
    return {
        "calls": len(rows),
        "ok": len(ok),
        "errors": len(rows) - len(ok),
        "limit_errors": sum(1 for r in rows if r.get("error_kind") == "limit"),
        "tokens": sum(_tokens(r) for r in rows),
        "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
        "thinking_tokens": sum(r.get("thinking_tokens", 0) for r in rows),
        "api_equiv_usd": round(sum(r.get("api_equiv_usd", 0) or 0 for r in rows), 4),
        "avg_latency_s": round(sum(r["latency_s"] for r in ok) / len(ok), 1) if ok else None,
        "avg_tokens": int(sum(_tokens(r) for r in ok) / len(ok)) if ok else None,
    }


def summary(days=14):
    rows = storage.read_jsonl(storage.USAGE_FILE)
    now = time.time()
    today = datetime.now().date()
    start_today = datetime.combine(today, datetime.min.time()).timestamp()

    per_day = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        lo = datetime.combine(d, datetime.min.time()).timestamp()
        hi = lo + 86400
        day_rows = [r for r in rows if lo <= r["time"] < hi]
        per_day.append({"date": d.isoformat(), "calls": len(day_rows), "tokens": sum(_tokens(r) for r in day_rows)})

    by_combo = defaultdict(list)
    for r in rows:
        by_combo[(r.get("model"), r.get("effort"))].append(r)
    combos = [{"model": m, "effort": e, **_block(rs)} for (m, e), rs in sorted(by_combo.items(), key=lambda x: -len(x[1]))]

    by_source = defaultdict(list)
    for r in rows:
        by_source[r.get("source", "?")].append(r)

    last_limit = next((r for r in reversed(rows) if r.get("error_kind") == "limit"), None)
    return {
        "today": _block([r for r in rows if r["time"] >= start_today]),
        "last_5h": _block([r for r in rows if r["time"] >= now - 5 * 3600]),
        "last_7d": _block([r for r in rows if r["time"] >= now - 7 * 86400]),
        "all_time": _block(rows),
        "per_day": per_day,
        "by_model_effort": combos,
        "by_source": {k: _block(v) for k, v in by_source.items()},
        "last_limit_error": last_limit,
        "recent": list(reversed(rows[-40:])),
    }
