"""Day-trader settings: defaults, validation, and the models' knowledge cutoffs."""
from . import store

MODELS = {
    "sonnet": "Sonnet 5.5 (default: strong and quick)",
    "opus": "Opus 5.5 (strongest, heaviest on usage limits)",
    "haiku": "Haiku 5.5 (lightest on usage limits)",
    "fable": "Fable 5.1 (most capable, may need extra usage)",
}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

BRAINS = {
    "claude": "Claude: scans news, picks stocks, plans, trades, reflects and rewrites its playbook",
    "gap_and_go": "Gap-and-go opening-range breakout (no AI, fixed rules, control)",
    "none": "No trades (flat control)",
}

# Backtesting a model on days it may have read about in training is not a backtest. A run starting
# before the model's cutoff is refused unless --allow-leaky is passed. Opus 5.5's cutoff is June
# 2026; the others are not published here, so they default to the same date. Override per model in
# settings.json ("knowledge_cutoff": {"sonnet": "YYYY-MM-DD"}) if you know better.
KNOWLEDGE_CUTOFF = {"haiku": "2026-06-30", "sonnet": "2026-06-30", "opus": "2026-06-30", "fable": "2026-06-30"}

DEFAULTS = {
    "brain": "claude",
    "model": "sonnet",
    "effort": "medium",
    "bankroll": 25000.0,          # US pattern-day-trader minimum
    "interval": "5m",             # intraday bar size used for fills and context
    "universe": [],               # empty = every stock in universe.py
    "scan_top": 25,               # candidates the pre-market scanner hands to the morning brief
    "focus_max": 4,               # stocks Claude may focus on per day
    "premarket_time": "09:00",    # when the morning brief runs (New York time)
    "checkins": ["10:00", "11:30", "13:30", "15:00"],  # intraday reviews where Claude can act
    "flatten_time": "15:55",      # every position is closed at this time: day trading only
    "risk_per_trade_pct": 0.5,    # % of equity lost if a full-size trade hits its stop
    "max_position_pct": 25.0,     # cap on one position's value, % of equity
    "max_leverage": 1.0,          # gross exposure / equity (raise to 2-4 for margin)
    "max_positions": 3,
    "max_daily_loss_pct": 2.0,    # flatten and stop for the day; 0 disables
    "scale_out_r": 0.25,          # take part of every position off this many R past the fill (0 = never)
    "scale_out_frac": 0.75,       # ...this share of it; the stop on the rest then moves to the entry
    "allow_short": True,
    "slippage_bps": 2.0,          # per side, on top of the bar price (minimum one cent)
    "commission_per_share": 0.0,
    "commission_min": 0.0,
    "learn": True,                # reflect after each day and evolve the playbook
    "curate_every": 5,            # trading days between playbook consolidations
    "max_rules": 25,              # playbook size cap
    "trial_size_cap": 0.5,        # plans that rely only on unproven (trial) rules trade at most this size
    "baserate_names": 15,         # scanner names the desk replays mechanical setups on after each close
    "memory_k": 6,                # lessons retrieved into each prompt
    "news_per_ticker": 8,
    "market_news": 20,
    "knowledge_cutoff": {},       # per-model overrides of KNOWLEDGE_CUTOFF
    "use_sec": True,              # SEC EDGAR filings (earnings 8-Ks, offerings) with exact times
    "use_gdelt": True,            # GDELT headlines with exact times (often rate-limited on shared networks)
    "use_alpaca_news": True,      # Alpaca's news service (Benzinga, exact times), when an Alpaca key is saved
    "sec_user_agent": "",         # SEC asks for "Name contact@email"; blank uses a generic one
    "instructions": "",           # your own notes for Claude, always included
}

_NUM = ("bankroll", "scan_top", "focus_max", "risk_per_trade_pct", "max_position_pct", "max_leverage",
        "max_positions", "max_daily_loss_pct", "scale_out_r", "scale_out_frac", "slippage_bps",
        "commission_per_share", "commission_min", "curate_every", "max_rules", "trial_size_cap", "baserate_names", "memory_k", "news_per_ticker", "market_news")
_INT = ("scan_top", "focus_max", "max_positions", "curate_every", "max_rules", "baserate_names", "memory_k",
        "news_per_ticker", "market_news")


def load():
    store.ensure_dirs()
    saved = store.read_json(store.SETTINGS_FILE, {}) or {}
    return validate({**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}})


def validate(cfg):
    out = {**DEFAULTS, **{k: v for k, v in cfg.items() if k in DEFAULTS}}
    if out["brain"] not in BRAINS:
        raise ValueError(f"brain must be one of {list(BRAINS)}")
    if out["model"] not in MODELS:
        raise ValueError(f"model must be one of {list(MODELS)}")
    if out["effort"] not in EFFORTS:
        raise ValueError(f"effort must be one of {EFFORTS}")
    if out["interval"] not in ("1m", "2m", "5m", "15m"):
        raise ValueError("interval must be 1m, 2m, 5m or 15m")
    for k in _NUM:
        try:
            out[k] = int(out[k]) if k in _INT else float(out[k])
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a number")
    for k in ("allow_short", "learn", "use_sec", "use_gdelt", "use_alpaca_news"):
        out[k] = out[k] if isinstance(out[k], bool) else str(out[k]).lower() in ("1", "true", "yes", "on")
    if out["bankroll"] <= 0 or not 0 < out["risk_per_trade_pct"] <= 5:
        raise ValueError("bankroll must be positive and risk_per_trade_pct in (0, 5]")
    if not 1 <= out["focus_max"] <= 10 or not 1 <= out["max_positions"] <= 10:
        raise ValueError("focus_max and max_positions must be 1-10")
    if not 0 < out["max_leverage"] <= 4:
        raise ValueError("max_leverage must be in (0, 4]")
    if not 0.25 <= out["trial_size_cap"] <= 1:
        raise ValueError("trial_size_cap must be 0.25-1.0")
    if not 0 <= out["baserate_names"] <= 40:
        raise ValueError("baserate_names must be 0-40")
    if out["scale_out_r"] and not 0.2 <= out["scale_out_r"] <= 3:
        raise ValueError("scale_out_r must be 0 (off) or 0.2-3")
    if not 0.25 <= out["scale_out_frac"] <= 0.75:
        raise ValueError("scale_out_frac must be 0.25-0.75")
    out["universe"] = [str(t).upper() for t in (out["universe"] or [])]
    times = {}
    for k in ("premarket_time", "flatten_time"):
        times[k] = _hhmm(out[k], k)
    out.update(times)
    checkins = sorted({_hhmm(t, "checkins") for t in (out["checkins"] or [])})
    if any(not "09:30" < t < out["flatten_time"] for t in checkins):
        raise ValueError(f"check-ins must be between 09:30 and the flatten time {out['flatten_time']}")
    if not "04:00" <= out["premarket_time"] <= "09:25" or not "09:45" <= out["flatten_time"] <= "15:59":
        raise ValueError("premarket_time must be 04:00-09:25 and flatten_time 09:45-15:59")
    out["checkins"] = checkins
    out["instructions"] = str(out["instructions"] or "")[:4000]
    return out


def _hhmm(value, key):
    """'9:45' -> '09:45'; zero-padded times sort correctly as text."""
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except ValueError:
        raise ValueError(f"{key} must be HH:MM times, got {value!r}")
    return f"{h:02d}:{m:02d}"


def save(cfg):
    cfg = validate(cfg)
    store.write_json(store.SETTINGS_FILE, cfg)
    return cfg


def cutoff_for(cfg):
    return (cfg.get("knowledge_cutoff") or {}).get(cfg["model"]) or KNOWLEDGE_CUTOFF.get(cfg["model"], "2026-06-30")
