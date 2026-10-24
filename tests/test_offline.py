"""Offline tests with fake Kalshi data. Run: python -m pytest -q"""
import base64
import time
from datetime import datetime, timedelta, timezone

import pytest

from scanner import db, gates
from scanner.config import Config, classify_asset
from scanner.engine import Engine
from scanner.model import edges, fair_yes, momentum, prob_above, taker_fee, up_side


def iso(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


class FakePublic:
    def __init__(self):
        self.markets = {}
        self.results = {}

    def discover(self):
        return {"KXBTC15M": {"asset": "BTC", "title": "Bitcoin 15 min", "fee_multiplier": 1.0},
                "KXGOLD15M": {"asset": "GOLD", "title": "Gold 15 min", "fee_multiplier": 1.0}}

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


CLOSE = iso(6)


def mk(ticker, strike, yes_ask, yes_bid, minutes=6, size=50, close=None):
    return {"ticker": ticker, "strike_type": "greater_or_equal", "floor_strike": strike, "cap_strike": None,
            "close_time": close or iso(minutes), "yes_ask_dollars": f"{yes_ask:.4f}",
            "yes_bid_dollars": f"{yes_bid:.4f}", "yes_ask_size_fp": f"{size}.00",
            "yes_bid_size_fp": f"{size}.00", "volume_fp": "1000.00"}


def feed(trend_per_min=40.0, end=99_980.0, price=100_000.0):
    closes = [end - trend_per_min * (59 - i) for i in range(60)]
    return {"price": price, "age_sec": 0, "sigma_1m": 0.0008, "closes": closes}


def make_price_fn(btc=None):
    def fn(feed_name, symbol, lookback):
        if symbol == "BTC-USD":
            return btc or feed()
        g = feed(0.5, 2400, 2401)
        g["age_sec"] = 900      # free gold feed 15 min late
        return g
    return fn


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


def run(cfg, markets, btc=None, trader=None):
    pub = FakePublic()
    pub.markets.update({m["ticker"]: m for m in markets})
    e = Engine(cfg, public=pub, trader=trader, price_fn=make_price_fn(btc))
    rows = {r["ticker"]: r for r in e.cycle()}
    return e, pub, rows


# ---------------- model ----------------
def test_classify():
    assert classify_asset("KXBTC15M Bitcoin price up or down in 15 min") == "BTC"
    assert classify_asset("KXGOLD15M Gold 15 min") == "GOLD"
    assert classify_asset("KXWTI15M WTI Oil 15 min") == "WTI"
    assert classify_asset("KXETH15M Ethereum 15 min") is None
    assert classify_asset("Brent crude close") is None


def test_model_sanity():
    assert abs(prob_above(100, 100, 0.001, 10) - 0.5) < 0.01
    assert prob_above(101, 100, 0.0005, 5) > 0.9
    assert prob_above(100, 100, 0.001, 5, drift_1m=0.0005) > 0.55   # momentum pushes odds up
    assert taker_fee(0.5) == 0.02 and taker_fee(0.5, 10) == 0.18
    m = feed()
    mo = momentum(m["price"], m["closes"], m["sigma_1m"], 5)
    assert mo["dir"] == "UP" and mo["z"] > 0.5
    dn = feed(-40, 100_020, 100_000)
    assert momentum(dn["price"], dn["closes"], dn["sigma_1m"], 5)["dir"] == "DOWN"
    assert up_side({"strike_type": "greater_or_equal"}) == "yes"
    assert up_side({"strike_type": "less"}) == "no"
    e = edges(0.70, {"yes_ask": 0.55, "no_ask": 0.47})
    assert e["yes"] > 0 > e["no"]


# ---------------- 7:30 / 2:00 window ----------------
def test_waits_until_7_30(cfg):
    e, _, rows = run(cfg, [mk("KXBTC15M-A", 99_900, 0.55, 0.50, minutes=10)])
    r = rows["KXBTC15M-A"]
    assert r["status"] == "WAITING" and not r["signal"] and r["direction"] == "UP"
    assert db.recent(e.c) == []


def test_trades_up_inside_window(cfg):
    e, _, rows = run(cfg, [mk("KXBTC15M-A", 99_900, 0.55, 0.50, minutes=6)])
    r = rows["KXBTC15M-A"]
    assert r["status"] == "TRADE" and r["side"] == "yes" and r["direction"] == "UP"
    t = db.recent(e.c)[0]
    assert t["direction"] == "UP" and t["fair_base"] is not None and t["close_ts"]


def test_closed_under_2_00(cfg):
    e, _, rows = run(cfg, [mk("KXBTC15M-A", 99_900, 0.55, 0.50, minutes=1.5)])
    assert rows["KXBTC15M-A"]["status"] == "CLOSED"
    assert db.recent(e.c) == []


def test_down_momentum_buys_no(cfg):
    down = feed(-40, 100_020, 100_000)
    e, _, rows = run(cfg, [mk("KXBTC15M-A", 100_100, 0.40, 0.45, minutes=6)], btc=down)
    r = rows["KXBTC15M-A"]
    assert r["direction"] == "DOWN" and r["side"] == "no" and r["status"] == "TRADE"


def test_weak_momentum_waits(cfg):
    flat = feed(0.5, 99_999.5, 100_000)
    e, _, rows = run(cfg, [mk("KXBTC15M-A", 99_900, 0.55, 0.50, minutes=6)], btc=flat)
    assert rows["KXBTC15M-A"]["status"] == "SETUP FORMING"
    assert "momentum weak" in rows["KXBTC15M-A"]["note"]


def test_stale_gold_feed_blocked(cfg):
    e, _, rows = run(cfg, [mk("KXGOLD15M-A", 2399, 0.40, 0.35, minutes=6)])
    assert rows["KXGOLD15M-A"]["status"] == "BLOCKED" and "delayed" in rows["KXGOLD15M-A"]["note"]


def test_one_trade_per_window(cfg):
    a = mk("KXBTC15M-A", 99_900, 0.55, 0.50, close=CLOSE)
    b = mk("KXBTC15M-B", 99_950, 0.45, 0.40, close=CLOSE)
    e, pub, _ = run(cfg, [a, b])
    e.cycle()
    assert len(db.recent(e.c)) == 1


def test_settles_and_scores(cfg):
    e, pub, _ = run(cfg, [mk("KXBTC15M-A", 99_900, 0.55, 0.50)])
    pub.results["KXBTC15M-A"] = "yes"
    e.cycle()
    t = db.recent(e.c)[0]
    assert t["status"] == "won" and t["pnl"] > 0


# ---------------- gates & live ----------------
def _seed(c, n, win_rate, price=0.5, asset="BTC", mom_better=True):
    for i in range(n):
        db.record_trade(c, ts=time.time() - 86400 * 10 + i, mode="paper", asset=asset, series="KXBTC15M",
                        ticker=f"T{asset}{i}", side="yes", price=price, contracts=1, fee=0.02,
                        fair=0.65 if mom_better else 0.55, fair_base=0.55 if mom_better else 0.65,
                        close_ts=1000 + i, direction="UP")
    for i, t in enumerate(db.open_trades(c)):
        db.settle(c, t["id"], "yes" if (i * win_rate) % 1 + win_rate >= 1 else "no")


def test_gates_block_and_unlock(cfg, tmp_path):
    c = db.connect(str(tmp_path / "a.db"))
    _seed(c, 30, 0.45)
    assert not gates.evaluate(db.settled(c, "paper", "BTC"), cfg)[0]
    c2 = db.connect(str(tmp_path / "b.db"))
    _seed(c2, 30, 0.80)
    ok, _, s = gates.evaluate(db.settled(c2, "paper", "BTC"), cfg)
    assert ok, s
    c3 = db.connect(str(tmp_path / "c.db"))
    _seed(c3, 30, 0.80, mom_better=False)      # profitable but momentum adds nothing -> blocked
    ok3, checks, _ = gates.evaluate(db.settled(c3, "paper", "BTC"), cfg)
    assert not ok3 and not next(x for x in checks if x["name"].startswith("Momentum"))["ok"]


def test_live_only_after_gates_and_respects_kill(cfg):
    cfg.trading_mode = "live"
    tr = FakeTrader()
    e, pub, _ = run(cfg, [mk("KXBTC15M-B", 99_900, 0.55, 0.50, minutes=6)], trader=tr)
    assert tr.orders == []                     # gates not passed
    _seed(e.c, 30, 0.85)
    del pub.markets["KXBTC15M-B"]
    pub.markets["KXBTC15M-C"] = mk("KXBTC15M-C", 99_900, 0.55, 0.50, minutes=5)
    e.cycle()
    assert len(tr.orders) == 1
    _, side, px, n = tr.orders[0]
    assert side == "yes" and n <= cfg.max_contracts_per_trade and n * px <= cfg.max_dollars_per_trade
    db.put(e.c, "kill", True)
    pub.markets["KXBTC15M-D"] = mk("KXBTC15M-D", 99_900, 0.55, 0.50, minutes=4)
    e.cycle()
    assert len(tr.orders) == 1                 # kill switch blocks


def test_daily_loss_limit_trips_kill(cfg):
    cfg.trading_mode = "live"
    cfg.max_daily_loss = 5
    tr = FakeTrader()
    e, pub, _ = run(cfg, [], trader=tr)
    _seed(e.c, 30, 0.85)
    for i in range(3):
        db.record_trade(e.c, ts=time.time(), mode="live", asset="BTC", ticker=f"L{i}", side="yes",
                        price=0.6, contracts=5, fee=0.1, close_ts=5000 + i)
    for t in db.open_trades(e.c):
        if t["mode"] == "live":
            db.settle(e.c, t["id"], "no")
    pub.markets["KXBTC15M-E"] = mk("KXBTC15M-E", 99_900, 0.55, 0.50)
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


def test_dashboard_api(cfg):
    e, _, _ = run(cfg, [mk("KXBTC15M-A", 99_900, 0.55, 0.50)])
    import dashboard.app as dapp
    dapp.conn = db.connect(cfg.db_path)
    dapp.cfg.dash_user, dapp.cfg.dash_password = "admin", "s3cret"
    client = dapp.app.test_client()
    # locked until signed in; app files stay public so the phone can install it
    assert client.get("/").status_code == 302
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/kill").status_code == 401
    for path in ("/manifest.json", "/sw.js", "/icons/icon-192.png", "/login"):
        assert client.get(path).status_code == 200, path
    assert client.post("/login", data={"username": "admin", "password": "nope"}).status_code == 401
    r = client.post("/login", data={"username": "admin", "password": "s3cret", "remember": "on"})
    assert r.status_code == 302
    s = client.get("/api/state").get_json()
    assert s["paper"]["signals_today"] == 1
    assert [c["asset"] for c in s["cards"]] == ["BTC", "GOLD"]
    assert s["cards"][0]["status"] == "IN TRADE" or s["cards"][0]["status"] == "TRADE"
    assert client.post("/api/kill").status_code == 200
    assert client.get("/api/state").get_json()["killed"] is True
    assert b"direction" in client.get("/export.csv").data
    assert b"Kalshi 15-Min Bot" in client.get("/").data and b"manifest.json" in client.get("/").data
    client.get("/logout")
    assert client.get("/api/state").status_code == 401
