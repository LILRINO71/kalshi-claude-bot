"""Decision makers. Claude is the one that learns; the others are controls it has to beat."""
from . import clock, llm, prompts


class ClaudeBrain:
    """Every step is one structured `claude -p` call; see prompts.py."""
    does_checkins = True
    learns = True
    name = "claude"

    def __init__(self, cfg, source="backtest"):
        self.model, self.effort, self.source = cfg["model"], cfg["effort"], source

    def _call(self, step, prompt, schema):
        answer, meta = llm.ask(prompt, schema, system=prompts.SYSTEM, model=self.model, effort=self.effort,
                               source=f"daytrader-{self.source}-{step}")
        return answer, {**meta, "llm": True}

    def morning(self, ctx):
        return self._call("morning", prompts.morning(ctx), prompts.MORNING_SCHEMA)

    def research(self, ctx):
        answer, meta = self._call("research", prompts.research(ctx), prompts.RESEARCH_SCHEMA)
        return {**answer, "plans": [_clean_plan(p) for p in answer.get("plans") or []]}, meta

    def checkin(self, ctx):
        answer, meta = self._call("checkin", prompts.checkin(ctx), prompts.CHECKIN_SCHEMA)
        acts = []
        for a in answer.get("actions") or []:
            a = _clean_plan(a)
            for k in ("stop", "target"):
                if a.get("action") == "adjust" and not a.get(k):
                    a[k] = None
            acts.append(a)
        return {**answer, "actions": acts}, meta

    def reflect(self, ctx):
        return self._call("reflect", prompts.reflect(ctx), prompts.REFLECT_SCHEMA)

    def curate(self, ctx):
        return self._call("curate", prompts.curate(ctx), prompts.CURATE_SCHEMA)


def _clean_plan(p):
    """Turn the schema's 0 / "" placeholders into None."""
    p = dict(p)
    for k in ("target", "breakeven_at"):
        if not p.get(k):
            p[k] = None
    if not p.get("time_exit"):
        p["time_exit"] = None
    if p.get("entry_type") == "market":
        p["entry"] = p.get("entry") or None
    return p


class GapAndGoBrain:
    """Fixed-rule control: trade the biggest gappers on a break of their pre-market range. No AI, no learning."""
    does_checkins = False
    learns = False
    name = "gap_and_go"

    def __init__(self, cfg, source="backtest"):
        self.cfg = cfg

    def morning(self, ctx):
        picks = []
        for r in ctx["scan"]:
            pm = r["pm"]
            if pm and pm["gap_pct"] is not None and abs(pm["gap_pct"]) >= 2 and pm["pm_active"] >= 0.5:
                picks.append({"ticker": r["ticker"], "why": f"gap {pm['gap_pct']:+.1f}%", "catalyst": "gap",
                              "bias": "long" if pm["gap_pct"] > 0 else "short", "rules_used": []})
        return {"market_view": "rules only", "regime": "unclear", "focus": picks[:ctx["focus_max"]], "avoid": []}, {}

    def research(self, ctx):
        plans = []
        for r in ctx["research"]:
            d, pm = r["daily"], r["pm"]
            if not d or not pm:
                continue
            risk = 0.35 * d["atr"]
            long = pm["gap_pct"] > 0
            if long:
                entry = round(pm["pm_high"] + 0.01, 2)
                stop, target = round(entry - risk, 2), round(entry + 2 * risk, 2)
            elif self.cfg["allow_short"]:
                entry = round(pm["pm_low"] - 0.01, 2)
                stop, target = round(entry + risk, 2), round(entry - 2 * risk, 2)
            else:
                continue
            plans.append({"ticker": r["ticker"], "side": "long" if long else "short", "entry_type": "stop",
                          "entry": entry, "stop": stop, "target": target, "valid_from": "09:35",
                          "valid_until": "11:00", "time_exit": None, "breakeven_at": None, "size": 1.0,
                          "setup": "pre-market range breakout", "thesis": "gap and go", "confidence": 0.5,
                          "rules_used": []})
        return {"plans": plans, "watch_only": []}, {}

    def checkin(self, ctx):
        return {"market_read": "", "actions": []}, {}

    def reflect(self, ctx):
        return None, {}

    def curate(self, ctx):
        return None, {}


class FlatBrain(GapAndGoBrain):
    """Never trades: shows what costs and data quirks do to an account that does nothing."""
    name = "none"

    def morning(self, ctx):
        return {"market_view": "flat control", "regime": "unclear", "focus": [], "avoid": []}, {}


def make(cfg, source="backtest"):
    return {"claude": ClaudeBrain, "gap_and_go": GapAndGoBrain, "none": FlatBrain}[cfg["brain"]](cfg, source)
