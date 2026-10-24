"""Offline tests with fake Kalshi data. Run: python -m pytest -q"""
import base64
import os
import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from scanner import db, gates
from scanner.config import Config, classify_asset
from scanner.engine import Engine
from scanner.model import edges, fair_yes, prob_above, taker_fee


def iso(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


class FakePublic:
    def __init__(self):
        self.markets = {}
        self.results = {}

    def discover(self):
        return {"KXBTC15M": {"asset": "BTC", "title": "Bitcoin 15 min", "fee_multiplier": 1.0},
                "KXWTI15M": {"asset": "WTI", "title": "WTI Oil 15 min", "fee_multiplier": 1.0}}

    def open_markets(self, st, max_minutes):
        return [m for m in self.markets.values() if m["ticker"].startswith(st)]

    def markets_by_tickers(self, tickers):
        out = []
        for t in tickers:
            m = dict(self.markets.get(t, {"ticker": t}))
            if t in self.results:
                m["result"], m["status"] = self.results[t], "finalized"
            out.append(m)
        return out


class FakeTrader:
    def __init__(self):
        self.orders = []

    def buy(self, ticker, side, price, count):
        self.orders.append((ticker, side, price, count))
        return {"order_id": "o1", "filled": count, "fill_price": price, "fee": 0.02 * count}

    def balance(self):
        return 100.0


def mk(ticker, strike, yes_ask, yes_bid, minutes=8, size=50):
    return {"ticker": ticker, "strike_type": "greater_or_equal", "floor_strike": strike, "cap_strike": None,
            "close_time": iso(minutes), "yes_ask_dollars": f"{yes_ask:.4f}", "yes_bid_dollars": f"{yes_bid:.4f}",
            "yes_ask_size_fp": f"{size}.00", "yes_bid_size_fp": f"{size}.00", "volume_fp": "1000.00"}


def price_fn(feed, symbol, lookback):
    if symbol == "BTC-USD":
        return {"price": 100_000.0, "age_sec": 0, "sigma_1m": 0.0008}
    return {"price": 90.0, "age_sec": 900, "sigma_1m": 0.001}   # delayed commodity feed


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.db_path = str(tmp_path / "t.db")
    c.alert_methods = ["console"]
    c.trading_mode = "paper"
    c.gate_min_trades = 20
    c.gate_min_days = 0
    c.gate_recent_n = 10
    return c


def test_classify():
    assert classify_asset("KXBTC15M Bitcoin price up or down in 15 min") == "BTC"
    assert classify_asset("KXETH15M Ethereum 15 min") is None
    assert classify_asset("KXWTI15M WTI Oil 15 min") == "WTI"
    assert classify_asset("Brent crude close") is None
    assert classify_asset("Gold daily close") == "GOLD"
    assert classify_asset("Natural gas weekly") == "NATGAS"
    assert classify_asset("US gas prices this week") is None


def test_model_sanity():
    assert abs(prob_above(100, 100, 0.001, 10) - 0.5) < 0.01
    assert prob_above(101, 100, 0.0005, 5) > 0.9
    assert taker_fee(0.5) == 0.02 and taker_fee(0.5, 10) == 0.18
    m = {"strike_type": "between", "floor_strike": 99, "cap_strike": 101}
    assert 0 < fair_yes(m, 100, 0.001, 10) < 1
    e = edges(0.70, {"yes_ask": 0.55, "no_ask": 0.47})
    assert e["yes"] > 0 > e["no"]


def test_signal_paper_and_stale_feed(cfg):
    pub = FakePublic()
    # BTC well above strike, YES offered cheap -> signal. WTI feed 15 min old -> skipped.
    pub.markets["KXBTC15M-A"] = mk("KXBTC15M-A", 99_800, 0.55, 0.50)
    pub.markets["KXWTI15M-A"] = mk("KXWTI15M-A", 89.5, 0.30, 0.25)
    e = Engine(cfg, public=pub, price_fn=price_fn)
    rows = {r["ticker"]: r for r in e.cycle()}
    assert rows["KXBTC15M-A"]["signal"] and rows["KXBTC15M-A"]["side"] == "yes"
    assert not rows["KXWTI15M-A"]["signal"] and "old" in rows["KXWTI15M-A"]["note"]
    e.cycle()  # second pass must not duplicate the paper trade
    assert len(db.recent(e.c)) == 1
    pub.results["KXBTC15M-A"] = "yes"
    e.cycle()
    t = db.recent(e.c)[0]
    assert t["status"] == "won" and t["pnl"] > 0


def _seed(c, n, win_rate, price=0.5, asset="BTC"):
    for i in range(n):
        db.record_trade(c, ts=time.time() - 86400 * 10 + i, mode="paper", asset=asset, series="KXBTC15M",
                        ticker=f"T{asset}{i}", side="yes", price=price, contracts=1, fee=0.02)
    wins = round(n * win_rate)
    for i, t in enumerate(db.open_trades(c)):
        # spread wins evenly so drawdown/recent checks reflect the rate
        db.settle(c, t["id"], "yes" if (i * win_rate) % 1 + win_rate >= 1 or i < 0 else "no")


def test_gates_block_and_unlock(cfg):
    c = db.connect(cfg.db_path)
    _seed(c, 30, 0.45)
    ok, checks, _ = gates.evaluate(db.settled(c, "paper", "BTC"), cfg)
    assert not ok
    c2 = db.connect(cfg.db_path.replace("t.db", "t2.db"))
    _seed(c2, 30, 0.80)
    ok2, _, s = gates.evaluate(db.settled(c2, "paper", "BTC"), cfg)
    assert ok2, s


def test_live_only_after_gates_and_respects_kill(cfg):
    cfg.trading_mode = "live"
    pub, tr = FakePublic(), FakeTrader()
    e = Engine(cfg, public=pub, trader=tr, price_fn=price_fn)
    pub.markets["KXBTC15M-B"] = mk("KXBTC15M-B", 99_800, 0.55, 0.50)
    e.cycle()
    assert tr.orders == []                     # gates not passed -> no live order
    _seed(e.c, 30, 0.85)
    del pub.markets["KXBTC15M-B"]
    pub.markets["KXBTC15M-C"] = mk("KXBTC15M-C", 99_800, 0.55, 0.50)
    e.cycle()
    assert len(tr.orders) == 1                 # unlocked
    t, side, px, n = tr.orders[0]
    assert n <= cfg.max_contracts_per_trade and n * px <= cfg.max_dollars_per_trade
    db.put(e.c, "kill", True)
    pub.markets["KXBTC15M-D"] = mk("KXBTC15M-D", 99_800, 0.55, 0.50)
    e.cycle()
    assert len(tr.orders) == 1                 # kill switch blocks


def test_daily_loss_limit_trips_kill(cfg):
    cfg.trading_mode = "live"
    cfg.max_daily_loss = 5
    pub, tr = FakePublic(), FakeTrader()
    e = Engine(cfg, public=pub, trader=tr, price_fn=price_fn)
    _seed(e.c, 30, 0.85)
    for i in range(3):
        db.record_trade(e.c, ts=time.time(), mode="live", asset="BTC", ticker=f"L{i}", side="yes",
                        price=0.6, contracts=5, fee=0.1)
    for t in db.open_trades(e.c):
        if t["mode"] == "live":
            db.settle(e.c, t["id"], "no")
    pub.markets["KXBTC15M-E"] = mk("KXBTC15M-E", 99_800, 0.55, 0.50)
    e.cycle()
    assert tr.orders == [] and db.get(e.c, "kill") is True


def test_signing(tmp_path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from scanner.kalshi import KalshiTrader
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "k.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()))
    c = Config(); c.api_key_id = "abc"; c.private_key_path = str(p)
    h = KalshiTrader(c)._sign("GET", "https://external-api.kalshi.com/trade-api/v2/portfolio/balance?x=1")
    msg = f"{h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/portfolio/balance".encode()
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg,
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                            hashes.SHA256())


def test_dashboard_api(cfg, monkeypatch):
    monkeypatch.setenv("DB_PATH", cfg.db_path)
    e = Engine(cfg, public=FakePublic(), price_fn=price_fn)
    e.pub.markets["KXBTC15M-A"] = mk("KXBTC15M-A", 99_800, 0.55, 0.50)
    e.cycle()
    import importlib
    import dashboard.app as dapp
    importlib.reload(dapp)
    dapp.conn = db.connect(cfg.db_path)
    client = dapp.app.test_client()
    s = client.get("/api/state").get_json()
    assert s["paper"]["signals_today"] == 1 and "BTC" in s["gates"]
    assert client.post("/api/kill").status_code == 200
    assert client.get("/api/state").get_json()["killed"] is True
    assert b"ticker" in client.get("/export.csv").data
    assert b"Kalshi Scanner" in client.get("/").data
