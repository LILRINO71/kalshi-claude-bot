"""Scoring that separates an edge from luck, checks whether learning helped, and compares runs."""
import math
import random

from . import regime


def wilson(k, n, z=1.96):
    if n == 0:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


def bootstrap_mean_ci(values, iters=2000, seed=7):
    if len(values) < 2:
        return None, None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(iters))
    return means[int(0.025 * iters)], means[int(0.975 * iters) - 1]


def max_drawdown(curve, start):
    peak, worst, worst_pct = start, 0.0, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        dd = peak - eq
        if dd > worst:
            worst, worst_pct = dd, dd / peak if peak else 0
    return worst, worst_pct


def _sd(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _block(trades):
    if not trades:
        return {"trades": 0, "win_rate": None, "avg_r": None, "pnl": 0.0, "pnl_per_trade": None}
    wins = sum(1 for t in trades if t["pnl"] > 0)
    rs = [t["r"] for t in trades if t.get("r") is not None]
    pnl = sum(t["pnl"] for t in trades)
    return {"trades": len(trades), "win_rate": wins / len(trades), "avg_r": sum(rs) / len(rs) if rs else None,
            "pnl": round(pnl, 2), "pnl_per_trade": pnl / len(trades)}


def trades_needed(rs):
    """Rough number of trades at this average and spread before a 95% interval would exclude zero."""
    if len(rs) < 5:
        return None
    m, s = sum(rs) / len(rs), _sd(rs)
    if m <= 0 or s <= 0:
        return None
    return int(math.ceil((1.96 * s / m) ** 2))


def verdict(rs, lo, hi):
    if len(rs) < 30:
        return "Too few trades to tell skill from luck (aim for 100+ over a few months)."
    if lo is not None and lo > 0:
        return "Average R is above zero with 95% confidence: a possible edge. Confirm it in live paper trading."
    if hi is not None and hi < 0:
        return "Losing with 95% confidence: as it stands, this has a negative edge."
    need = trades_needed(rs)
    tail = f" At this average and spread it would take about {need} trades to know." if need else ""
    return "Results are within the range of luck: no proven edge yet." + tail


def learning_curve(days, trades, block_days=5):
    """Average R per block of trading days, to see whether results improve as the playbook evolves."""
    out = []
    for i in range(0, len(days), block_days):
        chunk = days[i:i + block_days]
        names = {d["day"] for d in chunk}
        tr = [t for t in trades if t["day"] in names]
        out.append({"start": chunk[0]["day"], "end": chunk[-1]["day"], **_block(tr)})
    return out


def management(days):
    """What check-in interventions were worth compared with leaving each plan alone, by kind of action."""
    groups = {}
    for d in days:
        for m in d.get("mgmt") or []:
            g = groups.setdefault(m["action"], {"n": 0, "delta_r": 0.0})
            g["n"] += 1
            g["delta_r"] = round(g["delta_r"] + m["delta_r"], 3)
    return groups


def base_rates(days):
    """Mechanical setups replayed on the scanner's top names each day: count, average R, win rate."""
    groups = {}
    for d in days:
        for b in d.get("base_rates") or []:
            groups.setdefault(b["setup"], []).append(b["r"])
    return {k: {"n": len(v), "avg_r": sum(v) / len(v), "win_rate": sum(1 for r in v if r > 0) / len(v)}
            for k, v in sorted(groups.items())}


def by_market_regime(days, trades, gauges):
    """Results split by how volatile each day was (calm / normal / volatile, see regime.py)."""
    out = {}
    for lab in ("calm", "normal", "volatile"):
        names = {d["day"] for d in days if (gauges.get(d["day"]) or {}).get("label") == lab}
        if not names:
            continue
        base = [b["r"] for d in days if d["day"] in names for b in d.get("base_rates") or []]
        out[lab] = {"days": len(names), **_block([t for t in trades if t["day"] in names]),
                    "day_pnl": round(sum(d["pnl"] for d in days if d["day"] in names), 2),
                    "base_avg_r": sum(base) / len(base) if base else None, "base_n": len(base)}
    return out


def summarize(state, data=None):
    cfg = state["config"]
    bank = cfg["bankroll"]
    days, trades = state["days"], state["trades"]
    curve = state["equity_curve"]
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    rs = [t["r"] for t in trades if t.get("r") is not None]
    end = days[-1]["end_equity"] if days else bank
    daily = [d["pnl"] / d["start_equity"] for d in days if d["start_equity"]]
    mean_d = sum(daily) / len(daily) if daily else 0
    sd_d = _sd(daily)
    dd, dd_pct = max_drawdown(curve[1:], bank) if curve else (0, 0)
    lo, hi = wilson(wins, len(trades))
    mlo, mhi = bootstrap_mean_ci(pnls)
    rlo, rhi = bootstrap_mean_ci(rs)

    bench = None
    if data and days:
        try:
            spy = {d[0]: d for d in data.daily_all("SPY")}
            first, last = days[0]["day"], days[-1]["day"]
            prev = [k for k in sorted(spy) if k < first]
            if prev and last in spy:
                bench = (spy[last][4] / spy[prev[-1]][4] - 1)
        except Exception:
            bench = None

    market, by_regime = None, {}
    if data and days:
        try:
            g = regime.gauges(data, cfg.get("universe") or None)
            market = regime.summarize_days(g, [d["day"] for d in days])
            by_regime = by_market_regime(days, trades, g)
        except Exception:
            market, by_regime = None, {}

    half = len(days) // 2
    early_days = {d["day"] for d in days[:half]}
    early = [t for t in trades if t["day"] in early_days]
    late = [t for t in trades if t["day"] not in early_days]

    return {
        "days": len(days), "days_traded": sum(1 for d in days if d["trades"]), "trades": len(trades),
        "wins": wins, "win_rate": wins / len(trades) if trades else None, "win_rate_ci": [lo, hi],
        "net_pnl": round(sum(pnls), 2), "return_pct": (end / bank - 1) * 100, "ending_equity": round(end, 2),
        "avg_r": sum(rs) / len(rs) if rs else None, "avg_r_ci": [rlo, rhi], "total_r": round(sum(rs), 2) if rs else 0,
        "trades_needed": trades_needed(rs),
        "profit_factor": gross_win / gross_loss if gross_loss else None,
        "pnl_per_trade": sum(pnls) / len(pnls) if pnls else None, "pnl_per_trade_ci": [mlo, mhi],
        "sharpe_daily_annualized": mean_d / sd_d * math.sqrt(252) if sd_d else None,
        "max_drawdown": round(dd, 2), "max_drawdown_pct": dd_pct * 100,
        "fees": round(sum(t.get("fees", 0) for t in trades), 2),
        "benchmark_spy_pct": bench * 100 if bench is not None else None,
        "learning": {"first_half": _block(early), "second_half": _block(late)},
        "learning_curve": learning_curve(days, trades),
        "management": management(days),
        "base_rates": base_rates(days),
        "market": market, "by_regime": by_regime,
        "by_setup": _by(trades, "setup"), "by_side": _by(trades, "side"), "by_exit": _by(trades, "exit_reason"),
        "llm_calls": sum(d.get("calls", {}).get("made", 0) for d in days),
        "llm_cached": sum(d.get("calls", {}).get("cached", 0) for d in days),
        "llm_errors": sum(d.get("calls", {}).get("errors", 0) for d in days),
        "verdict": verdict(rs, rlo, rhi),
    }


def _by(trades, key):
    groups = {}
    for t in trades:
        groups.setdefault(str(t.get(key) or "?")[:40], []).append(t)
    return {k: _block(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:12]}


def merge_runs(states):
    """Join runs that cover different days into one, e.g. a frozen (non-learning) run done in two parts.
    Only sensible for runs that don't learn, or for one learning run continued from another's profile."""
    if len(states) == 1:
        return states[0]
    days = sorted((d for s in states for d in s["days"]), key=lambda d: d["day"])
    if len({d["day"] for d in days}) != len(days):
        raise ValueError("the runs being joined overlap; join runs that cover different days")
    return {**states[0], "id": "+".join(s["id"] for s in states),
            "name": " + ".join(s.get("name") or s["id"] for s in states), "days": days,
            "trades": sorted((t for s in states for t in s["trades"]), key=lambda t: (t["day"], t.get("entry_ts", 0)))}


def compare(states):
    """Line runs up on the trading days they all simulated. The first run is the baseline; for every other
    run, the paired difference in daily P&L (% of the day's starting equity) gets a bootstrap interval,
    which is the fair way to ask "did B beat A on the same days?"."""
    if len(states) < 2:
        raise ValueError("compare needs at least two runs")
    common = set.intersection(*({d["day"] for d in s["days"]} for s in states))
    days = sorted(common)
    rows = []
    for s in states:
        by_day = {d["day"]: d for d in s["days"]}
        tr = [t for t in s["trades"] if t["day"] in common]
        rs = [t["r"] for t in tr if t.get("r") is not None]
        rows.append({
            "id": s["id"], "name": s.get("name") or "", "brain": s["config"]["brain"],
            "engine": (f"{s['config']['model']}/{s['config']['effort']}" if s["config"]["brain"] == "claude"
                       else s["config"]["brain"]) + ("" if s["config"].get("learn", True) else " frozen"),
            "days": len(days), "trades": len(tr), "avg_r": sum(rs) / len(rs) if rs else None,
            "total_r": round(sum(rs), 2), "pnl": round(sum(by_day[d]["pnl"] for d in days), 2),
            "daily_pct": [by_day[d]["pnl"] / by_day[d]["start_equity"] * 100 if by_day[d]["start_equity"] else 0
                          for d in days],
        })
    base = rows[0]
    for r in rows[1:]:
        diffs = [b - a for a, b in zip(base["daily_pct"], r["daily_pct"])]
        lo, hi = bootstrap_mean_ci(diffs)
        mean = sum(diffs) / len(diffs) if diffs else None
        if lo is not None and lo > 0:
            word = "better than the baseline with 95% confidence"
        elif hi is not None and hi < 0:
            word = "worse than the baseline with 95% confidence"
        else:
            word = "not distinguishable from the baseline yet"
        r["vs_baseline"] = {"mean_daily_pct_diff": mean, "ci": [lo, hi], "verdict": word}
    for r in rows:
        r.pop("daily_pct")
    return {"days": days, "runs": rows}
