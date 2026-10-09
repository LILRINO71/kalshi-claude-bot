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
  from similar situations, not just similar words.
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

    def add_rule(self, text, tags=(), day="", source="reflection", status="trial"):
        text = " ".join(str(text).split())[:400]
        if not text or any(r["text"].lower() == text.lower() for r in self.active_rules()):
            return None
        dormant = next((r for r in self.rules if r["status"] == "dormant" and r["text"].lower() == text.lower()), None)
        if dormant:  # the same idea again: wake the old rule up, with its evidence
            return self.revive_rule(dormant["id"], day, "proposed again")
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
            r["status"] == "active" for r in olds) else "trial")
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
        text = " ".join(str(text).split())[:500]
        if not text:
            return None
        importance = int(min(10, max(1, int(importance or 5))))
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
