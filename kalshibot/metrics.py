"""Scoring that separates edge from luck."""
import math
import random


def wilson(k, n, z=1.96):
    if n == 0:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


def brier(pairs):
    """pairs: [(prob_up, outcome_up_bool)]"""
    pairs = [(p, o) for p, o in pairs if p is not None]
    if not pairs:
        return None
    return sum((p - (1.0 if o else 0.0)) ** 2 for p, o in pairs) / len(pairs)


def log_loss(pairs):
    pairs = [(p, o) for p, o in pairs if p is not None]
    if not pairs:
        return None
    eps = 1e-4
    return -sum(math.log(min(1 - eps, max(eps, p if o else 1 - p))) for p, o in pairs) / len(pairs)


def calibration(pairs, bins=10):
    buckets = [[] for _ in range(bins)]
    for p, o in pairs:
        if p is None:
            continue
        buckets[min(bins - 1, int(p * bins))].append((p, o))
    out = []
    for b in buckets:
        if b:
            out.append({"predicted": sum(p for p, _ in b) / len(b),
                        "actual": sum(1 for _, o in b if o) / len(b), "n": len(b)})
    return out


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


def summarize(decisions, trades, curve, bankroll):
    """decisions: [{probability_up, market_up, actual_up, direction}], trades: closed trades."""
    pairs = [(d.get("probability_up"), d["actual_up"]) for d in decisions]
    market_pairs = [(d.get("market_up"), d["actual_up"]) for d in decisions]
    calls = [d for d in decisions if d["direction"] in ("UP", "DOWN")]
    hits = sum(1 for d in calls if (d["direction"] == "UP") == d["actual_up"])

    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for t in trades if t["won"])
    staked = sum(t["cost"] for t in trades)
    total = sum(pnls)
    lo, hi = wilson(wins, len(trades))
    mlo, mhi = bootstrap_mean_ci(pnls)
    dd, dd_pct = max_drawdown(curve, bankroll)
    mean = total / len(pnls) if pnls else 0
    sd = math.sqrt(sum((x - mean) ** 2 for x in pnls) / (len(pnls) - 1)) if len(pnls) > 1 else 0
    avg_price = staked / sum(t["contracts"] for t in trades) if trades else None

    if len(trades) < 30:
        verdict = "Too few trades to tell skill from luck (aim for 100+)."
    elif mlo is not None and mlo > 0:
        verdict = "Profit per trade is above zero with 95% confidence: a possible edge. Confirm on a fresh date range."
    elif mhi is not None and mhi < 0:
        verdict = "Losing with 95% confidence: this setup has negative edge."
    else:
        verdict = "Results are within the range of luck: no proven edge yet."

    s_brier, m_brier = brier(pairs), brier(market_pairs)
    return {
        "markets": len(decisions),
        "calls": len(calls),
        "skips": len(decisions) - len(calls),
        "call_accuracy": hits / len(calls) if calls else None,
        "trades": len(trades),
        "wins": wins,
        "win_rate": wins / len(trades) if trades else None,
        "win_rate_ci": [lo, hi],
        "avg_price": avg_price,
        "net_pnl": round(total, 2),
        "fees": round(sum(t["fees"] for t in trades), 2),
        "staked": round(staked, 2),
        "return_on_stake": total / staked if staked else None,
        "return_on_bankroll": total / bankroll,
        "ending_equity": round(bankroll + total, 2),
        "pnl_per_trade": mean if pnls else None,
        "pnl_per_trade_ci": [mlo, mhi],
        "sharpe_per_trade": mean / sd if sd else None,
        "max_drawdown": round(dd, 2),
        "max_drawdown_pct": dd_pct,
        "brier": s_brier,
        "brier_market": m_brier,
        "brier_skill_vs_market": (1 - s_brier / m_brier) if s_brier is not None and m_brier else None,
        "log_loss": log_loss(pairs),
        "calibration": calibration(pairs),
        "verdict": verdict,
    }
