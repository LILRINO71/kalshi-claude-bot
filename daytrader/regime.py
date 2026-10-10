"""How volatile the market was, day by day, from daily bars only (no look-ahead concerns: used for
reporting and for choosing test windows, never shown to the trader).

Three gauges: the VIX close, SPY's day range as a % of the previous close, and the share of the scanner
universe that moved 5% or more close to close. A day is "volatile" if any gauge is high, "calm" if all
are low, and "normal" otherwise.
"""
import statistics as st

from .universe import STOCKS

VOLATILE = {"vix": 20.0, "spy_range": 1.3, "movers": 15.0}
CALM = {"vix": 16.0, "spy_range": 0.8, "movers": 8.0}


def label(g):
    if g["vix"] is None:
        return "unknown"
    if g["vix"] >= VOLATILE["vix"] or g["spy_range"] >= VOLATILE["spy_range"] or g["movers"] >= VOLATILE["movers"]:
        return "volatile"
    if g["vix"] < CALM["vix"] and g["spy_range"] < CALM["spy_range"] and g["movers"] < CALM["movers"]:
        return "calm"
    return "normal"


def gauges(data, universe=None):
    """{day: {'vix', 'spy_range', 'spy_move', 'movers', 'label'}} for every day SPY has a daily bar."""
    spy = data.daily_all("SPY")
    vix = {r[0]: r[4] for r in data.daily_all("^VIX")}
    moves = {}
    for tk in universe or STOCKS:
        rows = data.daily_all(tk)
        for prev, cur in zip(rows, rows[1:]):
            if prev[4]:
                moves.setdefault(cur[0], []).append(abs(cur[4] / prev[4] - 1) * 100)
    out = {}
    for prev, cur in zip(spy, spy[1:]):
        day = cur[0]
        ms = moves.get(day) or []
        g = {"vix": vix.get(day), "spy_range": (cur[2] - cur[3]) / prev[4] * 100,
             "spy_move": (cur[4] / prev[4] - 1) * 100,
             "movers": sum(1 for m in ms if m >= 5) / len(ms) * 100 if len(ms) >= 20 else 0.0}
        g["label"] = label(g)
        out[day] = g
    return out


def summarize_days(g, days):
    rows = [g[d] for d in days if d in g]
    if not rows:
        return None
    vix = [r["vix"] for r in rows if r["vix"] is not None]
    counts = {k: sum(1 for r in rows if r["label"] == k) for k in ("calm", "normal", "volatile")}
    return {"days": len(rows), "vix": st.mean(vix) if vix else None, "spy_range": st.mean(r["spy_range"] for r in rows),
            "movers": st.mean(r["movers"] for r in rows), **counts}


def monthly(g):
    """Rows per month for `python -m daytrader regimes`."""
    by = {}
    for day, r in sorted(g.items()):
        by.setdefault(day[:7], []).append(day)
    return [{"month": m, **summarize_days(g, days)} for m, days in by.items()]
