"""Aggregate data/daytrader/usage.jsonl (one row per Claude call) for the dashboard."""
import time
from collections import defaultdict
from datetime import datetime, timedelta

from . import store


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
        "api_equiv_usd": round(sum(r.get("api_equiv_usd", 0) or 0 for r in rows), 4),
        "avg_latency_s": round(sum(r["latency_s"] for r in ok) / len(ok), 1) if ok else None,
        "avg_tokens": int(sum(_tokens(r) for r in ok) / len(ok)) if ok else None,
    }


def summary(days=14):
    rows = store.read_jsonl(store.USAGE_FILE)
    now = time.time()
    today = datetime.now().date()
    start_today = datetime.combine(today, datetime.min.time()).timestamp()
    per_day = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        lo = datetime.combine(d, datetime.min.time()).timestamp()
        day_rows = [r for r in rows if lo <= r["time"] < lo + 86400]
        per_day.append({"date": d.isoformat(), "calls": len(day_rows), "tokens": sum(_tokens(r) for r in day_rows)})
    by_combo = defaultdict(list)
    for r in rows:
        by_combo[(r.get("model"), r.get("effort"))].append(r)
    by_step = defaultdict(list)
    for r in rows:
        by_step[str(r.get("source", "?")).rsplit("-", 1)[-1]].append(r)
    return {
        "today": _block([r for r in rows if r["time"] >= start_today]),
        "last_5h": _block([r for r in rows if r["time"] >= now - 5 * 3600]),
        "last_7d": _block([r for r in rows if r["time"] >= now - 7 * 86400]),
        "all_time": _block(rows),
        "per_day": per_day,
        "by_model_effort": [{"model": m, "effort": e, **_block(rs)}
                            for (m, e), rs in sorted(by_combo.items(), key=lambda x: -len(x[1]))],
        "by_step": {k: _block(v) for k, v in sorted(by_step.items())},
        "last_limit_error": next((r for r in reversed(rows) if r.get("error_kind") == "limit"), None),
        "recent": list(reversed(rows[-40:])),
    }
