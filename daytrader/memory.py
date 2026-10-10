"""What the trader learns: an evolving playbook of rules, layered lessons, and a daily journal.

Design, adapted from open-source agent research (see CREDITS.md):
- Playbook rules are itemized with evidence counters, as in ACE (Agentic Context Engineering).
  The playbook only changes through small delta operations (add, edit, merge, retire), never a
  full rewrite, which ACE showed can collapse a playbook to a fraction of its content.
- Rules are judged on outcomes, not on how convincing a reflection sounded: every trade credits
  the R-multiple it made to the rules it cited (half to the primary rule, half split among the
  rest). Stats are shrunk toward zero, R~ = sum_R / (n + 5), so a lucky trade or two can't promote
  a rule, and losing rules are retired automatically. The model's own helpful/harmful opinions
  only decide the fate of rules that have no trades yet.
- Lessons live in short- and long-term layers with FinMem-style scoring: recency decay +
  importance + BM25 relevance. Queries and lessons carry discrete "situation fingerprint" tokens
  (gap size, volume, volatility, trend, sector, market regime) so lexical retrieval finds lessons
  from similar situations, not just similar words. A lesson that says what an existing one says
  reinforces it (more important, fresher) instead of piling up as a near-copy.
- Numbers beat stories. Three code-kept ledgers are shown back to the model every day:
  its track record (by side, time of day, origin, exit type, confidence, profit capture), what its
  check-in interventions were worth against leaving each plan alone, and base rates of simple
  mechanical setups replayed on the scanner's names after every close. A real account makes two
  or three trades a day; the base rates add dozens of samples a day from the same fill rules.
- Unproven ideas trade small: a plan whose cited rules are all still on trial is capped in size
  until one of them earns promotion on its results.
"""
import copy
import math
import re
from collections import Counter

from . import store

HALF_LIFE = {"short": 3.0, "long": 60.0}   # trading days
PROMOTE_IMPORTANCE = 8                      # lessons this important start in long-term memory
SHORT_MAX_AGE = 15                          # short-term lessons older than this are forgotten unless promoted
SHRINK = 5                                  # pseudo-trades of zero R added when judging a rule
PROMOTE = (5, 0.05)                         # trial -> active: at least 5 trades and shrunk R above +0.05
DEMOTE = (8, 0.0)                           # active -> trial: 8+ trades and shrunk R below 0
RETIRE = (6, -0.15)                         # retire: 6+ trades and shrunk R below -0.15
OPINION_RETIRE = 4                          # untested rule judged harmful this many times more than helpful
DORMANT_DAYS = 15                           # uncited this long: hidden from prompts until revived
MAX_TRIAL = 8                               # trial rules allowed at once
SIMILAR_RULE = 0.5                          # word overlap at which a proposed rule duplicates an existing one
SIMILAR_LESSON = 0.55                       # word overlap at which a new lesson reinforces an old one
TRACK_MAX = 400                             # trades kept in the track record
MGMT_MAX = 300                              # check-in interventions kept
BASE_KEEP = 60                              # trading days of base-rate samples kept
BASE_WINDOW = 20                            # trading days of base rates shown in prompts

# Mechanical setups the desk replays on the scanner's names after each close (see replay.py).
SETUP_NAMES = {
    "orb_with_gap": "opening-range (first 15 min) breakout in the gap's direction",
    "orb_against_gap": "opening-range breakout against the gap (a fade)",
    "pm_break": "break of the pre-market high (gap up) or low (gap down)",
    "trend_1030": "10:30 entry with the move since the open, held to the close",
}

SEED_RULES = [
    ("When a stock has no fresh, specific catalyst (earnings, guidance, analyst action, deal, FDA, sector news) "
     "and no unusual pre-market volume, leave it alone.", ["selection", "catalyst"]),
    ("Put the stop where the idea is proven wrong and the target at least 1.5x the risk; when the levels don't "
     "allow that, skip the trade.", ["risk", "planning"]),
    ("Let the open settle: no entries before 09:35; prefer a break of the opening range or a pullback that holds.",
     ["timing", "entry"]),
    ("When SPY and QQQ are falling hard, be careful buying; when they are ripping, be careful shorting.",
     ["market", "context"]),
    ("When a gap immediately fills back through the previous close, treat the move as failed and stop trying it.",
     ["gap", "exit"]),
    ("When conviction is low, size down; a missed trade costs nothing.", ["risk", "sizing"]),
]

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-]+")
_STOP = set("""a an and are as at be but by for from has have if in into is it its of on or so that the their
then there these this to was were will with not no do does did can could should would you your we our
they them than too very just also more most less when""".split())


def tokens(text):
    return [w.lower() for w in _WORD.findall(text or "") if w.lower() not in _STOP and len(w) > 1]


def bm25_scores(query, docs, k1=1.5, b=0.75):
    """BM25 of one query against a list of token lists. Small, pure Python, no index needed."""
    if not docs:
        return []
    n = len(docs)
    avgdl = sum(len(d) for d in docs) / n or 1
    df = Counter(t for d in docs for t in set(d))
    out = []
    for d in docs:
        tf = Counter(d)
        s = 0.0
        for t in set(query):
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avgdl))
        out.append(s)
    return out


def shrunk_r(rule):
    return rule["sum_r"] / (rule["uses"] + SHRINK)


def similarity(a, b):
    """How much of the shorter text the longer one already says (overlap of content words), 0-1.
    Paraphrased rewrites of a rule score about 0.5+; distinct lessons on one theme stay under ~0.35."""
    x, y = set(tokens(a)), set(tokens(b))
    if not x or not y:
        return 0.0
    if min(len(x), len(y)) < 6:  # too short for containment to mean anything
        return len(x & y) / len(x | y)
    return len(x & y) / min(len(x), len(y))


def _stats(rows):
    rs = [t["r"] for t in rows if t.get("r") is not None]
    if not rs:
        return None
    return len(rs), sum(rs) / len(rs), sum(1 for r in rs if r > 0) / len(rs), sum(rs)


def _line(label, rows):
    s = _stats(rows)
    if not s:
        return None
    n, avg, win, total = s
    return f"{label} {n}: {avg:+.2f}R avg, {win * 100:.0f}% won, {total:+.1f}R total"


def gap_bucket(gap_pct):
    g = abs(gap_pct or 0)
    return "gap 3%+" if g >= 3 else "gap 1-3%" if g >= 1 else "gap <1%"


class Memory:
    """Everything one trader profile has learned. State is a few files in one folder."""

    def __init__(self, folder, seed=True):
        self.folder = folder
        state = store.read_json(folder / "memory.json", None, strict=True)
        if state is None:
            self.state = {"rules": [], "lessons": [], "next_rule": 1, "next_lesson": 1, "day_index": 0,
                          "version": 0}
            if seed:
                for text, tags in SEED_RULES:
                    self.add_rule(text, tags, day="seed", source="seed", status="active")
        else:
            self.state = state
        for key in ("track", "base", "mgmt"):  # ledgers added after the first profiles were saved
            self.state.setdefault(key, [])

    # ------------------------------------------------------------ persistence

    def save(self):
        store.write_json(self.folder / "memory.json", self.state)

    def snapshot(self):
        return copy.deepcopy(self.state)

    def journal(self):
        return store.read_jsonl(self.folder / "journal.jsonl")

    def write_journal(self, row):
        store.append_jsonl(self.folder / "journal.jsonl", row)

    def log_change(self, row):
        store.append_jsonl(self.folder / "playbook_history.jsonl", row)

    @property
    def day_index(self):
        return self.state["day_index"]

    # ------------------------------------------------------------ playbook

    @property
    def rules(self):
        return self.state["rules"]

    def active_rules(self, include_dormant=False):
        ok = ("active", "trial", "dormant") if include_dormant else ("active", "trial")
        return [r for r in self.rules if r["status"] in ok]

    def rule(self, rid):
        return next((r for r in self.rules if r["id"] == rid), None)

    def similar_rule(self, text):
        """An existing rule that already says this: a live one, a dormant one, or one retired for losing.
        Returns (rule, how) or (None, None)."""
        best, score = None, 0.0
        for r in self.rules:
            if r["status"] == "retired" and not str(r.get("retired_why", "")).startswith(("losing", "untested")):
                continue  # merged or capped rules aren't a verdict on the idea
            s = 1.0 if r["text"].lower() == text.lower() else similarity(text, r["text"])
            if s > score:
                best, score = r, s
        if best and score >= SIMILAR_RULE:
            return best, best["status"]
        return None, None

    def add_rule(self, text, tags=(), day="", source="reflection", status="trial", dedupe=True):
        text = " ".join(str(text).split())[:400]
        if not text:
            return None
        if dedupe:
            twin, how = self.similar_rule(text)
            if how == "dormant":  # the same idea again: wake the old rule up, with its evidence
                return self.revive_rule(twin["id"], day, "proposed again")
            if twin:
                return None
        if status == "trial" and sum(r["status"] == "trial" for r in self.rules) >= MAX_TRIAL:
            return None
        rid = f"R{self.state['next_rule']}"
        self.state["next_rule"] += 1
        rule = {"id": rid, "text": text, "tags": [str(t).lower()[:24] for t in tags][:6], "status": status,
                "source": source, "created": day, "updated": day, "born_index": self.day_index,
                "last_cited": self.day_index, "uses": 0, "wins": 0, "losses": 0, "sum_r": 0.0, "sum_r2": 0.0,
                "helpful": 0, "harmful": 0}
        self.rules.append(rule)
        self._bump(day, {"op": "add", "rule": dict(rule)})
        return rule

    def _bump(self, day, row):
        self.state["version"] += 1
        self.log_change({"day": day, "version": self.state["version"], **row})

    def edit_rule(self, rid, text, day, why=""):
        r = self.rule(rid)
        text = " ".join(str(text).split())[:400]
        if not r or r["status"] == "retired" or not text or text == r["text"]:
            return False
        old, r["text"], r["updated"] = r["text"], text, day
        self._bump(day, {"op": "edit", "id": rid, "old": old, "new": text, "why": str(why)[:300]})
        return True

    def retire_rule(self, rid, day, why):
        r = self.rule(rid)
        if not r or r["status"] == "retired":
            return False
        r["status"], r["updated"], r["retired_why"] = "retired", day, str(why)[:300]
        self._bump(day, {"op": "retire", "id": rid, "why": r["retired_why"]})
        return True

    def revive_rule(self, rid, day, why=""):
        r = self.rule(rid)
        if not r or r["status"] != "dormant":
            return None
        r["status"], r["updated"], r["last_cited"] = "trial", day, self.day_index
        self._bump(day, {"op": "revive", "id": rid, "why": str(why)[:300]})
        return r

    def merge_rules(self, ids, text, tags, day, why=""):
        """Replace several rules with one; the new rule inherits their summed evidence."""
        olds = [self.rule(i) for i in dict.fromkeys(ids)]
        olds = [r for r in olds if r and r["status"] != "retired"]
        if len(olds) < 2:
            return None
        new = self.add_rule(text, tags, day, source="merge", status="active" if any(
            r["status"] == "active" for r in olds) else "trial", dedupe=False)
        if not new:
            return None
        for k in ("uses", "wins", "losses", "sum_r", "sum_r2", "helpful", "harmful"):
            new[k] = round(sum(r[k] for r in olds), 4)
        for r in olds:
            self.retire_rule(r["id"], day, f"merged into {new['id']}")
        self._bump(day, {"op": "merge", "into": new["id"], "from": [r["id"] for r in olds], "why": str(why)[:300]})
        return new

    def cite(self, rule_ids):
        for rid in rule_ids or []:
            r = self.rule(rid)
            if r:
                r["last_cited"] = self.day_index
                if r["status"] == "dormant":
                    r["status"] = "trial"

    def record_trade(self, trade):
        """Outcome attribution: half the trade's R to the primary (first) cited rule, half split among the rest."""
        r_mult = trade.get("r")
        ids = [i for i in dict.fromkeys(trade.get("rules") or []) if self.rule(i)]
        if r_mult is None or not ids:
            return
        shares = [1.0] if len(ids) == 1 else [0.5] + [0.5 / (len(ids) - 1)] * (len(ids) - 1)
        for rid, w in zip(ids, shares):
            r = self.rule(rid)
            credit = r_mult * w
            r["uses"] += 1
            r["sum_r"] = round(r["sum_r"] + credit, 4)
            r["sum_r2"] = round(r["sum_r2"] + credit * credit, 4)
            r["wins" if trade["pnl"] > 0 else "losses"] += 1
            r["last_cited"] = self.day_index

    def apply_feedback(self, rid, verdict):
        r = self.rule(rid)
        if r and verdict in ("helpful", "harmful"):
            r[verdict] += 1

    def size_cap(self, rule_ids, trial_cap):
        """Largest size a plan may take: full size only when it relies on at least one active (proven or
        seeded) rule; ideas resting only on trial rules are tested small."""
        rules = [self.rule(i) for i in rule_ids or []]
        if any(r and r["status"] == "active" for r in rules):
            return 1.0
        return trial_cap

    # ------------------------------------------------------------ ledgers

    def record_track(self, row):
        """One closed trade, compactly, for the track record."""
        self.state["track"] = (self.state["track"] + [row])[-TRACK_MAX:]

    def record_mgmt(self, rows):
        self.state["mgmt"] = (self.state["mgmt"] + list(rows))[-MGMT_MAX:]

    def record_base_rates(self, rows):
        keep = [b for b in self.state["base"] if self.day_index - b["di"] < BASE_KEEP]
        self.state["base"] = keep + [{**b, "di": self.day_index} for b in rows]

    def track_text(self, n=60):
        tr = self.state["track"][-n:]
        if not tr:
            return "(no trades yet)"
        out = [f"Your last {len(tr)} trades over {len({t['day'] for t in tr})} sessions. " + (_line("All", tr) or "")]
        out.append(" | ".join(filter(None, [_line("Long", [t for t in tr if t["side"] == "long"]),
                                            _line("Short", [t for t in tr if t["side"] == "short"])])))
        by_time = [("Entered 09:30-09:59", "09:30", "10:00"), ("10:00-11:29", "10:00", "11:30"),
                   ("11:30-close", "11:30", "16:01")]
        out.append(" | ".join(filter(None, [_line(lab, [t for t in tr if a <= t.get("entry", "") < b])
                                            for lab, a, b in by_time])))
        out.append(" | ".join(filter(None, [_line("Pre-market plans", [t for t in tr if t.get("origin") != "checkin"]),
                                            _line("Check-in entries", [t for t in tr if t.get("origin") == "checkin"])])))
        kinds = {"Stopped out": ("stop",), "Hit target": ("target",), "Time/close exit": ("time", "end of day"),
                 "Your early exits": ("manual",)}
        out.append(" | ".join(filter(None, [_line(lab, [t for t in tr if str(t.get("exit_reason", "")).startswith(ks)])
                                            for lab, ks in kinds.items()])))
        conf = [t for t in tr if isinstance(t.get("confidence"), (int, float))]
        out.append(" | ".join(filter(None, [_line("Confidence >= 0.55", [t for t in conf if t["confidence"] >= 0.55]),
                                            _line("Confidence < 0.55", [t for t in conf if t["confidence"] < 0.55])])))
        mfe = [t["mfe_r"] for t in tr if t.get("mfe_r") is not None]
        rs = [t["r"] for t in tr if t.get("r") is not None]
        if mfe and rs:
            reached = sum(1 for t in tr if (t.get("mfe_r") or 0) >= 1)
            out.append(f"Profit capture: best open profit averaged {sum(mfe) / len(mfe):+.2f}R but trades finished at "
                       f"{sum(rs) / len(rs):+.2f}R; {reached} of {len(tr)} trades were up 1R or more at some point.")
        return "\n".join(x for x in out if x)

    def mgmt_text(self):
        rows = self.state["mgmt"]
        if not rows:
            return "(no interventions measured yet)"
        names = {"exit": "Early exits", "adjust": "Stop/target changes", "cancel": "Cancelled orders",
                 "replace": "Orders replaced by a new plan"}
        out = []
        for key, label in names.items():
            g = [m["delta_r"] for m in rows if m["action"] == key]
            if g:
                total = sum(g)
                out.append(f"{label} {len(g)}x: {total:+.1f}R vs leaving the plan alone "
                           f"({sum(1 for d in g if d > 0)} helped, {sum(1 for d in g if d < 0)} hurt)")
        return " | ".join(out) + " (positive = your intervention beat the untouched plan)"

    def base_rates_text(self, window=BASE_WINDOW):
        rows = [b for b in self.state["base"] if self.day_index - b["di"] <= window]
        if not rows:
            return "(none yet: base rates build up after each session)"
        days = len({b["day"] for b in rows})
        recent = self.day_index - 5
        out = [f"Last {days} session{'s' if days != 1 else ''}, code-simulated on the scanner's top names with your "
               f"fill rules (stop wins ties, slippage, no target on the entry bar). R per trade:"]
        for key, label in SETUP_NAMES.items():
            g = [b for b in rows if b["setup"] == key]
            s = _stats(g)
            if not s:
                continue
            n, avg, win, _ = s
            last = _stats([b for b in g if b["di"] > recent])
            parts = [f"- {label}: {n} trades, {avg:+.2f}R avg, {win * 100:.0f}% won"
                     + (f"; last 5 sessions {last[1]:+.2f}R (n={last[0]})" if last else "")]
            by_gap = []
            for bucket in ("gap <1%", "gap 1-3%", "gap 3%+"):
                gs = _stats([b for b in g if b.get("gap") == bucket])
                if gs and gs[0] >= 5:
                    by_gap.append(f"{bucket} {gs[1]:+.2f}R (n={gs[0]})")
            if by_gap:
                parts.append("  by gap size: " + ", ".join(by_gap))
            out.append("\n".join(parts))
        return "\n".join(out)

    def base_rates_for(self, tickers, window=BASE_WINDOW):
        """Recent base-rate results on specific stocks, newest first, for research."""
        want = set(tickers)
        rows = [b for b in self.state["base"] if b["ticker"] in want and self.day_index - b["di"] <= window]
        out = {}
        for b in sorted(rows, key=lambda b: -b["di"]):
            out.setdefault(b["ticker"], []).append(f"{b['day']} {b['setup']} {b['r']:+.1f}R")
        return {tk: v[:6] for tk, v in out.items()}

    def evolve(self, day, max_rules):
        """Evidence-based lifecycle: promote, demote, retire, hide stale rules, enforce the size cap."""
        changes = []
        for r in self.active_rules(True):
            n, s = r["uses"], shrunk_r(r)
            if r["status"] == "trial" and n >= PROMOTE[0] and s > PROMOTE[1]:
                r["status"] = "active"
                self._bump(day, {"op": "promote", "id": r["id"], "shrunk_r": round(s, 3)})
                changes.append(f"promoted {r['id']}")
            elif r["status"] == "active" and n >= DEMOTE[0] and s < DEMOTE[1]:
                r["status"] = "trial"
                self._bump(day, {"op": "demote", "id": r["id"], "shrunk_r": round(s, 3)})
                changes.append(f"demoted {r['id']}")
            if n >= RETIRE[0] and s < RETIRE[1]:
                self.retire_rule(r["id"], day, f"losing on evidence: {n} trades, {r['sum_r']:+.2f}R")
                changes.append(f"retired {r['id']}")
            elif n < 3 and r["harmful"] - r["helpful"] >= OPINION_RETIRE:
                self.retire_rule(r["id"], day, f"untested and judged harmful {r['harmful']}x vs helpful {r['helpful']}x")
                changes.append(f"retired {r['id']}")
            elif (r["status"] != "dormant" and self.day_index - r["last_cited"] >= DORMANT_DAYS
                  and r["source"] != "seed" and not (n >= 3 and r["sum_r"] > 0)):
                r["status"] = "dormant"
                self._bump(day, {"op": "dormant", "id": r["id"]})
                changes.append(f"dormant {r['id']}")
        live = self.active_rules()
        if len(live) > max_rules:
            live.sort(key=lambda r: (r["status"] == "active", shrunk_r(r) + 0.05 * (r["helpful"] - r["harmful"])))
            for r in live[:len(live) - max_rules]:
                self.retire_rule(r["id"], day, "playbook over its size cap")
                changes.append(f"capped {r['id']}")
        return changes

    def playbook_text(self):
        rows = sorted(self.active_rules(), key=lambda r: (r["status"] != "active", -shrunk_r(r)))
        lines = []
        for r in rows:
            if r["uses"]:
                win = r["wins"] / r["uses"] * 100
                stat = f"{r['uses']} trades, {win:.0f}% wins, {r['sum_r']:+.1f}R"
            else:
                stat = "no trades yet"
            lines.append(f"[{r['id']}] ({r['status']}, {stat}) {r['text']}")
        return "\n".join(lines) or "(empty: no rules yet; use your judgment and write some tonight)"

    # ------------------------------------------------------------ lessons

    @property
    def lessons(self):
        return self.state["lessons"]

    def add_lesson(self, text, day, tags=(), tickers=(), importance=5, fingerprint=()):
        """Store a lesson, or reinforce the existing lesson that already says it. Returns the lesson;
        a reinforced one keeps its original `day` and gets `reinforced_on` = this day."""
        text = " ".join(str(text).split())[:500]
        if not text:
            return None
        try:
            importance = int(min(10, max(1, int(importance or 5))))
        except (TypeError, ValueError):
            importance = 5
        twin = max(self.lessons, key=lambda l: similarity(text, l["text"]), default=None)
        if twin and similarity(text, twin["text"]) >= SIMILAR_LESSON:
            twin["importance"] = min(10, max(twin["importance"], importance) + 1)
            twin["day_index"], twin["reinforced_on"] = self.day_index, day
            twin["reinforced"] = twin.get("reinforced", 0) + 1
            twin["fp"] = list(dict.fromkeys(twin.get("fp", []) + list(fingerprint)))[:24]
            twin["tickers"] = list(dict.fromkeys(twin["tickers"] + [str(t).upper() for t in tickers]))[:6]
            if twin["importance"] >= PROMOTE_IMPORTANCE:
                twin["layer"] = "long"
            return twin
        lid = f"L{self.state['next_lesson']}"
        self.state["next_lesson"] += 1
        lesson = {"id": lid, "text": text, "day": day, "day_index": self.day_index,
                  "tags": [str(t).lower()[:24] for t in tags][:6], "tickers": [str(t).upper() for t in tickers][:6],
                  "fp": list(dict.fromkeys(fingerprint))[:24], "importance": importance,
                  "layer": "long" if importance >= PROMOTE_IMPORTANCE else "short", "hits": 0}
        self.lessons.append(lesson)
        return lesson

    def recall(self, query_text, tickers=(), k=6, fingerprint=()):
        """Top-k lessons by recency + importance + relevance, each roughly on a 0-1 scale."""
        if not self.lessons or k <= 0:
            return []
        q = tokens(query_text) + [t.lower() for t in tickers] + list(fingerprint)
        docs = [tokens(l["text"]) + l["tags"] + [t.lower() for t in l["tickers"]] + l.get("fp", [])
                for l in self.lessons]
        rel = bm25_scores(q, docs)
        top = max(rel) if rel and max(rel) > 0 else 1.0
        want = {t.upper() for t in tickers}
        scored = []
        for lesson, r in zip(self.lessons, rel):
            age = max(0, self.day_index - lesson["day_index"])
            recency = 0.5 ** (age / HALF_LIFE[lesson["layer"]])
            bonus = 0.3 if want & set(lesson["tickers"]) else 0.0
            scored.append((recency + lesson["importance"] / 10 + r / top + bonus, lesson))
        scored.sort(key=lambda x: -x[0])
        picked = [l for _, l in scored[:k]]
        for l in picked:
            if l.get("last_hit") != self.day_index:  # hits count days a lesson was useful, not lookups
                l["last_hit"] = self.day_index
                l["hits"] += 1
        return picked

    def end_day(self):
        """Advance the memory clock, promote reinforced short-term lessons, forget stale ones."""
        self.state["day_index"] += 1
        keep = []
        for l in self.lessons:
            if l["layer"] == "short" and (l["hits"] >= 3 or l["importance"] >= PROMOTE_IMPORTANCE):
                l["layer"] = "long"
            if l["layer"] == "short" and self.day_index - l["day_index"] > SHORT_MAX_AGE:
                continue
            keep.append(l)
        self.state["lessons"] = keep[-400:]

    @staticmethod
    def lessons_text(lessons):
        return "\n".join(f"- ({l['day']}, importance {l['importance']}) {l['text']}" for l in lessons) or "(none yet)"
