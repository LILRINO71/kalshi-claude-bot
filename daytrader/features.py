"""Numbers a trader looks at, computed only from data the view allows (nothing after `now`)."""
import math

from . import clock


def daily_stats(daily):
    """daily: [(day, o, h, l, c, v)] oldest first, all completed sessions before today."""
    if len(daily) < 2:
        return None
    closes = [d[4] for d in daily]
    trs = []
    for i in range(1, len(daily)):
        _, _, h, l, _, _ = daily[i]
        pc = daily[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[-14:]) / len(trs[-14:])
    last = daily[-1]
    vols = [d[5] for d in daily[-20:] if d[5]]

    def ret(n):
        return (closes[-1] / closes[-1 - n] - 1) * 100 if len(closes) > n else None

    return {
        "prev_day": last[0], "prev_open": last[1], "prev_high": last[2], "prev_low": last[3], "prev_close": last[4],
        "prev_volume": last[5], "atr": atr, "atr_pct": atr / last[4] * 100,
        "avg_volume": sum(vols) / len(vols) if vols else 0.0,
        "ret_1d": ret(1), "ret_5d": ret(5), "ret_20d": ret(20),
        "high_20d": max(d[2] for d in daily[-20:]), "low_20d": min(d[3] for d in daily[-20:]),
        "closes_10d": [round(c, 2) for c in closes[-10:]],
    }


def split_session(bars, day):
    """(premarket, regular) bars of one day; afternoon/after-hours bars are dropped."""
    open_ts, close_ts = clock.at(day, "09:30"), clock.at(day, "16:00")
    pre = [b for b in bars if b[0] < open_ts]
    reg = [b for b in bars if open_ts <= b[0] < close_ts]
    return pre, reg


def premarket_stats(pre, prev_close):
    """Pre-market price action. Yahoo reports pre-market volume as zero, so activity is measured as the
    share of pre-market bars in which the price actually moved."""
    if not pre:
        return None
    last = pre[-1][4]
    vol = sum(b[5] or 0 for b in pre)
    hi, lo = max(b[2] for b in pre), min(b[3] for b in pre)
    return {
        "pm_last": last, "pm_high": hi, "pm_low": lo, "pm_volume": vol or None,
        "pm_active": sum(1 for b in pre if b[2] > b[3]) / len(pre),
        "pm_range_pct": (hi / lo - 1) * 100 if lo else None,
        "gap_pct": (last / prev_close - 1) * 100 if prev_close else None,
    }


def intraday_stats(reg, prev_close, avg_volume, interval_s):
    """Session so far: price, VWAP, range, opening range, volume pace."""
    if not reg:
        return None
    o = reg[0][1]
    last = reg[-1][4]
    vol = sum(b[5] or 0 for b in reg)
    pv = sum(((b[2] + b[3] + b[4]) / 3) * (b[5] or 0) for b in reg)
    n_or = max(1, 900 // interval_s)  # first 15 minutes
    orb = reg[:n_or]
    minutes = len(reg) * interval_s / 60
    return {
        "open": o, "last": last, "high": max(b[2] for b in reg), "low": min(b[3] for b in reg),
        "vwap": pv / vol if vol else last, "volume": vol,
        "volume_pace": (vol / (avg_volume * minutes / 390)) if avg_volume and minutes else None,
        "chg_from_prev_close_pct": (last / prev_close - 1) * 100 if prev_close else None,
        "chg_from_open_pct": (last / o - 1) * 100 if o else None,
        "or_high": max(b[2] for b in orb), "or_low": min(b[3] for b in orb),
        "minutes": minutes,
    }


def compress(bars, every_s, start_ts=None):
    """Aggregate bars into coarser OHLCV rows, e.g. 5-minute bars into 30-minute rows for a prompt."""
    out = {}
    for t, o, h, l, c, v in bars:
        k = (t - (start_ts or 0)) // every_s
        if k not in out:
            out[k] = [t, o, h, l, c, v or 0]
        else:
            r = out[k]
            r[2], r[3], r[4], r[5] = max(r[2], h), min(r[3], l), c, r[5] + (v or 0)
    return [out[k] for k in sorted(out)]


def scan_score(d, pm):
    """Rank pre-market candidates: a gap that is large for this stock, trading actively, in a liquid name."""
    if not d or d["prev_close"] <= 0:
        return 0.0
    gap = abs(pm["gap_pct"]) if pm and pm["gap_pct"] is not None else 0.0
    gap_in_atr = gap / d["atr_pct"] if d["atr_pct"] else 0.0
    active = pm["pm_active"] if pm else 0.0
    liquidity = math.log10(10 + d["avg_volume"] * d["prev_close"]) / 9
    return gap_in_atr * (0.5 + active) * liquidity


def _bucket(x, edges, names):
    for edge, name in zip(edges, names):
        if x >= edge:
            return name
    return names[-1]


def fingerprint(d, pm, sector="", news_mentions=0):
    """Discrete situation tokens for memory retrieval, e.g. ['gapup2', 'pmvolhigh', 'highvol', ...]."""
    out = []
    if pm and pm.get("gap_pct") is not None:
        g = pm["gap_pct"]
        out.append(_bucket(g, [5, 2, 0.75, -0.75, -2, -5], ["gapup5", "gapup2", "gapup1", "gapflat", "gapdown1",
                                                            "gapdown2", "gapdown5"]))
        if d and d["atr_pct"] and abs(g) / d["atr_pct"] >= 1:
            out.append("gapbig_atr")
        out.append("pmactive" if pm.get("pm_active", 0) >= 0.7 else "pmquiet" if pm.get("pm_active", 0) < 0.3
                   else "pmsome")
    if d:
        out.append(_bucket(d["atr_pct"], [4, 1.5], ["highvol", "midvol", "lowvol"]))
        if d["ret_20d"] is not None:
            out.append(_bucket(d["ret_20d"], [10, -10], ["uptrend20", "range20", "downtrend20"]))
    if news_mentions:
        out.append("innews")
    if sector:
        out.append("sector_" + sector.lower().replace(" ", ""))
    return out


def market_fingerprint(spy_pm, regime=""):
    out = []
    if spy_pm and spy_pm.get("gap_pct") is not None:
        out.append(_bucket(spy_pm["gap_pct"], [0.5, -0.5], ["spygapup", "spyflat", "spygapdown"]))
    if regime:
        out.append("regime_" + regime)
    return out


def fmt(x, d=2):
    if x is None:
        return "n/a"
    if isinstance(x, float) and abs(x) >= 1e6:
        return f"{x / 1e6:.1f}M"
    return f"{x:.{d}f}" if isinstance(x, (int, float)) else str(x)
