"""Counterfactual replays, run after the close on that day's bars.

Two questions the real account can't answer on its own, because it only makes two or three trades a day:

- What would each plan have done if nobody had touched it? Comparing that with what actually happened
  measures whether check-in interventions (early exits, stop moves, cancels) help or hurt.
- What did simple mechanical setups do on the scanner's names today? A few dozen code-simulated trades a
  day give the trader base rates for its environment (are opening-range breakouts working this month?
  are gap fades?) far faster than its own trades can.

Both go through the same conservative Broker as real orders: fills after the trigger plus slippage, the
stop wins same-bar ties, no target on the entry bar, gaps fill at the open. They are computed after the
day is over and only shown on later days, so they add evidence without adding look-ahead.
"""
from . import clock

TICK = 0.01
# A desk with room for any one order: R doesn't depend on share count, and nothing else may block the fill.
_SIM = {"max_positions": 99, "max_position_pct": 1e9, "max_leverage": 1e9, "max_daily_loss_pct": 0,
        "allow_short": True, "commission_per_share": 0.0, "commission_min": 0.0}


def simulate(order, bars, cfg, flatten_ts):
    """Run one order alone through a fresh broker over `bars` (regular-session bars of its stock) and close
    any position at the first bar at or after `flatten_ts`. Returns None if it never filled, else
    {'r', 'exit_reason', 'mfe_r', 'mae_r', 'entry_ts'}."""
    from .broker import Broker
    b = Broker({**cfg, **_SIM}, 1e7)
    o, err = b.submit(dict(order), order.get("active_from") or 0)
    if err:
        return None
    tk = o["ticker"]
    for bar in bars:
        if bar[0] >= flatten_ts:
            break
        b.on_bar(tk, bar)
        if not b.working(tk) and not b.positions:
            break  # expired, cancelled, or the trade is already over
    if b.positions:
        nxt = next((x for x in bars if x[0] >= flatten_ts), None)
        b.flatten(flatten_ts, {tk: nxt[1] if nxt else b.last_price.get(tk)}, "end of day")
    if not b.trades:
        return None
    t = b.trades[0]
    risk = abs(t["entry"] - t["initial_stop"]) or None
    return {"r": t["r"], "exit_reason": t["exit_reason"], "entry_ts": t["entry_ts"],
            "mfe_r": round(t["mfe"] / risk, 2) if risk else None, "mae_r": round(t["mae"] / risk, 2) if risk else None}


def _ok(entry, stop):
    return entry > 0 and 0.001 <= abs(entry - stop) / entry <= 0.08


def mechanical_orders(tk, day, reg, pm, daily, interval_s, flatten_ts):
    """Orders for each mechanical setup on one stock. Every level comes from bars that had finished before
    the order becomes active, so each setup could have been traded live exactly as written."""
    out = []
    if not reg or not daily or not pm or pm.get("gap_pct") is None or abs(pm["gap_pct"]) < 0.3:
        return out
    up = pm["gap_pct"] > 0
    n_or = max(1, 900 // interval_s)
    or_end = clock.at(day, "09:45")
    orb = [b for b in reg if b[0] < or_end][:n_or]
    if len(orb) == n_or and or_end < flatten_ts:
        hi, lo = max(b[2] for b in orb), min(b[3] for b in orb)
        for name, long in (("orb_with_gap", up), ("orb_against_gap", not up)):
            entry = hi + TICK if long else lo - TICK
            stop = lo - TICK if long else hi + TICK
            if _ok(entry, stop):
                risk = abs(entry - stop)
                out.append((name, {"ticker": tk, "side": "long" if long else "short", "kind": "stop",
                                   "price": round(entry, 2), "stop": round(stop, 2),
                                   "target": round(entry + 2 * risk if long else entry - 2 * risk, 2),
                                   "active_from": or_end, "expires": min(clock.at(day, "11:30"), flatten_ts)}))
    atr = daily.get("atr")
    level = pm.get("pm_high") if up else pm.get("pm_low")
    if atr and level:
        entry = level + TICK if up else level - TICK
        risk = 0.35 * atr
        stop = entry - risk if up else entry + risk
        if _ok(entry, stop):
            out.append(("pm_break", {"ticker": tk, "side": "long" if up else "short", "kind": "stop",
                                     "price": round(entry, 2), "stop": round(stop, 2),
                                     "target": round(entry + 2 * risk if up else entry - 2 * risk, 2),
                                     "active_from": clock.at(day, "09:35"),
                                     "expires": min(clock.at(day, "11:00"), flatten_ts)}))
    t1030 = clock.at(day, "10:30")
    seen = [b for b in reg if b[0] + interval_s <= t1030]
    if seen and t1030 < flatten_ts and seen[-1][4] != reg[0][1]:
        long = seen[-1][4] > reg[0][1]
        stop = min(b[3] for b in seen) - TICK if long else max(b[2] for b in seen) + TICK
        if _ok(seen[-1][4], stop):
            out.append(("trend_1030", {"ticker": tk, "side": "long" if long else "short", "kind": "market",
                                       "price": None, "stop": round(stop, 2), "target": None,
                                       "active_from": t1030, "expires": t1030 + 2 * interval_s}))
    return out


def base_rates(day, rows, bars_of, cfg, interval_s, flatten_ts):
    """Replay every mechanical setup on each scanner row. `rows` are scan rows (ticker, daily, pm);
    `bars_of(tk)` returns that stock's regular-session bars for the day. Returns one sample per fill."""
    from .memory import gap_bucket
    out = []
    for r in rows:
        tk, reg = r["ticker"], bars_of(r["ticker"])
        for setup, order in mechanical_orders(tk, day, reg, r.get("pm"), r.get("daily"), interval_s, flatten_ts):
            res = simulate(order, reg, cfg, flatten_ts)
            if res and res["r"] is not None:
                out.append({"day": day, "ticker": tk, "setup": setup, "side": order["side"], "r": res["r"],
                            "gap": gap_bucket((r.get("pm") or {}).get("gap_pct")), "exit": res["exit_reason"]})
    return out
