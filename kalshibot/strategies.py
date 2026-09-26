"""Decision makers. Claude is one; the others are free baselines it has to beat."""
import hashlib
import math

from . import claude_cli, snapshot as snap_mod


def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _edge_pick(s, p_up, min_edge, reason):
    """Take the side whose probability beats its ask by the most, if by more than min_edge."""
    edge_up = p_up - s.up_ask
    edge_down = (1 - p_up) - s.down_ask
    if max(edge_up, edge_down) < min_edge:
        return {"direction": "SKIP", "probability_up": p_up,
                "reason": f"{reason}; best edge {max(edge_up, edge_down):+.3f} below {min_edge:.3f}"}
    side = "UP" if edge_up >= edge_down else "DOWN"
    return {"direction": side, "probability_up": p_up,
            "reason": f"{reason}; edge {max(edge_up, edge_down):+.3f} on {side}"}


def random_walk(s, cfg):
    p = s.features["baseline_up"]
    return _edge_pick(s, p, cfg["min_edge"], f"random-walk P(up) {p:.3f}")


def momentum(s, cfg):
    f = s.features
    vol = f["vol_1m"] / 100
    if vol <= 0 or f["change_5m"] is None:
        return {"direction": "SKIP", "probability_up": 0.5, "reason": "no volatility data"}
    # Project half of the last 5-minute drift forward to the close.
    drift = (f["change_5m"] / 100) / 5 * s.mins_left * 0.5
    z = (math.log(s.spot / s.strike) + drift) / (vol * math.sqrt(max(s.mins_left, 1e-6)))
    p = _phi(z)
    return _edge_pick(s, p, cfg["min_edge"], f"momentum P(up) {p:.3f}")


def favorite(s, cfg):
    p = s.market_prob_up
    side = "UP" if s.up_ask >= s.down_ask else "DOWN"
    return {"direction": side, "probability_up": p, "reason": f"market favorite {side} at mid {p:.3f}"}


def coin_flip(s, cfg):
    bit = int(hashlib.sha256(s.ticker.encode()).hexdigest(), 16) & 1
    return {"direction": "UP" if bit else "DOWN", "probability_up": 0.5, "reason": "coin flip"}


def claude(s, cfg, source="backtest"):
    return claude_cli.decide(snap_mod.prompt(s, cfg["instructions"]), cfg["model"], cfg["effort"], source)


BASELINES = {"random_walk": random_walk, "momentum": momentum, "favorite": favorite, "coin_flip": coin_flip}


def decide(s, cfg, source="backtest"):
    if cfg["strategy"] == "claude":
        return claude(s, cfg, source)
    d = BASELINES[cfg["strategy"]](s, cfg)
    return {**d, "latency_s": 0.0, "cached": False}


def uses_claude(cfg):
    return cfg["strategy"] == "claude"
