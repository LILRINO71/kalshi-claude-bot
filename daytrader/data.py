"""Market data with no API keys or accounts.

- Bars: Yahoo Finance's public chart API. Intraday bars (with pre- and post-market) only go back
  about 60 days (5m/15m) or 30 days (1m), so a backtest can only reach that far back, but every
  completed day is cached on disk forever: the longer you use it, the more history you have.
- News: SEC EDGAR filings with their exact acceptance times (earnings 8-Ks, offerings, deals),
  Google News search (dated to the day for past results, so each item is held back until its day
  is over) and, when it isn't rate-limited, the GDELT DOC API (exact to ~15 minutes). Everything is
  filtered by when it became known, so a backtest never shows news from after the simulated moment.

Every read goes through a View, which hides anything after its `now`.
"""
import email.utils
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
import requests

from . import alpaca, clock, store
from .universe import INDEX_ETFS, STOCKS, aliases

INTERVAL_S = {"1m": 60, "2m": 120, "5m": 300, "15m": 900}
EARLY_CLOSES = {"2026-11-27", "2026-12-24", "2027-11-26"}  # NYSE 13:00 closes (others are detected from bars)
LOOKBACK_DAYS = {"1m": 29, "2m": 59, "5m": 59, "15m": 59}   # how far back Yahoo serves each interval
SPAN_DAYS = {"1m": 7, "2m": 30, "5m": 30, "15m": 30}         # days fetched per request

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
GNEWS = "https://news.google.com/rss/search"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{:010d}.json"
SEC_HEADER = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{acc}.hdr.sgml"
# SEC asks automated tools to identify themselves; set your own contact in settings (sec_user_agent).
SEC_UA = "ai-day-trader research@example.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")

_session = requests.Session()
_session.headers["User-Agent"] = UA
_lock = threading.Lock()
_last = {}
_GAP = {"yahoo": 0.35, "gnews": 1.0, "gdelt": 5.5, "sec": 0.15}
_BACKOFF_S = {"gdelt": 1800, "gnews": 300, "sec": 300}


_backoff = {}  # host -> time before which we don't try it again
FAILURES = {}  # host -> requests that failed after retries (skips during a backoff don't count)


def _get(url, params, host, retries=3, as_json=True, headers=None):
    if _backoff.get(host, 0) > time.time():
        return None
    for attempt in range(retries):
        with _lock:  # reserve a slot per host; sleep outside the lock so other hosts aren't held up
            slot = max(time.time(), _last.get(host, 0) + _GAP[host])
            _last[host] = slot
        if slot > time.time():
            time.sleep(slot - time.time())
        try:
            r = _session.get(url, params=params, timeout=20, headers=headers)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200:
            if not as_json:
                return r.content  # bytes: let the XML parser read the declared encoding
            try:
                return r.json()
            except ValueError:
                if b"limit requests" in r.content[:200]:  # GDELT's rate-limit notice, sometimes with a 200
                    break
                return None  # GDELT answers bad queries with a plain-text message
        if r.status_code == 429 and host == "gdelt":
            break  # GDELT throttles per IP for a long time; retrying only wastes the run's time
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(5 * (attempt + 1))
            continue
        return None
    FAILURES[host] = FAILURES.get(host, 0) + 1
    if host in _BACKOFF_S:
        _backoff[host] = time.time() + _BACKOFF_S[host]  # a news source still failing: leave it alone a while
    return None


def _parse_chart(js):
    try:
        res = js["chart"]["result"][0]
        ts = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
    except (TypeError, KeyError, IndexError):
        return None
    rows = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        o, h, l, v = q["open"][i], q["high"][i], q["low"][i], q["volume"][i]
        o = c if o is None else o
        rows.append((int(t), round(o, 4), round(max(h if h is not None else c, o, c), 4),
                     round(min(l if l is not None else c, o, c), 4), round(c, 4), int(v or 0)))
    return rows


class MarketData:
    """Historical data source with a permanent disk cache of completed days."""

    def __init__(self, interval="5m", cfg=None):
        cfg = cfg or {}
        self.interval = interval
        self.interval_s = INTERVAL_S[interval]
        self._mem = {}
        self._daily = {}
        self.sec_enabled = cfg.get("use_sec", True)
        self.gdelt_enabled = cfg.get("use_gdelt", True)
        self.sec_user_agent = cfg.get("sec_user_agent") or SEC_UA
        self._ciks, self._subs, self._acc = None, {}, None

    # ------------------------------------------------------------ daily bars

    def daily_all(self, tk, need_day=None):
        """[(day, o, h, l, c, v)] oldest first, as far back as about two years."""
        if tk in self._daily and (need_day is None or (self._daily[tk] and self._daily[tk][-1][0] >= need_day)):
            return self._daily[tk]
        path = store.CACHE / "yahoo" / "1d" / f"{tk}.json"
        cached = store.read_json(path)
        fresh = cached and time.time() - cached.get("fetched", 0) < 6 * 3600
        have = cached and cached["rows"] and (need_day is None or cached["rows"][-1][0] >= need_day)
        if cached and (have or fresh):
            rows = [tuple(r) for r in cached["rows"]]
        else:
            js = _get(YAHOO.format(tk), {"interval": "1d", "range": "2y"}, "yahoo")
            bars = _parse_chart(js) if js else None
            if bars is None:
                rows = [tuple(r) for r in (cached or {}).get("rows", [])]
            else:
                by_day = {}
                for t, o, h, l, c, v in bars:
                    by_day[clock.day_of(t)] = (clock.day_of(t), o, h, l, c, v)
                rows = [by_day[k] for k in sorted(by_day)]
                store.write_json(path, {"fetched": time.time(), "rows": rows})
        self._daily[tk] = rows
        return rows

    def daily(self, tk, before_day, n=60):
        rows = [r for r in self.daily_all(tk, clock.prev_weekday(before_day)) if r[0] < before_day]
        return rows[-n:]

    def trading_days(self, start, end):
        return [r[0] for r in self.daily_all("SPY", end) if start <= r[0] <= end]

    def prev_trading_day(self, day):
        prior = [r[0] for r in self.daily_all("SPY") if r[0] < day]
        return prior[-1] if prior else clock.prev_weekday(day)

    # ------------------------------------------------------------ intraday bars

    def _day_path(self, tk, day):
        return store.CACHE / "yahoo" / self.interval / tk / f"{day}.json"

    def _alpaca_path(self, tk, day):
        return store.CACHE / "alpaca" / self.interval / tk / f"{day}.json"

    def yahoo_oldest(self):
        """The earliest day Yahoo still serves intraday bars of this size for."""
        return (date.today() - timedelta(days=LOOKBACK_DAYS[self.interval])).isoformat()

    def _alpaca_span(self, tickers, first, last, log=None):
        """Fetch older days from Alpaca and cache every (ticker, day) in the span, empty days included."""
        try:
            got = alpaca.fetch(tickers, first, last, self.interval, log=log or (lambda *_: None))
        except alpaca.AlpacaError as e:
            FAILURES["alpaca"] = FAILURES.get("alpaca", 0) + 1
            if log:
                log(f"Alpaca history unavailable: {e}")
            return {}
        d = date.fromisoformat(first)
        while d.isoformat() <= last:
            k = d.isoformat()
            if d.weekday() < 5:
                for tk in tickers:
                    rows = (got.get(tk) or {}).get(k, [])
                    self._mem[(tk, k)] = rows
                    store.write_json(self._alpaca_path(tk, k), rows)
            d += timedelta(days=1)
        return got

    def _complete(self, day):
        return time.time() > clock.at(day, "20:05")

    def _fetch_span(self, tk, first, last):
        """Fetch [first, last] in one request and cache each completed day (empty days too)."""
        p1 = clock.at(first, "04:00")
        p2 = clock.at(last, "20:00")
        js = _get(YAHOO.format(tk), {"interval": self.interval, "period1": p1, "period2": p2,
                                     "includePrePost": "true"}, "yahoo")
        bars = _parse_chart(js) if js else None
        if bars is None:
            return False
        bars = [b for b in bars if b[0] < p2]  # Yahoo appends a stray "now" bar even to past ranges
        by_day = {}
        for b in bars:
            by_day.setdefault(clock.day_of(b[0]), []).append(b)
        d = date.fromisoformat(first)
        while d.isoformat() <= last:
            k = d.isoformat()
            rows = sorted(by_day.get(k, []))
            self._mem[(tk, k)] = rows
            if self._complete(k):
                store.write_json(self._day_path(tk, k), rows)
            d += timedelta(days=1)
        return True

    def intraday(self, tk, day):
        key = (tk, day)
        if key in self._mem:
            return self._mem[key]
        cached = store.read_json(self._day_path(tk, day))
        if cached is None:
            cached = store.read_json(self._alpaca_path(tk, day))
        if cached is not None:
            rows = [tuple(r) for r in cached]
            self._mem[key] = rows
            return rows
        if day < self.yahoo_oldest():
            if alpaca.configured():
                self._alpaca_span([tk], day, day)
            self._mem.setdefault(key, [])
            return self._mem[key]
        last = min((date.fromisoformat(day) + timedelta(days=SPAN_DAYS[self.interval] - 1)).isoformat(),
                   clock.today_et())
        if not self._fetch_span(tk, day, last):
            self._mem[key] = []
            return []
        return self._mem.get(key, [])

    def prefetch(self, tickers, days, log=print):
        """Warm the cache for a backtest: one request per ticker per span instead of one per day."""
        if not days:
            return
        tickers = list(dict.fromkeys(list(tickers or STOCKS) + list(INDEX_ETFS)))
        oldest = self.yahoo_oldest()
        need = [d for d in days if d >= oldest]
        older = [d for d in days if d < oldest]
        missing_old = [d for d in older if any(not self._day_path(tk, d).exists() and not self._alpaca_path(tk, d).exists()
                                               for tk in tickers)]
        if missing_old and alpaca.configured():
            log(f"Fetching {len(missing_old)} older day(s) of {self.interval} bars from Alpaca "
                f"({missing_old[0]} to {missing_old[-1]})")
            for i in range(0, len(missing_old), 20):  # about a month of trading days per span
                span = missing_old[i:i + 20]
                self._alpaca_span(tickers, span[0], span[-1], log=log)
        elif missing_old:
            log(f"Note: Yahoo only serves {self.interval} bars for the last ~{LOOKBACK_DAYS[self.interval]} days; "
                f"{len(missing_old)} earlier day(s) have no bars and will be skipped. A free Alpaca key reaches "
                f"further back: python -m daytrader alpaca KEY SECRET")
        last_day = max(days)
        for i, tk in enumerate(tickers):
            self.daily_all(tk, clock.prev_weekday(last_day))
            missing = [d for d in need if not self._day_path(tk, d).exists()]
            while missing:
                first = missing[0]
                end = min((date.fromisoformat(first) + timedelta(days=SPAN_DAYS[self.interval] - 1)).isoformat(),
                          clock.today_et())
                self._fetch_span(tk, first, end)
                missing = [d for d in missing if d > end]
            if (i + 1) % 20 == 0:
                log(f"Loaded bars for {i + 1}/{len(tickers)} tickers")

    def session_close(self, day):
        if day in EARLY_CLOSES:
            return clock.at(day, "13:00")
        # Yahoo's after-hours bars have no volume, so the regular session is the bars that traded volume.
        bars = [b for b in self.intraday("SPY", day)
                if clock.at(day, "09:30") <= b[0] < clock.at(day, "16:00") and b[5]]
        if not bars:
            return clock.at(day, "16:00")
        end = bars[-1][0] + self.interval_s
        # A half day (13:00 close) shows up as bars that stop early.
        return clock.at(day, "13:00") if end <= clock.at(day, "13:05") and self._complete(day) else clock.at(day, "16:00")

    # ------------------------------------------------------------ news
    #
    # Google News dates historical search results to the day: nearly every item says 07:00 UTC. An item
    # dated day D may have been published at any time on D, so it only becomes visible once D is over
    # (`known_ts`). GDELT's "seen" times are exact to about 15 minutes, so GDELT supplies the overnight,
    # pre-market and intraday headlines of the day being simulated.

    def _news_cache(self, key):
        return store.CACHE / "news" / f"{key}.json"

    def _gnews(self, query, start_day, end_day):
        """Google News RSS search bounded by days."""
        q = f"{query} after:{start_day} before:{end_day}"
        raw = _get(GNEWS, {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}, "gnews", as_json=False)
        if not raw:
            return None
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return None
        items = []
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            pub = it.findtext("pubDate")
            src = it.find("source")
            try:
                ts = int(email.utils.parsedate_to_datetime(pub).timestamp()) if pub else None
            except (TypeError, ValueError):
                ts = None
            if not title or ts is None:
                continue
            source = (src.text or "").strip() if src is not None else ""
            if source and title.endswith(" - " + source):
                title = title[: -len(source) - 3]
            coarse = ts % 3600 == 0  # a date, not a time: assume it came out at the very end of that day
            items.append({"ts": ts, "known_ts": ts + 86400 if coarse else ts, "coarse": coarse, "title": title,
                          "source": source, "url": it.findtext("link") or ""})
        return items

    def _gdelt(self, query, start_ts, end_ts, n=250):
        fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d%H%M%S")
        js = _get(GDELT, {"query": query, "mode": "artlist", "format": "json", "maxrecords": n, "sort": "datedesc",
                          "startdatetime": fmt(start_ts), "enddatetime": fmt(end_ts)}, "gdelt")
        if not isinstance(js, dict):
            return None
        items = []
        for a in js.get("articles") or []:
            try:
                ts = int(datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp())
            except (KeyError, ValueError):
                continue
            # seendate is rounded to GDELT's 15-minute crawl cycle; count it as known at the end of that cycle.
            items.append({"ts": ts, "known_ts": ts + 900, "coarse": False, "title": a.get("title", "").strip(),
                          "source": a.get("domain", ""), "url": a.get("url", "")})
        return items

    def _cached_news(self, key, end_ts, fetch):
        """Fetch once and keep forever when the window is fully in the past."""
        path = self._news_cache(key)
        cached = store.read_json(path)
        if cached is not None:
            return cached
        items = fetch()
        if items is None:
            return []
        if end_ts < time.time() - 3600:
            store.write_json(path, items)
        return items

    def _gdelt_day(self, key, query, day):
        """Exact-time headlines from the previous close to this day's close, in two windows so the busy
        session can't crowd the overnight and pre-market news out of the result limit."""
        if not self.gdelt_enabled:
            return []
        pc = clock.at(self.prev_trading_day(day), "16:00")
        op, cl = clock.at(day, "09:30"), clock.at(day, "16:00")
        out = []
        for part, (a, b) in (("pre", (pc, op)), ("rth", (op, cl))):
            out += self._cached_news(f"{key}_{day}_{part}", b, lambda a=a, b=b: self._gdelt(query, a, b))
        return out

    def _gnews_days(self, key, query, day):
        """Google News for the three days before `day` (date-stamped, so only useful for earlier days)."""
        first = (date.fromisoformat(day) - timedelta(days=4)).isoformat()
        last = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        return self._cached_news(f"{key}_{first}_{last}", clock.at(last, "03:00"),
                                 lambda: self._gnews(query, first, last))

    def news_market(self, start_ts, end_ts, limit=20):
        day = clock.day_of(end_ts)
        items = []
        for i, q in enumerate(GNEWS_MARKET):
            items += self._gnews_days(f"market{i}", q, day)
        items += [i for i in self._gdelt_day("gd_market", GDELT_MARKET, day) if _domain(i) in NEWS_DOMAINS]
        # Keep room for both: timed headlines from the last hours and date-only ones from the day before.
        timed = _dedupe([i for i in items if not i.get("coarse")], start_ts, end_ts, limit)
        dated = _dedupe([i for i in items if i.get("coarse")], start_ts, end_ts, limit)
        keep = timed[:max(limit - len(dated[:limit // 2]), limit // 2)]
        return sorted(keep + dated[:limit - len(keep)], key=lambda i: -i["ts"])

    def news_ticker(self, tk, start_ts, end_ts, limit=8):
        day = clock.day_of(end_ts)
        names = [a for a in aliases(tk) if len(a) > 2] or [tk]
        quoted = " OR ".join(f'"{a}"' for a in names[:3])
        items = self.sec_filings(tk, start_ts, end_ts)
        items += self._gnews_days(f"t_{tk}", f"({quoted}) ({tk} OR stock OR shares)", day)
        gq = (f"({quoted})" if len(names) > 1 else quoted) + " sourcelang:english"
        items += [i for i in self._gdelt_day(f"gd_{tk}", gq, day) if _domain(i) not in JUNK_DOMAINS]
        filings = _dedupe([i for i in items if i.get("source") == "SEC EDGAR"], start_ts, end_ts, 5)
        rest = _dedupe([i for i in items if i.get("source") != "SEC EDGAR"], start_ts, end_ts, limit)
        return sorted(filings + rest, key=lambda i: -i["ts"])

    # ------------------------------------------------------------ SEC filings

    def _sec_get(self, url, as_json=True):
        return _get(url, None, "sec", as_json=as_json, headers={"User-Agent": self.sec_user_agent})

    def _cik(self, tk):
        if self._ciks is None:
            path = store.CACHE / "sec" / "company_tickers.json"
            cached = store.read_json(path)
            if not cached or time.time() - cached.get("fetched", 0) > 7 * 86400:
                js = self._sec_get(SEC_TICKERS)
                if isinstance(js, dict):
                    cached = {"fetched": time.time(),
                              "map": {v["ticker"].upper(): int(v["cik_str"]) for v in js.values()}}
                    store.write_json(path, cached)
            self._ciks = (cached or {}).get("map", {})
        return self._ciks.get(tk.upper()) or self._ciks.get(tk.upper().replace("-", "."))

    def _submissions(self, cik, need_ts):
        """Recent filings for one company, refetched only when they might be missing something we need."""
        if cik in self._subs:
            return self._subs[cik]
        path = store.CACHE / "sec" / f"sub_{cik}.json"
        cached = store.read_json(path)
        stale = not cached or (cached["fetched"] < need_ts + 600 and time.time() - cached["fetched"] > 900)
        if stale:
            js = self._sec_get(SEC_SUBMISSIONS.format(cik))
            if isinstance(js, dict):
                r = js.get("filings", {}).get("recent", {})
                keep = ("form", "filingDate", "acceptanceDateTime", "accessionNumber", "items", "primaryDocDescription")
                cached = {"fetched": time.time(), **{k: r.get(k, []) for k in keep}}
                store.write_json(path, cached)
        self._subs[cik] = cached
        return cached

    def _accepted(self, cik, acc, fallback):
        """Exact acceptance time from the filing's header (the JSON index mislabels its time zone)."""
        path = store.CACHE / "sec" / "accepted.json"
        if self._acc is None:
            self._acc = store.read_json(path, {}) or {}
        if acc in self._acc:
            return self._acc[acc]
        raw = self._sec_get(SEC_HEADER.format(cik=cik, folder=acc.replace("-", ""), acc=acc), as_json=False)
        m = re.search(rb"<ACCEPTANCE-DATETIME>(\d{14})", raw or b"")
        if not m:
            return fallback
        t = m.group(1).decode()
        ts = clock.at(f"{t[:4]}-{t[4:6]}-{t[6:8]}", f"{t[8:10]}:{t[10:12]}") + int(t[12:14])
        self._acc[acc] = ts
        store.write_json(path, self._acc)
        return ts

    def sec_filings(self, tk, start_ts, end_ts):
        if not self.sec_enabled or tk in INDEX_ETFS:
            return []
        cik = self._cik(tk)
        sub = self._submissions(cik, end_ts) if cik else None
        if not sub:
            return []
        first = (date.fromisoformat(clock.day_of(start_ts)) - timedelta(days=1)).isoformat()
        last = clock.day_of(end_ts)
        out = []
        for i, form in enumerate(sub["form"]):
            day = sub["filingDate"][i]
            if not first <= day <= last or not _sec_relevant(form):
                continue
            # Without the header's exact time, assume the latest it could be: the end of its filing date.
            # (The index's own time can't be trusted: its time zone label is wrong on many filings.)
            ts = self._accepted(cik, sub["accessionNumber"][i], clock.at(day, "23:59"))
            if start_ts <= ts <= end_ts:
                out.append({"ts": ts, "known_ts": ts, "coarse": False, "source": "SEC EDGAR", "url": "",
                            "title": _sec_title(form, sub["items"][i], sub["primaryDocDescription"][i])})
        return out

    def view(self, now):
        return View(self, now)

    @property
    def news_failures(self):
        """News requests that failed after retries, by source, since the program started."""
        return {h: n for h, n in FAILURES.items() if h not in ("yahoo", "alpaca")}


SEC_ITEMS = {
    "1.01": "material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy", "1.05": "cybersecurity incident",
    "2.01": "acquisition or sale completed", "2.02": "earnings results", "2.03": "new debt", "2.05": "restructuring",
    "2.06": "impairment", "3.01": "delisting notice", "3.02": "unregistered share sale", "4.01": "auditor change",
    "4.02": "financials no longer reliable", "5.01": "change of control", "5.02": "director/officer change",
    "5.07": "shareholder vote", "7.01": "Reg FD disclosure", "8.01": "other events",
}


def _sec_relevant(form):
    # 424B1/B4/B5 are stock offerings; banks file hundreds of 424B2/B3 structured-note supplements a day.
    return form in ("8-K", "8-K/A", "6-K", "10-Q", "10-K", "20-F", "SC 13D", "SC 13D/A", "424B1", "424B4", "424B5")


def _sec_title(form, items, desc):
    if form.startswith("8-K"):
        what = [SEC_ITEMS.get(i.strip(), "") for i in str(items or "").split(",")]
        what = [w for w in what if w]
        return f"SEC {form}: {', '.join(what) or desc or 'current report'}"
    if form.startswith("424B"):
        return f"SEC {form}: prospectus supplement (securities offering)"
    if form.startswith("SC 13D"):
        return f"SEC {form}: activist or large-holder stake"
    return f"SEC {form}: {desc or form}"


GNEWS_MARKET = ['"stock market today" OR "stock futures" OR "stocks making the biggest moves"',
                '"S&P 500" OR Nasdaq OR "Dow Jones" stocks', 'Fed OR inflation OR "jobs report" OR "Treasury yields" stocks']
GDELT_MARKET = '("stock market" OR "stock futures" OR "S&P 500" OR "Dow Jones" OR "Wall Street") sourcelang:english'
# Market-wide GDELT results are kept only from established news outlets; per-stock results drop the
# auto-generated "fund X bought shares of Y" sites that flood every ticker.
NEWS_DOMAINS = set('''reuters.com cnbc.com marketwatch.com bloomberg.com wsj.com ft.com barrons.com investors.com
apnews.com finance.yahoo.com yahoo.com nasdaq.com investopedia.com fool.com thestreet.com forbes.com businessinsider.com
seekingalpha.com benzinga.com axios.com cnn.com nytimes.com washingtonpost.com theguardian.com bbc.com bbc.co.uk
foxbusiness.com morningstar.com kiplinger.com economist.com fortune.com latimes.com usatoday.com npr.org
investing.com zacks.com tipranks.com techcrunch.com theverge.com politico.com'''.split())
JUNK_DOMAINS = set('''themarketsdaily.com tickerreport.com etfdailynews.com marketbeat.com americanbankingnews.com
thelincolnianonline.com wkrb13.com dailypolitical.com thecerbatgem.com theenterpriseleader.com zolmax.com
modernreaders.com com-unik.info baseballnewssource.com thestockobserver.com tradingnewsdaily.com
transcriptdaily.com sportsperspectives.com weekherald.com'''.split())


def _domain(item):
    d = (item.get("source") or "").lower()
    return d[4:] if d.startswith("www.") else d


def _norm(title):
    return re.sub(r"[^a-z0-9 ]", "", title.lower())[:80]


def _dedupe(items, start_ts, end_ts, limit):
    """Items known by end_ts that may have appeared after start_ts, newest first, one per headline."""
    seen, out = set(), []
    for it in sorted(items, key=lambda x: -x["ts"]):
        known = it.get("known_ts", it["ts"])
        latest = it["ts"] + (86400 if it.get("coarse") else 0)
        if known > end_ts or latest < start_ts:
            continue
        k = _norm(it["title"])
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out[:limit]


class View:
    """Read-only window onto the data as it was at `now`."""

    def __init__(self, data, now):
        self.data, self.now = data, now

    def daily(self, tk, day):
        return self.data.daily(tk, day)

    def intraday(self, tk, day):
        step = self.data.interval_s
        return [b for b in self.data.intraday(tk, day) if b[0] + step <= self.now]

    def news_market(self, start_ts, end_ts, limit=20):
        return self.data.news_market(start_ts, min(end_ts, self.now), limit)

    def news_ticker(self, tk, start_ts, end_ts, limit=8):
        return self.data.news_ticker(tk, start_ts, min(end_ts, self.now), limit)

    def sec_filings(self, tk, start_ts, end_ts):
        return self.data.sec_filings(tk, start_ts, min(end_ts, self.now))


class LiveData(MarketData):
    """Today's bars and news are refetched with a short time-to-live instead of cached forever."""

    TTL = {"bars": 25, "news": 300}

    def __init__(self, interval="1m", cfg=None):
        super().__init__(interval, cfg)
        self._fresh = {}

    def intraday(self, tk, day):
        if day != clock.today_et():
            return super().intraday(tk, day)
        key = ("bars", tk)
        hit = self._fresh.get(key)
        if hit and time.time() - hit[0] < self.TTL["bars"]:
            return hit[1]
        fetched = time.time()
        js = _get(YAHOO.format(tk), {"interval": self.interval, "range": "1d", "includePrePost": "true"}, "yahoo")
        # Only bars that had finished when this fetch was made: a reused fetch must not pass off a
        # half-built bar as finished.
        rows = [b for b in (_parse_chart(js) or []) if clock.day_of(b[0]) == day and b[0] + self.interval_s <= fetched]
        if not rows and hit:
            rows = hit[1]
        self._fresh[key] = (time.time(), rows)
        return rows

    def _cached_news(self, key, end_ts, fetch):
        if end_ts < time.time() - 3600:
            return super()._cached_news(key, end_ts, fetch)
        hit = self._fresh.get(("news", key))
        if hit and time.time() - hit[0] < self.TTL["news"]:
            return hit[1]
        items = fetch() or (hit[1] if hit else [])
        self._fresh[("news", key)] = (time.time(), items)
        return items
