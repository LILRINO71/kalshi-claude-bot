"""One trading day, the same way in a backtest and live:

    pre-market scan -> morning brief (focus list) -> research (trade plans) -> intraday bars and
    check-ins -> flatten before the close -> reflection and playbook evolution after the close.

The session never reads data directly: it asks a View, which only returns what was known at the
view's `now`. A backtest builds views at historical times; live trading builds them at the real
time. Claude's decisions take effect only after its real response time.
"""
import re

from . import clock, features
from .broker import Broker
from .llm import LLMError
from .universe import INDEX_ETFS, STOCKS, aliases, name_of, sector_of

CONTEXT = list(INDEX_ETFS)


_patterns = {}


def _pattern(tk):
    """Company names as whole words (all-caps names like UPS must match in caps), plus the ticker in
    caps; tickers of one or two letters only count as "(F)" or "$F" so words like "AI" don't match."""
    parts = []
    for a in aliases(tk):
        if len(a) > 2:
            word = r"\b" + re.escape(a) + r"\b"
            parts.append(word if a.isupper() else f"(?i:{word})")
    parts.append(r"\$" + re.escape(tk) + r"\b|\(" + re.escape(tk) + r"\)" if len(tk) <= 2 else r"\b" + re.escape(tk) + r"\b")
    return re.compile("|".join(parts))


def mentioned(title, tickers):
    """Tickers a headline is about."""
    out = []
    for tk in tickers:
        if tk not in _patterns:
            _patterns[tk] = _pattern(tk)
        if _patterns[tk].search(title):
            out.append(tk)
    return out


class StopRun(RuntimeError):
    """A fatal model error (usage limit, login, missing CLI): stop the whole run."""


class DaySession:
    def __init__(self, day, cfg, data, brain, memory, equity, log=print):
        self.day, self.cfg, self.data, self.brain, self.memory = day, cfg, data, brain, memory
        self.log_fn = log
        self.interval_s = data.interval_s
        self.broker = Broker(cfg, equity, clock.at(day, "09:30"))
        self.universe = cfg["universe"] or list(STOCKS)
        self.scan, self.focus, self.plans, self.checkins, self.notes = [], [], [], [], []
        self.brief, self.reflection = None, None
        self.processed = {}
        self._recorded = False
        self.calls = {"made": 0, "cached": 0, "errors": 0, "latency_s": 0.0}
        self.market_news, self.ticker_news, self.fps, self.market_fp = [], {}, {}, []
        close = data.session_close(day)
        self.close_ts = close
        flatten = clock.at(day, cfg["flatten_time"])
        self.flatten_ts = min(flatten, close - 5 * 60)

    # ------------------------------------------------------------ helpers

    def log(self, msg, kind="info", ts=None):
        self.broker.log(ts or clock.now_ts(), msg, kind)
        self.log_fn(f"{self.day} {clock.hhmm(ts) if ts else ''} {msg}".replace("  ", " "))

    def _ask(self, step, fn, ctx, now):
        """Run one brain step; returns (answer, latency). Fatal model errors stop the run."""
        try:
            answer, meta = fn(ctx)
        except LLMError as e:
            self.calls["errors"] += 1
            self.notes.append({"t": now, "step": step, "error": str(e), "kind": e.kind})
            self.log(f"{step}: model call failed ({e.kind}): {e}", "error", now)
            if e.fatal:
                raise StopRun(str(e))
            return None, 0.0
        if meta.get("llm"):
            self.calls["cached" if meta.get("cached") else "made"] += 1
            self.calls["latency_s"] += meta.get("latency_s", 0)
        return answer, meta.get("latency_s", 0.0)

    def _daily(self, view, tk):
        return features.daily_stats(view.daily(tk, self.day))

    def _bars(self, view, tk):
        return features.split_session(view.intraday(tk, self.day), self.day)

    # ------------------------------------------------------------ pre-market

    def premarket(self, now):
        view = self.data.view(now)
        prev_close_ts = clock.at(self.data.prev_trading_day(self.day), "16:00")
        self.market_news = view.news_market(prev_close_ts, now, self.cfg["market_news"])
        mentions = {}
        for item in self.market_news:
            for tk in mentioned(item["title"], self.universe):
                mentions[tk] = mentions.get(tk, 0) + 1
        rows = []
        for tk in self.universe + CONTEXT:
            d = self._daily(view, tk)
            if not d:
                continue
            pre, _ = self._bars(view, tk)
            pm = features.premarket_stats(pre, d["prev_close"])
            filings = view.sec_filings(tk, prev_close_ts - 4 * 3600, now) if tk not in CONTEXT else []
            catalyst = 1.0 * any("earnings" in f["title"] for f in filings) + 0.5 * bool(filings)
            fp = features.fingerprint(d, pm, sector_of(tk), mentions.get(tk, 0))
            if any("earnings" in f["title"] for f in filings):
                fp.append("earnings")
            rows.append({"ticker": tk, "name": name_of(tk), "sector": sector_of(tk), "daily": d, "pm": pm,
                         "news_mentions": mentions.get(tk, 0), "filings": filings, "fp": fp,
                         "score": features.scan_score(d, pm) + catalyst})
        context = {r["ticker"]: r for r in rows if r["ticker"] in CONTEXT}
        stocks = [r for r in rows if r["ticker"] not in CONTEXT]
        stocks.sort(key=lambda r: (r["score"] + 0.5 * min(r["news_mentions"], 3)), reverse=True)
        self.scan = stocks[:self.cfg["scan_top"]]
        if not self.scan:
            self.log("No scan data for this day (missing bars); skipping", "warn", now)
            return False

        self.market_fp = features.market_fingerprint((context.get("SPY") or {}).get("pm"))
        query = " ".join(i["title"] for i in self.market_news[:15])
        ctx = {"day": self.day, "now": now, "scan": self.scan, "context": context, "market_news": self.market_news,
               "playbook": self.memory.playbook_text(),
               "lessons": self.memory.recall(query, [r["ticker"] for r in self.scan[:8]], self.cfg["memory_k"],
                                             self.market_fp),
               "recent_days": self.memory.journal()[-3:], "focus_max": self.cfg["focus_max"], "cfg": self.cfg}
        brief, _ = self._ask("morning brief", self.brain.morning, ctx, now)
        if not brief:
            return False
        known = {r["ticker"] for r in self.scan} | set(self.universe)
        focus = []
        for f in brief.get("focus") or []:
            tk = str(f.get("ticker", "")).upper()
            if tk in known and tk not in [x["ticker"] for x in focus]:
                focus.append({**f, "ticker": tk})
        self.brief = {**brief, "focus": focus[:self.cfg["focus_max"]]}
        self.focus = [f["ticker"] for f in self.brief["focus"]]
        self.market_fp += features.market_fingerprint(None, brief.get("regime", ""))
        for f in self.brief["focus"]:
            self.memory.cite(f.get("rules_used"))
        self.log(f"Focus: {', '.join(self.focus) or 'none'} | {brief.get('market_view', '')[:160]}", "brief", now)
        if not self.focus:
            return True

        research = []
        for f in self.brief["focus"]:
            tk = f["ticker"]
            d = self._daily(view, tk)
            pre, _ = self._bars(view, tk)
            news = view.news_ticker(tk, now - 3 * 86400, now, self.cfg["news_per_ticker"])
            self.ticker_news[tk] = news
            pm = features.premarket_stats(pre, d["prev_close"]) if d else None
            fp = features.fingerprint(d, pm, sector_of(tk), 1 if news else 0) + self.market_fp
            self.fps[tk] = fp
            research.append({"ticker": tk, "name": name_of(tk), "sector": sector_of(tk), "focus": f, "daily": d,
                             "pm": pm, "pm_bars": features.compress(pre[-36:], 1800) if pre else [], "news": news,
                             "lessons": self.memory.recall(f"{tk} {sector_of(tk)} {f.get('why', '')}", [tk],
                                                           max(2, self.cfg["memory_k"] // 2), fp)})
        ctx = {"day": self.day, "now": now, "brief": self.brief, "research": research, "context": context,
               "playbook": self.memory.playbook_text(), "cfg": self.cfg}
        answer, latency = self._ask("research", self.brain.research, ctx, now)
        if answer:
            self._place_plans(answer.get("plans") or [], max(now + latency, clock.at(self.day, "09:30")), "premarket",
                              decided_at=now)
            self.brief["watch_only"] = answer.get("watch_only") or []
        return True

    # ------------------------------------------------------------ plans and actions

    def _ref_price(self, tk, at_ts):
        view = self.data.view(at_ts)
        pre, reg = self._bars(view, tk)
        bars = reg or pre
        if bars:
            return bars[-1][4]
        d = self._daily(view, tk)
        return d["prev_close"] if d else None

    def _place_plans(self, plans, active_from, origin, decided_at=None):
        """Turn plans into broker orders. Prices are checked against the price Claude saw when it decided;
        what the market did after that (a gap at the open, say) is the broker's job."""
        for i, p in enumerate(plans):
            tk = str(p.get("ticker", "")).upper()
            plan = {**p, "ticker": tk, "id": f"{self.day}-{origin}-{len(self.plans) + 1}", "origin": origin,
                    "placed": active_from}
            self.plans.append(plan)
            if tk not in self.universe:
                plan["status"] = "rejected: not in universe"
                continue
            ref = self._ref_price(tk, decided_at or active_from)
            err = self._sanity(plan, ref)
            if err:
                plan["status"] = f"rejected: {err}"
                self.log(f"{tk} plan rejected: {err}", "warn", active_from)
                continue
            start = max(active_from, self._t(p.get("valid_from"), clock.at(self.day, "09:35"))) \
                if origin == "premarket" else active_from
            order = {"ticker": tk, "side": p.get("side"), "kind": p.get("entry_type"),
                     "price": p.get("entry"), "stop": p.get("stop"), "target": p.get("target"),
                     "active_from": start, "expires": self._t(p.get("valid_until"), self.flatten_ts),
                     "time_exit": self._t(p.get("time_exit"), None), "breakeven_at": p.get("breakeven_at"),
                     "size": p.get("size", 1.0), "plan_id": plan["id"], "rules": (p.get("rules_used") or [])[:3],
                     "setup": p.get("setup", ""), "confidence": p.get("confidence")}
            o, err = self.broker.submit(order, active_from)
            plan["status"] = f"rejected: {err}" if err else "working"
            plan["order_id"] = o["id"]
            self.memory.cite(order["rules"])

    def _t(self, hhmm, default):
        if not hhmm:
            return default
        try:
            return min(clock.at(self.day, hhmm), self.flatten_ts)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _sanity(p, ref):
        side, stop, entry = p.get("side"), p.get("stop"), p.get("entry")
        if side not in ("long", "short"):
            return "side must be long or short"
        if p.get("entry_type") not in ("stop", "limit", "market"):
            return "entry_type must be stop, limit or market"
        if not isinstance(stop, (int, float)) or stop <= 0:
            return "missing stop"
        if ref is None:
            return "no price data"
        level = entry if p.get("entry_type") in ("stop", "limit") else ref
        if not isinstance(level, (int, float)) or level <= 0:
            return "missing entry price"
        if abs(level / ref - 1) > 0.15:
            return f"entry {level} is more than 15% from the price {ref:.2f}"
        kind = p.get("entry_type")
        # Buy stops and sell limits belong above the price, sell stops and buy limits below it. A level
        # within 0.3% of the price just means "enter near the open", so it is let through.
        above = kind == "stop" if side == "long" else kind == "limit"
        slack = ref * 0.003
        if kind in ("stop", "limit") and (level < ref - slack if above else level > ref + slack):
            return (f"a {side} {kind} at {level} is {'below' if above else 'above'} the price {ref:.2f} and would "
                    f"fill at once; use a {'limit' if kind == 'stop' else 'stop'} order or a market entry")
        target = p.get("target")
        if target and ((side == "long" and target <= level) or (side == "short" and target >= level)):
            return f"target {target} is on the wrong side of the entry {level}"
        risk = abs(level - stop) / level
        if risk < 0.001 or risk > 0.08:
            return f"stop distance {risk * 100:.2f}% is outside 0.1%-8%"
        return None

    def _apply_actions(self, actions, at_ts, decided_at=None):
        b = self.broker
        new_plans = []
        for a in actions:
            tk = str(a.get("ticker", "")).upper()
            act = a.get("action")
            err = None
            if act == "exit_now":
                err = b.exit_at_next_bar(tk, at_ts)
            elif act == "cancel":
                b.schedule(at_ts, tk, "cancel", why=a.get("why", "check-in"))
            elif act == "adjust":
                # Like every other action, it only takes effect once the answer exists.
                b.schedule(at_ts, tk, "adjust", stop=a.get("stop"), target=a.get("target"),
                           time_exit=self._t(a.get("time_exit"), None))
            elif act == "new_entry":
                new_plans.append(a)
            elif act not in ("hold", None):
                err = f"unknown action {act}"
            if err:
                self.log(f"{tk} {act} ignored: {err}", "warn", at_ts)
        if new_plans:
            self._place_plans(new_plans, at_ts, "checkin", decided_at)

    # ------------------------------------------------------------ intraday

    def tickers_in_play(self):
        b = self.broker
        return sorted(set(self.focus) | set(b.positions) | {o["ticker"] for o in b.working()})

    def advance_to(self, t):
        """Feed every regular-session bar that finished by time t to the broker, in time order."""
        view = self.data.view(t)
        queue = []
        for tk in self.tickers_in_play():
            _, reg = self._bars(view, tk)
            last = self.processed.get(tk, -1)
            queue.extend((bar[0], tk, bar) for bar in reg if bar[0] > last)
        for ts, tk, bar in sorted(queue):
            self.broker.on_bar(tk, bar)
            self.processed[tk] = ts

    def checkin(self, now):
        if now >= self.flatten_ts or not self.brain.does_checkins:
            return
        self.advance_to(now)
        if not self.tickers_in_play():
            return  # nothing focused, held or working: no call needed
        view = self.data.view(now)
        b = self.broker
        context = {}
        for tk in CONTEXT[:2]:
            d = self._daily(view, tk)
            _, reg = self._bars(view, tk)
            context[tk] = features.intraday_stats(reg, d["prev_close"] if d else None, d["avg_volume"] if d else 0,
                                                  self.interval_s)
        tickers = []
        for tk in self.tickers_in_play():
            d = self._daily(view, tk)
            _, reg = self._bars(view, tk)
            st = features.intraday_stats(reg, d["prev_close"] if d else None, d["avg_volume"] if d else 0,
                                         self.interval_s)
            news = view.news_ticker(tk, clock.at(self.day, "09:30"), now, 5)
            tickers.append({"ticker": tk, "daily": d, "stats": st,
                            "bars": features.compress(reg, 1800, clock.at(self.day, "09:30")),
                            "position": b.positions.get(tk), "orders": b.working(tk),
                            "plans": [p for p in self.plans if p["ticker"] == tk][-2:], "news": news})
        ctx = {"day": self.day, "now": now, "close_ts": self.flatten_ts, "tickers": tickers, "context": context,
               "equity": b.equity(), "start_equity": b.start_equity, "realized": b.realized, "blocked": b.blocked,
               "trades_today": b.trades, "brief": self.brief, "playbook": self.memory.playbook_text(), "cfg": self.cfg}
        answer, latency = self._ask(f"check-in {clock.hhmm(now)}", self.brain.checkin, ctx, now)
        row = {"t": now, "answer": answer, "latency_s": latency}
        self.checkins.append(row)
        if answer:
            self.log(f"Check-in: {str(answer.get('market_read', ''))[:160]}", "checkin", now)
            self._apply_actions(answer.get("actions") or [], now + int(latency) + 1, now)

    def close(self):
        self.advance_to(self.flatten_ts)
        view = self.data.view(self.flatten_ts + self.interval_s)
        prices = {}
        for tk in list(self.broker.positions):
            _, reg = self._bars(view, tk)
            bar = next((x for x in reg if x[0] >= self.flatten_ts), None)
            prices[tk] = bar[1] if bar else self.broker.last_price.get(tk)
        self.broker.flatten(self.flatten_ts, prices, "end of day")

    # ------------------------------------------------------------ after the close

    def reflect(self, now, learn=True):
        view = self.data.view(now)
        b = self.broker
        if not self._recorded:  # once per day, even if reflect is retried after a failed model call
            self._recorded = True
            for t in b.trades:
                self.memory.record_trade(t)
        day_moves = []
        for r in self.scan[:12]:
            tk = r["ticker"]
            _, reg = self._bars(view, tk)
            st = features.intraday_stats(reg, r["daily"]["prev_close"], r["daily"]["avg_volume"], self.interval_s)
            if st:
                day_moves.append({"ticker": tk, "focus": tk in self.focus, "gap_pct": (r["pm"] or {}).get("gap_pct"),
                                  "open_to_close_pct": st["chg_from_open_pct"],
                                  "high_pct": (st["high"] / st["open"] - 1) * 100,
                                  "low_pct": (st["low"] / st["open"] - 1) * 100,
                                  "close_vs_vwap": "above" if st["last"] >= st["vwap"] else "below"})
        result = {"day": self.day, "pnl": round(b.realized, 2), "trades": len(b.trades),
                  "start_equity": b.start_equity, "end_equity": round(b.start_equity + b.realized, 2)}
        changes = []
        if learn and self.brain.learns:
            ctx = {"day": self.day, "now": now, "brief": self.brief, "plans": self.plans, "trades": b.trades,
                   "events": b.events[-60:], "checkins": self.checkins, "day_moves": day_moves, "result": result,
                   "rules": self.memory.active_rules(), "playbook": self.memory.playbook_text(),
                   "retired": [r for r in self.memory.rules if r["status"] == "retired"],
                   "recent_days": self.memory.journal()[-5:], "cfg": self.cfg}
            ans, _ = self._ask("reflection", self.brain.reflect, ctx, now)
            self.reflection = ans
            if ans:
                changes += self._learn(ans)
            changes += self.memory.evolve(self.day, self.cfg["max_rules"])
            if self.cfg["curate_every"] and (self.memory.state["day_index"] + 1) % self.cfg["curate_every"] == 0:
                changes += self._curate(now)
        self.memory.end_day()
        self.memory.save()
        row = {**result, "focus": self.focus, "market_view": (self.brief or {}).get("market_view", ""),
               "summary": (self.reflection or {}).get("summary", ""), "plans": len(self.plans),
               "rule_changes": changes, "playbook_version": self.memory.state["version"], "calls": self.calls}
        self.memory.write_journal(row)
        return row

    def _learn(self, ans):
        m, day, changes = self.memory, self.day, []
        scan_fp = {r["ticker"]: r.get("fp", []) for r in self.scan}
        for les in (ans.get("lessons") or [])[:5]:
            tks = [str(t).upper() for t in les.get("tickers") or []]
            fp = list(self.market_fp) + [tok for t in tks for tok in self.fps.get(t) or scan_fp.get(t) or []]
            if m.add_lesson(les.get("text", ""), day, les.get("tags") or [], tks, les.get("importance", 5), fp):
                changes.append("lesson")
        for fb in ans.get("rule_feedback") or []:
            m.apply_feedback(str(fb.get("rule_id", "")), fb.get("verdict"))
        for ed in (ans.get("edit_rules") or [])[:3]:
            if m.edit_rule(str(ed.get("rule_id", "")), ed.get("text", ""), day):
                changes.append(f"edited {ed.get('rule_id')}")
        for rt in (ans.get("retire_rules") or [])[:3]:
            r = m.rule(str(rt.get("rule_id", "")))
            # A reflection may retire an untested idea, but proven rules only go on the evidence.
            if r and not (r["uses"] >= 3 and r["sum_r"] > 0) and m.retire_rule(r["id"], day, rt.get("why", "")):
                changes.append(f"retired {r['id']}")
        for nr in (ans.get("new_rules") or [])[:3]:
            rule = m.add_rule(nr.get("text", ""), nr.get("tags") or [], day)
            if rule:
                changes.append(f"added {rule['id']}")
        return changes

    def _curate(self, now):
        """Weekly consolidation through small delta operations, never a full rewrite."""
        m = self.memory
        ctx = {"day": self.day, "rules": m.active_rules(True), "journal": m.journal()[-10:], "cfg": self.cfg,
               "retired": [r for r in m.rules if r["status"] == "retired"]}
        ans, _ = self._ask("curation", self.brain.curate, ctx, now)
        if not ans:
            return []
        changes, budget = [], {"add": 3, "edit": 3, "merge": 3, "retire": 3, "revive": 3}
        for op in ans.get("operations") or []:
            kind, ids = op.get("op"), [str(i) for i in op.get("rule_ids") or []]
            if kind not in budget or budget[kind] <= 0:
                continue
            budget[kind] -= 1
            if kind == "add":
                r = m.add_rule(op.get("text", ""), op.get("tags") or [], self.day, source="curator")
                changes += [f"added {r['id']}"] if r else []
            elif kind == "edit" and ids and m.edit_rule(ids[0], op.get("text", ""), self.day, op.get("why", "")):
                changes.append(f"edited {ids[0]}")
            elif kind == "merge":
                r = m.merge_rules(ids, op.get("text", ""), op.get("tags") or [], self.day, op.get("why", ""))
                changes += [f"merged {'+'.join(ids)}->{r['id']}"] if r else []
            elif kind == "revive" and ids and m.revive_rule(ids[0], self.day, op.get("why", "")):
                changes.append(f"revived {ids[0]}")
            elif kind == "retire" and ids:
                r = m.rule(ids[0])
                proven = r and r["uses"] >= 3 and r["sum_r"] > 0
                if r and not proven and m.retire_rule(ids[0], self.day, op.get("why", "curation")):
                    changes.append(f"retired {ids[0]}")
        return changes
