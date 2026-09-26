import json
import math

import pytest

from kalshibot import backtest, config, markets, metrics, snapshot, storage, strategies
from kalshibot.portfolio import Account, kalshi_fee


@pytest.fixture(autouse=True)
def isolated_data(monkeypatch, tmp_path):
    for name, sub in [("DATA", ""), ("CACHE", "cache"), ("BACKTESTS", "backtests"), ("LIVE", "live")]:
        monkeypatch.setattr(storage, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(storage, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(storage, "USAGE_FILE", tmp_path / "usage.jsonl")
    storage.ensure_dirs()


def cfg(**kw):
    return config.validate({**config.DEFAULTS, **kw})


# ---------------------------------------------------------------- fees and sizing

def test_fee_matches_kalshi_schedule():
    assert kalshi_fee(0.50, 1) == 0.02
    assert kalshi_fee(0.50, 100) == 1.75
    assert kalshi_fee(0.90, 100) == 0.63
    assert kalshi_fee(0.50, 0) == 0


def test_contracts_fit_inside_stake_including_fees():
    a = Account(1000)
    n, fees = a.contracts_for(20.0, 0.5, True)
    assert n * 0.5 + fees <= 20.0 and (n + 1) * 0.5 + kalshi_fee(0.5, n + 1) > 20.0


def test_percent_sizing_and_cap():
    a = Account(1000)
    assert a.stake_for(cfg(sizing="percent", percent_stake=2), 0.5, None)[0] == pytest.approx(20)
    assert a.stake_for(cfg(sizing="percent", percent_stake=50, max_stake_pct=5), 0.5, None)[0] == pytest.approx(50)


def test_kelly_needs_edge():
    a = Account(1000)
    stake, why = a.stake_for(cfg(sizing="kelly", kelly_fraction=0.5), 0.6, 0.55)
    assert stake == 0 and "does not beat" in why
    stake, _ = a.stake_for(cfg(sizing="kelly", kelly_fraction=0.5, max_stake_pct=100), 0.5, 0.6)
    assert stake == pytest.approx(1000 * 0.5 * 0.2)


def test_open_and_settle_moves_cash():
    a = Account(1000)
    a.open("T", "BTC", "DOWN", 0.4, 100, 1.68, 100, 50)
    assert a.cash == pytest.approx(1000 - 40 - 1.68)
    assert a.equity() == pytest.approx(1000)
    t = a.settle("T", "no", 100)
    assert t["won"] and t["pnl"] == pytest.approx(58.32)
    assert a.cash == pytest.approx(1058.32)


def test_daily_loss_limit_blocks():
    a = Account(1000)
    a.open("T", "BTC", "UP", 0.5, 300, 0, 100, 50)
    a.settle("T", "no", 100)
    assert "loss limit" in a.daily_block(100, cfg(max_daily_loss=-100))


# ---------------------------------------------------------------- market data

def test_order_book_converts_bids_to_asks(monkeypatch):
    book = {"orderbook_fp": {"yes_dollars": [["0.40", "10"], ["0.45", "5"]],
                             "no_dollars": [["0.50", "20"], ["0.53", "7"]]}}
    monkeypatch.setattr(markets, "get_json", lambda *a, **k: book)
    asks = markets.order_book("T")
    assert asks["UP"] == [(0.47, 7.0), (0.5, 20.0)]
    assert asks["DOWN"] == [(0.55, 5.0), (0.6, 10.0)]
    assert markets.walk_book(asks["UP"], 10) == pytest.approx((7 * 0.47 + 3 * 0.5) / 10)
    assert markets.walk_book(asks["UP"], 100) is None


def test_historical_quote_never_peeks_ahead():
    candles = [{"end": 60, "ask_open": 0.5, "ask_close": 0.52, "bid_open": 0.48, "bid_close": 0.5},
               {"end": 120, "ask_open": 0.52, "ask_close": 0.9, "bid_open": 0.5, "bid_close": 0.88}]
    assert markets.historical_quote(candles, 30)["up_ask"] == 0.5     # before any minute closed
    assert markets.historical_quote(candles, 119)["up_ask"] == 0.52   # minute 2 not finished yet
    q = markets.historical_quote(candles, 120)
    assert q["up_ask"] == 0.9 and q["down_ask"] == pytest.approx(0.12)


def make_snap(price=100.0, strike=100.0, up_ask=0.5, down_ask=0.52, mins_left=10):
    closes = [100 + 0.05 * math.sin(i) for i in range(89)] + [price]
    candles = [[i * 60, c, c, c, c, 1] for i, c in enumerate(closes)]
    q = {"up_ask": up_ask, "up_bid": up_ask - 0.01, "down_ask": down_ask, "down_bid": down_ask - 0.01}
    return snapshot.build("BTC", "KXBTC15M-TEST", 6000 + mins_left * 60, 6000, strike, q, candles)


def test_prompt_hides_dates_and_tickers():
    p = snapshot.prompt(make_snap(), "be careful")
    assert "KXBTC15M-TEST" not in p and "2026" not in p and "be careful" in p


def test_random_walk_trades_only_with_edge():
    fair = strategies.random_walk(make_snap(price=100.3, up_ask=0.5), cfg(min_edge=0.03))
    assert fair["direction"] == "UP"
    flat = strategies.random_walk(make_snap(price=100.0, up_ask=0.5, down_ask=0.5), cfg(min_edge=0.03))
    assert flat["direction"] == "SKIP"


# ---------------------------------------------------------------- metrics

def test_metrics_on_known_numbers():
    assert metrics.brier([(1.0, True), (0.0, False)]) == 0
    assert metrics.brier([(0.5, True)]) == 0.25
    lo, hi = metrics.wilson(50, 100)
    assert lo < 0.5 < hi
    assert metrics.max_drawdown([[1, 1100], [2, 900], [3, 1200]], 1000) == (200, pytest.approx(200 / 1100))


# ---------------------------------------------------------------- backtest simulation

def test_backtest_simulation_end_to_end(monkeypatch):
    run = backtest.Run(cfg(strategy="favorite", sizing="fixed", fixed_stake=10), {"start": "", "end": ""})
    items = []
    for i, (res, ask) in enumerate([("yes", 0.6), ("no", 0.7), ("yes", 0.55)]):
        s = make_snap(up_ask=ask, down_ask=round(1.01 - ask, 2))
        s.ticker, s.decision_ts, s.close_ts = f"T{i}", 1000 + i * 900, 1600 + i * 900
        candles = [{"end": s.decision_ts, "ask_open": ask, "ask_close": ask, "bid_open": ask - .01, "bid_close": ask - .01}]
        items.append({"market": {"ticker": s.ticker, "result": res}, "snap": s, "candles": candles,
                      "decision": strategies.decide(s, run.cfg)})
    log, acct, summary = run.simulate(items)
    assert summary["trades"] == 3 and summary["wins"] == 2
    assert not acct.positions
    assert acct.cash == pytest.approx(1000 + summary["net_pnl"], abs=0.01)
