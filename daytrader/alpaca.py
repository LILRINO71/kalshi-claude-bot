"""Optional deeper intraday history from Alpaca's free market-data API.

Yahoo only serves 5-minute bars for about the last 60 days. A free Alpaca account (alpaca.markets: sign up,
open the paper-trading dashboard, generate an API key; no funding, no real account) gives historical bars for
all US stocks back to 2016, so a backtest can start earlier, for example right after the model's training
cutoff. Keys come from the environment (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY, or APCA_API_KEY_ID /
APCA_API_SECRET_KEY) or from data/daytrader/alpaca.json, which `python -m daytrader alpaca KEY SECRET` writes.

Bars are normalized to look like Yahoo's so the scanner and features behave the same on both sources:
split-adjusted prices, timestamps at the bar's start, and pre/post-market volume reported as zero (Yahoo
reports none). The same key reads Alpaca's news service (Benzinga headlines with exact publication times,
past and present). Only market data and news are read; this module can't place orders.
"""
import html
import os
import re
import time
from datetime import datetime, timezone

import requests

from . import clock, store

URL = "https://data.alpaca.markets/v2/stocks/bars"
NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
TIMEFRAMES = {"1m": "1Min", "2m": "2Min", "5m": "5Min", "15m": "15Min"}
BATCH = 40          # symbols per request
GAP_S = 0.35        # the free plan allows 200 requests a minute

_session = requests.Session()


class AlpacaError(RuntimeError):
    pass


def key_file():
    return store.DATA / "alpaca.json"


def keys():
    kid = os.environ.get("ALPACA_API_KEY_ID") or os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY")
    if kid and secret:
        return kid, secret
    saved = store.read_json(key_file()) or {}
    if saved.get("key_id") and saved.get("secret_key"):
        return saved["key_id"], saved["secret_key"]
    return None


def configured():
    return keys() is not None


def save_keys(key_id, secret_key):
    store.write_json(key_file(), {"key_id": key_id.strip(), "secret_key": secret_key.strip()})


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def symbol(ticker):
    """Yahoo writes share classes with a dash (BRK-B); Alpaca uses a dot (BRK.B)."""
    return ticker.replace("-", ".")


def normalize(bars, day_of=clock.day_of):
    """Alpaca bars -> {day: [(t, o, h, l, c, v)]}, with volume zeroed outside 09:30-16:00 like Yahoo's."""
    out = {}
    for b in bars:
        t = int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp())
        day = day_of(t)
        regular = clock.at(day, "09:30") <= t < clock.at(day, "16:00")
        o, h, l, c = (round(float(b[k]), 4) for k in ("o", "h", "l", "c"))
        out.setdefault(day, []).append((t, o, max(h, o, c), min(l, o, c), c, int(b.get("v") or 0) if regular else 0))
    return {d: sorted(rows) for d, rows in out.items()}


def fetch(tickers, first_day, last_day, interval="5m", log=print):
    """{ticker: {day: bars}} from 04:00 on first_day to 20:00 on last_day (New York time). Raises AlpacaError."""
    k = keys()
    if not k:
        raise AlpacaError("no Alpaca API key: set ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY or run "
                          "`python -m daytrader alpaca KEY SECRET`")
    headers = {"APCA-API-KEY-ID": k[0], "APCA-API-SECRET-KEY": k[1]}
    start, end = _iso(clock.at(first_day, "04:00")), _iso(clock.at(last_day, "20:00"))
    tickers = list(dict.fromkeys(tickers))
    ours = {symbol(tk): tk for tk in tickers}
    raw = {tk: [] for tk in tickers}
    feed = "sip"
    for i in range(0, len(tickers), BATCH):
        chunk = [symbol(tk) for tk in tickers[i:i + BATCH]]
        token, tries = None, 0
        while chunk:
            params = {"symbols": ",".join(chunk), "timeframe": TIMEFRAMES[interval], "start": start, "end": end,
                      "limit": 10000, "adjustment": "split", "feed": feed, "sort": "asc"}
            if token:
                params["page_token"] = token
            time.sleep(GAP_S)
            try:
                r = _session.get(URL, params=params, headers=headers, timeout=30)
            except requests.RequestException as e:
                tries += 1
                if tries > 4:
                    raise AlpacaError(f"network error: {e}")
                time.sleep(3 * tries)
                continue
            if r.status_code in (401, 403) and "subscription" in r.text.lower() and feed == "sip":
                log("Alpaca: this key can't read consolidated (SIP) history; falling back to the IEX feed, which "
                    "sees only IEX's share of trades, so bars are thinner and less exact")
                feed = "iex"
                continue
            if r.status_code in (401, 403):
                raise AlpacaError(f"Alpaca refused the key ({r.status_code}): {r.text[:200]}")
            if r.status_code == 429 or r.status_code >= 500:
                tries += 1
                if tries > 6:
                    raise AlpacaError(f"Alpaca kept failing ({r.status_code})")
                time.sleep(5 * tries)
                continue
            bad = re.search(r"invalid symbol: ([A-Za-z0-9.\-]+)", r.text) if r.status_code == 400 else None
            if bad and bad.group(1) in chunk and not token:
                log(f"Alpaca doesn't know {ours.get(bad.group(1), bad.group(1))}; skipping it")
                chunk = [s for s in chunk if s != bad.group(1)]
                continue
            if r.status_code != 200:
                raise AlpacaError(f"Alpaca error {r.status_code}: {r.text[:200]}")
            js = r.json()
            for sym, bars in (js.get("bars") or {}).items():
                raw.setdefault(ours.get(sym, sym), []).extend(bars)
            token = js.get("next_page_token")
            if not token:
                break
    return {tk: normalize(bars) for tk, bars in raw.items()}


def _parse_ts(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def news(tickers, start_ts, end_ts, pages=2):
    """Headlines tagged with any of `tickers` between two times, newest first, at most 50 per page:
    [{ts, known_ts, title, source, url, symbols}], or None when there is no key or a request failed (so a
    caller never caches a partial answer). `ts` is first publication; a story edited later counts as known
    only from its last update, because the edited headline is the one that comes back."""
    k = keys()
    if not k or not tickers:
        return None
    headers = {"APCA-API-KEY-ID": k[0], "APCA-API-SECRET-KEY": k[1]}
    ours = {symbol(tk): tk for tk in tickers}
    params = {"symbols": ",".join(ours), "start": _iso(start_ts), "end": _iso(end_ts), "limit": 50, "sort": "desc"}
    out, token = [], None
    for _ in range(pages):
        for tries in range(3):
            time.sleep(GAP_S * (1 + 4 * tries))
            try:
                r = _session.get(NEWS_URL, params={**params, **({"page_token": token} if token else {})},
                                 headers=headers, timeout=30)
            except requests.RequestException:
                continue
            if r.status_code == 429 or r.status_code >= 500:
                continue
            break
        else:
            return None
        if r.status_code != 200:
            return None
        js = r.json()
        for n in js.get("news") or []:
            try:
                ts = _parse_ts(n["created_at"])
                known = max(ts, _parse_ts(n.get("updated_at") or n["created_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            title = html.unescape(n.get("headline") or "").strip()
            if title:
                out.append({"ts": ts, "known_ts": known, "coarse": False, "title": title,
                            "source": (n.get("source") or "benzinga").capitalize(), "url": n.get("url") or "",
                            "symbols": [ours.get(s, s.replace(".", "-")) for s in n.get("symbols") or []]})
        token = js.get("next_page_token")
        if not token:
            break
    return out
