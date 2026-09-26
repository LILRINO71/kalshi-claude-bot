import json
from datetime import datetime, timedelta, timezone

import bot


def test_fee_matches_kalshi_schedule():
    assert bot.kalshi_fee(0.50, 1) == 0.02
    assert bot.kalshi_fee(0.50, 100) == 1.75
    assert bot.kalshi_fee(0.90, 100) == 0.63


def test_order_book_converts_bids_to_asks(monkeypatch):
    book = {"orderbook_fp": {"yes_dollars": [["0.40", "10"], ["0.45", "5"]],
                             "no_dollars": [["0.50", "20"], ["0.53", "7"]]}}
    monkeypatch.setattr(bot, "get_json", lambda *a, **k: book)
    asks = bot.order_book("T")
    assert asks["UP"] == [(0.47, 7.0), (0.5, 20.0)]
    assert asks["DOWN"] == [(0.55, 5.0), (0.6, 10.0)]


def test_fill_walks_book_and_rejects_thin_book(monkeypatch):
    monkeypatch.setattr(bot, "order_book", lambda t: {"UP": [(0.47, 7.0), (0.5, 20.0)], "DOWN": []})
    cfg = dict(bot.DEFAULT_CONFIG)
    assert abs(bot.simulate_fill("T", "UP", 10, 0.47, cfg) - (7 * 0.47 + 3 * 0.5) / 10) < 1e-9
    assert bot.simulate_fill("T", "UP", 100, 0.47, cfg) is None


def test_settlement_pnl(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "DATA", tmp_path)
    monkeypatch.setattr(bot, "TRADES_FILE", tmp_path / "trades.jsonl")
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    state = {"positions": {"T": {"asset": "BTC", "ticker": "T", "side": "DOWN", "entry": 0.4,
                                 "size": 100, "fees": 1.68, "close_time": past}}, "decided": {}}
    monkeypatch.setattr(bot, "market_by_ticker", lambda t: {"result": "no"})
    bot.settle_positions(state)
    row = json.loads((tmp_path / "trades.jsonl").read_text().splitlines()[0])
    assert row["won"] and abs(row["pnl"] - 58.32) < 1e-6
    assert state["positions"] == {}
