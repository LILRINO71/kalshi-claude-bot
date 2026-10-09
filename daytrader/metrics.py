"""Scoring that separates an edge from luck, and checks whether learning helped."""
import math

from kalshibot.metrics import bootstrap_mean_ci, max_drawdown, wilson


def _block(trades):
    if not trades:
        return {"trades": 0, "win_rate": None, "avg_r": None, "pnl": 0.0, "pnl_per_trade": None}
    wins = sum(1 for t in trades if t["pnl"] > 0)
    rs = [t["r"] for t in trades if t.get("r") is not None]
    pnl = sum(t["pnl"] for t in trades)
    return {"trades": len(trades), "win_rate": wins / len(trades), "avg_r": sum(rs) / len(rs) if rs else None,
            "pnl": round(pnl, 2), "pnl_per_trade": pnl / len(trades)}


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
    sd_d = math.sqrt(sum((x - mean_d) ** 2 for x in daily) / (len(daily) - 1)) if len(daily) > 1 else 0
    dd, dd_pct = max_drawdown(curve[1:], bank) if curve else (0, 0)
    lo, hi = wilson(wins, len(trades))
    mlo, mhi = bootstrap_mean_ci(pnls)

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

    half = len(days) // 2
    early_days = {d["day"] for d in days[:half]}
    early = [t for t in trades if t["day"] in early_days]
    late = [t for t in trades if t["day"] not in early_days]

    if len(trades) < 30:
        verdict = "Too few trades to tell skill from luck (aim for 100+ over a few months)."
    elif mlo is not None and mlo > 0:
        verdict = "Profit per trade is above zero with 95% confidence: a possible edge. Confirm on new dates."
    elif mhi is not None and mhi < 0:
        verdict = "Losing with 95% confidence: this setup has a negative edge."
    else:
        verdict = "Results are within the range of luck: no proven edge yet."

    return {
        "days": len(days), "days_traded": sum(1 for d in days if d["trades"]), "trades": len(trades),
        "wins": wins, "win_rate": wins / len(trades) if trades else None, "win_rate_ci": [lo, hi],
        "net_pnl": round(sum(pnls), 2), "return_pct": (end / bank - 1) * 100, "ending_equity": round(end, 2),
        "avg_r": sum(rs) / len(rs) if rs else None, "total_r": round(sum(rs), 2) if rs else 0,
        "profit_factor": gross_win / gross_loss if gross_loss else None,
        "pnl_per_trade": sum(pnls) / len(pnls) if pnls else None, "pnl_per_trade_ci": [mlo, mhi],
        "sharpe_daily_annualized": mean_d / sd_d * math.sqrt(252) if sd_d else None,
        "max_drawdown": round(dd, 2), "max_drawdown_pct": dd_pct * 100,
        "fees": round(sum(t.get("fees", 0) for t in trades), 2),
        "benchmark_spy_pct": bench * 100 if bench is not None else None,
        "learning": {"first_half": _block(early), "second_half": _block(late)},
        "by_setup": _by(trades, "setup"), "by_side": _by(trades, "side"), "by_exit": _by(trades, "exit_reason"),
        "llm_calls": sum(d.get("calls", {}).get("made", 0) for d in days),
        "llm_cached": sum(d.get("calls", {}).get("cached", 0) for d in days),
        "llm_errors": sum(d.get("calls", {}).get("errors", 0) for d in days),
        "verdict": verdict,
    }


def _by(trades, key):
    groups = {}
    for t in trades:
        groups.setdefault(str(t.get(key) or "?")[:40], []).append(t)
    return {k: _block(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:12]}
