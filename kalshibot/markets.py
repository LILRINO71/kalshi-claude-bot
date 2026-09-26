"""Public market data: Kalshi (quotes, order books, history) and Coinbase (spot candles).

Neither source needs an account or API key. Historical responses are cached on disk
because settled markets and closed candles never change.
"""
import threading
import time
from datetime import datetime, timezone

import requests

from . import storage
from .config import INDEX_HISTORY_DAYS, INDICES, SERIES, is_index

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE = "https://api.exchange.coinbase.com"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"

_session = requests.Session()
_session.headers["User-Agent"] = "kalshi-claude-paper-bot/2.0"
_throttle_lock = threading.Lock()
_last_call = {"kalshi": 0.0, "coinbase": 0.0, "yahoo": 0.0}
_MIN_GAP = {"kalshi": 0.08, "coinbase": 0.15, "yahoo": 0.3}


def utc_now():
    return datetime.now(timezone.utc)


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def ts(s):
    return int(parse_iso(s).timestamp())


def fnum(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def get_json(url, params=None, host="kalshi", retries=4):
    for attempt in range(retries):
        with _throttle_lock:
            wait = _last_call[host] + _MIN_GAP[host] - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_call[host] = time.time()
        try:
            headers = {"User-Agent": "Mozilla/5.0"} if host == "yahoo" else None
            r = _session.get(url, params=params, timeout=15, headers=headers)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1 + attempt)
    return None


# ---------------------------------------------------------------- Kalshi live

def open_market(asset):
    """The market to trade right now for an asset, or None.

    Crypto has one 15-minute market per session. A stock index has a ladder of strikes per
    hour; we take the soonest-closing hour and the strike nearest the live index level.
    """
    if is_index(asset):
        return _open_index_market(asset)
    data = get_json(f"{KALSHI}/markets", {"series_ticker": SERIES[asset], "status": "open", "limit": 5})
    if not data or not data.get("markets"):
        return None
    now = utc_now()
    live = [m for m in data["markets"] if parse_iso(m["close_time"]) > now]
    return min(live, key=lambda m: m["close_time"]) if live else None


def _open_index_market(asset):
    data = get_json(f"{KALSHI}/markets", {"series_ticker": SERIES[asset], "status": "open", "limit": 1000})
    now = utc_now()
    ladder = [m for m in (data or {}).get("markets") or []
              if parse_iso(m["close_time"]) > now and fnum(m.get("floor_strike")) is not None]
    if not ladder:
        return None
    soonest = min(m["close_time"] for m in ladder)
    ladder = [m for m in ladder if m["close_time"] == soonest]
    spot = spot_candles(asset, int(time.time()), 30)
    if not spot:
        return None
    level = spot[-1][4]
    return min(ladder, key=lambda m: abs(fnum(m["floor_strike"]) - level))


def market(ticker):
    data = get_json(f"{KALSHI}/markets/{ticker}")
    return data.get("market") if data else None


def quote(m):
    return {
        "up_ask": fnum(m.get("yes_ask_dollars")),
        "up_bid": fnum(m.get("yes_bid_dollars")),
        "down_ask": fnum(m.get("no_ask_dollars")),
        "down_bid": fnum(m.get("no_bid_dollars")),
    }


def order_book(ticker, depth=30):
    """Asks per side as [(price, size)], cheapest first.

    Kalshi only publishes bids; a YES ask is the far side of a NO bid at 1 - price.
    """
    data = get_json(f"{KALSHI}/markets/{ticker}/orderbook", {"depth": depth})
    if not data:
        return None
    book = data.get("orderbook_fp") or {}
    yes_bids = [(fnum(p), fnum(s)) for p, s in book.get("yes_dollars") or []]
    no_bids = [(fnum(p), fnum(s)) for p, s in book.get("no_dollars") or []]
    return {
        "UP": sorted((round(1 - p, 4), s) for p, s in no_bids),
        "DOWN": sorted((round(1 - p, 4), s) for p, s in yes_bids),
    }


def walk_book(asks, contracts):
    """Average price to buy `contracts` against the book, or None if too thin."""
    remaining, cost = contracts, 0.0
    for price, avail in asks:
        take = min(remaining, avail)
        cost += take * price
        remaining -= take
        if remaining <= 1e-9:
            return cost / contracts
    return None


# ---------------------------------------------------------------- Kalshi history

def _slim(m):
    return {
        "ticker": m["ticker"], "event_ticker": m.get("event_ticker"),
        "open_time": m["open_time"], "close_time": m["close_time"],
        "result": m.get("result", ""), "floor_strike": fnum(m.get("floor_strike")),
        "expiration_value": fnum(m.get("expiration_value")), "volume": fnum(m.get("volume_fp")),
    }


def settled_markets(asset, progress=None):
    """Every settled 15-minute market for an asset (newest first), refreshed incrementally."""
    series = SERIES[asset]
    path = storage.CACHE / "kalshi" / f"{series}_settled.json"
    cached = storage.read_json(path, []) or []
    known = {m["ticker"] for m in cached}
    fresh, cursor, pages = [], None, 0
    # Index ladders have ~60 strikes an hour and Yahoo only has ~30 days of minute data, so stop there.
    cutoff = (utc_now().timestamp() - INDEX_HISTORY_DAYS * 86400) if is_index(asset) else None
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = get_json(f"{KALSHI}/markets", params)
        if not data:
            break
        batch = data.get("markets") or []
        pages += 1
        if progress:
            progress(f"Loading {asset} market list (page {pages})")
        stop = False
        for m in batch:
            if m["ticker"] in known or (cutoff and parse_iso(m["close_time"]).timestamp() < cutoff):
                stop = True
                break
            if m.get("result") in ("yes", "no"):
                fresh.append(_slim(m))
        cursor = data.get("cursor")
        if stop or not cursor or not batch:
            break
    merged = fresh + cached
    if cutoff:
        merged = [m for m in merged if parse_iso(m["close_time"]).timestamp() >= cutoff]
    merged.sort(key=lambda m: m["close_time"], reverse=True)
    if fresh:
        storage.write_json(path, merged)
    return merged


def index_events(asset):
    """Settled hourly index events: [{event_ticker, close_time, strikes: [market, ...]}], newest first."""
    events = {}
    for m in settled_markets(asset):
        if m.get("floor_strike") is None:
            continue
        ev = events.setdefault(m["event_ticker"], {"event_ticker": m["event_ticker"],
                                                  "close_time": m["close_time"], "strikes": []})
        ev["strikes"].append(m)
    return sorted(events.values(), key=lambda e: e["close_time"], reverse=True)


def market_candles(asset, ticker, open_time, close_time):
    """Per-minute YES bid/ask candles for a settled market (cached forever)."""
    path = storage.CACHE / "kalshi" / "candles" / f"{ticker}.json"
    cached = storage.read_json(path)
    if cached is not None:
        return cached
    data = get_json(f"{KALSHI}/series/{SERIES[asset]}/markets/{ticker}/candlesticks",
                    {"start_ts": ts(open_time), "end_ts": ts(close_time), "period_interval": 1})
    if data is None:
        return None
    rows = []
    for c in data.get("candlesticks") or []:
        ya, yb = c.get("yes_ask") or {}, c.get("yes_bid") or {}
        rows.append({
            "end": c["end_period_ts"],
            "ask_open": fnum(ya.get("open_dollars")), "ask_close": fnum(ya.get("close_dollars")),
            "bid_open": fnum(yb.get("open_dollars")), "bid_close": fnum(yb.get("close_dollars")),
            "volume": fnum(c.get("volume_fp"), 0.0),
        })
    rows.sort(key=lambda r: r["end"])
    storage.write_json(path, rows)
    return rows


def historical_quote(candles, at_ts, max_age=None):
    """The YES bid/ask as of `at_ts`, using only minutes that had finished by then.

    max_age (seconds) rejects a quote whose last update is older than that.
    """
    done = [c for c in candles if c["end"] <= at_ts and c["ask_close"] is not None]
    if done:
        c = done[-1]
        if max_age is not None and at_ts - c["end"] > max_age:
            return None
        ask, bid = c["ask_close"], c["bid_close"]
    else:
        # Only the minute in progress at `at_ts` may lend its opening quote; never a later one.
        first = next((c for c in candles if c["ask_open"] is not None and c["end"] - 60 <= at_ts), None)
        if not first:
            return None
        ask, bid = first["ask_open"], first["bid_open"]
    if ask is None or bid is None:
        return None
    return {"up_ask": ask, "up_bid": bid, "down_ask": round(1 - bid, 4), "down_bid": round(1 - ask, 4)}


# ---------------------------------------------------------------- Coinbase

_BLOCK = 300 * 60  # Coinbase returns at most 300 candles per request


def _coinbase_block(asset, block):
    start = block * _BLOCK
    end = start + _BLOCK - 60
    path = storage.CACHE / "coinbase" / asset / f"{block}.json"
    complete = end + 120 < time.time()
    if complete:
        cached = storage.read_json(path)
        if cached is not None:
            return cached
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = get_json(f"{COINBASE}/products/{asset}-USD/candles",
                    {"granularity": 60, "start": iso(start), "end": iso(end)}, host="coinbase")
    if not isinstance(data, list):
        return []
    rows = sorted(([int(c[0]), c[1], c[2], c[3], c[4], c[5]] for c in data), key=lambda c: c[0])
    if complete:
        storage.write_json(path, rows)
    return rows


def _yahoo_day(asset, day):
    """One UTC day of 1-minute index candles from Yahoo, cached once the day is over."""
    start = day * 86400
    path = storage.CACHE / "yahoo" / asset / f"{day}.json"
    complete = start + 86400 + 3600 < time.time()
    if complete:
        cached = storage.read_json(path)
        if cached is not None:
            return cached
    data = get_json(f"{YAHOO}/{INDICES[asset]['yahoo']}",
                    {"interval": "1m", "period1": start, "period2": start + 86400}, host="yahoo")
    try:
        res = data["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        rows = [[int(t), lo, hi, op, cl, vol or 0] for t, lo, hi, op, cl, vol in
                zip(res.get("timestamp") or [], q["low"], q["high"], q["open"], q["close"], q["volume"])
                if cl is not None]
    except (TypeError, KeyError, IndexError):
        return []
    if complete:
        storage.write_json(path, rows)
    return rows


def spot_candles(asset, end_ts, minutes=90):
    """1-minute candles [time, low, high, open, close, volume] that closed at or before end_ts."""
    start_ts = end_ts - minutes * 60
    rows = []
    if is_index(asset):
        for day in range(start_ts // 86400, end_ts // 86400 + 1):
            rows.extend(_yahoo_day(asset, day))
    else:
        for block in range(start_ts // _BLOCK, end_ts // _BLOCK + 1):
            rows.extend(_coinbase_block(asset, block))
    seen, out = set(), []
    for c in rows:
        if start_ts <= c[0] and c[0] + 60 <= end_ts and c[0] not in seen:
            seen.add(c[0])
            out.append(c)
    return sorted(out, key=lambda c: c[0])
