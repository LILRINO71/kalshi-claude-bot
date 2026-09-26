"""Settings shared by the dashboard, backtests and the live paper trader."""
from . import storage

CRYPTO = ["BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB"]
# Kalshi has no recurring single-stock price markets; these hourly index ladders are the stock markets that trade.
INDICES = {
    "SPX": {"series": "KXINXU", "label": "S&P 500", "yahoo": "^GSPC"},
    "NDX": {"series": "KXNASDAQ100U", "label": "Nasdaq-100", "yahoo": "^NDX"},
}
ASSETS = CRYPTO + list(INDICES)
SERIES = {**{a: f"KX{a}15M" for a in CRYPTO}, **{k: v["series"] for k, v in INDICES.items()}}
LABELS = {**{a: a for a in CRYPTO}, **{k: v["label"] for k, v in INDICES.items()}}
INDEX_HISTORY_DAYS = 29  # Yahoo keeps about 30 days of 1-minute index data


def is_index(asset):
    return asset in INDICES


def decision_delay(cfg, asset):
    """Minutes before close when a decision is made for this asset."""
    return cfg["index_time_delay"] if is_index(asset) else cfg["time_delay"]

MODELS = {
    "haiku": "Haiku 4.5 (lightest on usage limits)",
    "sonnet": "Sonnet 5 (default)",
    "opus": "Opus 5.5 (strongest, heaviest)",
    "fable": "Fable 5.1 (may need extra usage)",
}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
STRATEGIES = {
    "claude": "Claude (AI decision)",
    "random_walk": "Random-walk fair value (no AI)",
    "momentum": "5-minute momentum (no AI)",
    "favorite": "Buy the market favorite (no AI)",
    "coin_flip": "Coin flip (no AI, control)",
}
SIZING = {
    "fixed": "Fixed dollars per trade",
    "percent": "Percent of equity per trade",
    "kelly": "Fractional Kelly on model edge",
}

DEFAULT_INSTRUCTIONS = """- Prefer SKIP when spot is very close to the strike and there is lots of time left: that is close to a coin flip.
- Momentum over the last 5 minutes matters more than the 60-minute trend for a 15-minute market.
- Never pay more than 0.85 for a side; the payoff is too small for the risk."""

DEFAULTS = {
    "model": "sonnet",
    "effort": "medium",
    "strategy": "claude",
    "assets": ["BTC"],
    "time_delay": 10,            # minutes left in a 15-minute crypto session when the decision is made
    "index_time_delay": 15,      # minutes left in an hourly stock-index market when the decision is made
    "obey_model": True,          # False trades the opposite of the model's call
    "bankroll": 1000.0,
    "sizing": "percent",
    "fixed_stake": 20.0,         # dollars per trade for sizing=fixed
    "percent_stake": 2.0,        # % of equity per trade for sizing=percent
    "kelly_fraction": 0.25,      # share of full Kelly for sizing=kelly
    "max_stake_pct": 5.0,        # hard cap on any single trade, % of equity
    "min_edge": 0.03,            # model probability must beat the ask by this much (non-Claude strategies)
    "max_price": 0.85,           # never pay more than this per contract
    "simulate_fees": True,
    "slippage": 0.01,            # extra dollars per contract on backtest fills (no historical depth)
    "max_daily_profit": 0,       # stop new trades after settled PnL for the day reaches this; 0 disables
    "max_daily_loss": -100,      # stop new trades after settled PnL for the day reaches this; 0 disables
    "parallel_calls": 2,         # concurrent Claude calls during backtests
    "instructions": DEFAULT_INSTRUCTIONS,
}


def load():
    storage.ensure_dirs()
    saved = storage.read_json(storage.SETTINGS_FILE, {}) or {}
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    return cfg


def validate(cfg):
    """Return a cleaned copy or raise ValueError with a readable message."""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    assets = out["assets"]
    if isinstance(assets, str):
        assets = [a for a in assets.replace(" ", "").split(",") if a]
    out["assets"] = [a.upper() for a in assets]
    if not out["assets"] or any(a not in SERIES for a in out["assets"]):
        raise ValueError(f"assets must be a non-empty subset of {ASSETS}")
    if out["model"] not in MODELS:
        raise ValueError(f"model must be one of {list(MODELS)}")
    if out["effort"] not in EFFORTS:
        raise ValueError(f"effort must be one of {EFFORTS}")
    if out["strategy"] not in STRATEGIES:
        raise ValueError(f"strategy must be one of {list(STRATEGIES)}")
    if out["sizing"] not in SIZING:
        raise ValueError(f"sizing must be one of {list(SIZING)}")
    for key in ("time_delay", "index_time_delay", "bankroll", "fixed_stake", "percent_stake", "kelly_fraction",
                "max_stake_pct", "min_edge", "max_price", "slippage", "max_daily_profit",
                "max_daily_loss", "parallel_calls"):
        try:
            out[key] = float(out[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number")
    for key in ("obey_model", "simulate_fees"):
        v = out[key]
        out[key] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    if not 0.5 <= out["time_delay"] <= 15:
        raise ValueError("time_delay must be between 0.5 and 15 minutes")
    if not 1 <= out["index_time_delay"] <= 55:
        raise ValueError("index_time_delay must be between 1 and 55 minutes")
    if out["bankroll"] <= 0:
        raise ValueError("bankroll must be positive")
    if not 0 < out["max_price"] < 1:
        raise ValueError("max_price must be between 0 and 1")
    if out["max_daily_profit"] < 0 or out["max_daily_loss"] > 0:
        raise ValueError("max_daily_profit must be >= 0 and max_daily_loss <= 0")
    out["parallel_calls"] = int(min(6, max(1, out["parallel_calls"])))
    out["instructions"] = str(out["instructions"])[:4000]
    return out


def save(cfg):
    cfg = validate(cfg)
    storage.write_json(storage.SETTINGS_FILE, cfg)
    return cfg
