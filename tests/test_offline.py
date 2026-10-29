"""Offline tests with fake Kalshi data. Run: python -m pytest -q"""
import base64
import os
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
    if asset == "BTC":   # good price-accuracy history so the BTC price gate passes
        db.put(c, "btc_accuracy", [{"ts": 1000 + i, "ticker": f"W{i}", "ours": 80000 + 3, "kalshi": 80000,
                                    "diff": 3.0, "pct": 0.0038, "n": 30} for i in range(60)])


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


# ---------------- Natural gas, Paper/Live switch, Kalshi connection ----------------
def test_natgas_classified():
    assert classify_asset("KXNATGAS15M Natural Gas 15 min") == "NATGAS"
    assert "KXNATGAS15M" in Config().series


def _client(cfg):
    import dashboard.app as dapp
    dapp.conn = db.connect(cfg.db_path)
    dapp.cfg.dash_password = ""            # no login for these tests
    dapp.cfg.key_dir = os.path.join(os.path.dirname(cfg.db_path), "keys")
    dapp.cfg.api_key_id = dapp.cfg.private_key_path = ""
    return dapp, dapp.app.test_client()


def _pem():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                           serialization.NoEncryption()).decode()


def test_mode_switch_requires_key_and_confirmation(cfg, monkeypatch):
    dapp, c = _client(cfg)
    assert c.post("/api/mode", json={"mode": "live", "confirm": "LIVE"}).status_code == 400   # no key yet
    monkeypatch.setattr(dapp, "_verify_key", lambda k, p, d: 123.45)
    r = c.post("/api/kalshi-key", json={"key_id": "abcd-1234-efgh-5678", "pem": _pem(), "demo": True})
    assert r.get_json()["ok"] and r.get_json()["balance"] == 123.45
    saved = db.get(dapp.conn, "kalshi_key")
    assert oct(os.stat(saved["path"]).st_mode & 0o777) == "0o600"          # private key locked down
    s = c.get("/api/state").get_json()
    assert s["kalshi"]["connected"] and s["kalshi"]["key_hint"] == "…5678" and s["kalshi"]["demo"]
    assert "BEGIN" not in str(s)                                              # key never sent back
    assert c.post("/api/mode", json={"mode": "live", "confirm": "nope"}).status_code == 400
    assert c.post("/api/mode", json={"mode": "live", "confirm": "live"}).get_json()["ok"]
    assert c.get("/api/state").get_json()["mode"] == "live"
    c.delete("/api/kalshi-key")                                               # disconnect -> back to paper
    s = c.get("/api/state").get_json()
    assert s["mode"] == "paper" and not s["kalshi"]["connected"] and not os.path.exists(saved["path"])


def test_bad_key_rejected(cfg, monkeypatch):
    dapp, c = _client(cfg)
    assert c.post("/api/kalshi-key", json={"key_id": "abcd-1234", "pem": "hello"}).status_code == 400

    def boom(*a):
        raise RuntimeError("401 unauthorized")
    monkeypatch.setattr(dapp, "_verify_key", boom)
    r = c.post("/api/kalshi-key", json={"key_id": "abcd-1234-efgh", "pem": _pem()})
    assert r.status_code == 400 and "rejected" in r.get_json()["error"]
    assert db.get(dapp.conn, "kalshi_key") is None
    assert not os.listdir(dapp.cfg.key_dir)                                  # nothing left behind


def test_engine_follows_dashboard_switch_and_loads_key(cfg, tmp_path):
    e, pub, _ = run(cfg, [])
    assert e.mode() == "paper" and e.trader is None
    p = tmp_path / "k.pem"
    p.write_text(_pem())
    db.put(e.c, "kalshi_key", {"key_id": "abcd-1234-efgh", "path": str(p), "demo": True, "version": "1"})
    db.put(e.c, "trading_mode", "live")
    e.cycle()
    assert e.mode() == "live" and e.trader is not None and e.demo is True    # hot-loaded, no restart
    db.put(e.c, "kalshi_key", None)
    e.cycle()
    assert e.trader is None


def test_card_never_shows_ended_window(cfg):
    ended = mk("KXBTC15M-OLD", 99_900, 0.97, 0.95, minutes=-0.2)
    current = mk("KXBTC15M-NEW", 99_950, 0.55, 0.50, minutes=12)
    e, _, _ = run(cfg, [ended, current])
    card = next(c for c in db.get(e.c, "state")["cards"] if c["asset"] == "BTC")
    assert card["ticker"] == "KXBTC15M-NEW" and card["minutes_left"] > 11
    assert card["up_side"] == "yes" and card["yes_ask"] == 0.55


def test_login_has_show_password():
    html = open("dashboard/static/login.html").read()
    assert "Show password" in html and 'id="pw"' in html


# ---------------- BRTI-style price + accuracy vs Kalshi ----------------
class FakeSession:
    def __init__(self, data):
        self.data = data

    def get(self, url, **kw):
        class R:
            def __init__(s, j): s._j = j
            def json(s):
                if isinstance(s._j, Exception):
                    raise s._j
                return s._j
        for key, val in self.data.items():
            if key in url:
                return R(val)
        return R(RuntimeError("down"))


def test_composite_median_and_outlier():
    from scanner.prices import BtcComposite
    sess = FakeSession({
        "coinbase": {"bid": "80000", "ask": "80002", "price": "80001"},
        "kraken":   {"error": [], "result": {"XXBTZUSD": {"b": ["80004", "1", "1"], "a": ["80006", "1", "1"], "c": ["80005", "1"]}}},
        "bitstamp": {"bid": "79998", "ask": "80000", "last": "79999"},
        "gemini":   {"bid": "81000", "ask": "81002", "last": "81001"},      # bad tick, 1.2% off
    })
    comp = BtcComposite(session=sess, start=False)
    got = comp.sample_once(ts=100)
    assert all(got[k] for k in ("Coinbase", "Kraken", "Bitstamp", "Gemini"))
    ts, px, n, spread = comp.latest()
    assert n == 3 and 79998 < px < 80006          # Gemini outlier dropped
    for i in range(30):
        comp.add(1000 + i * 2, 80000 + i)
    avg, cnt = comp.avg(1060, 60)                 # samples in (1000, 1060]
    assert cnt == 29 and abs(avg - 80015.0) < 0.01   # sample at exactly 1000 is outside the window


def test_composite_survives_exchange_outage():
    from scanner.prices import BtcComposite
    sess = FakeSession({"coinbase": {"bid": "80000", "ask": "80002"},
                        "bitstamp": {"bid": "80001", "ask": "80003"}})
    comp = BtcComposite(session=sess, start=False)
    comp.sample_once(ts=5)
    assert comp.latest()[2] == 2                  # 2 of 4 exchanges is still enough
    comp2 = BtcComposite(session=FakeSession({"coinbase": {"bid": "1", "ask": "2"}}), start=False)
    comp2.sample_once()
    assert comp2.latest() is None                 # fewer than 2 good exchanges -> no price


def test_averaged_settlement_shrinks_uncertainty():
    from scanner.model import fair_yes
    m = {"strike_type": "greater_or_equal", "floor_strike": 80000}
    plain = fair_yes(m, 80040, 0.0008, 3, 1.0)
    averaged = fair_yes(m, 80040, 0.0008, 3 - 2 / 3, 1.0)
    assert averaged > plain > 0.5                 # less time for noise -> more confident


def test_accuracy_check_against_kalshi_target(cfg):
    from scanner.prices import BtcComposite
    comp = BtcComposite(session=FakeSession({}), start=False)
    opened = time.time() - 120
    for i in range(30):
        comp.add(opened - 59 + i * 2, 80010.0)    # our 60-sec average = 80010
    m = mk("KXBTC15M-ACC", 80004.0, 0.55, 0.50, minutes=10)
    m["open_time"] = datetime.fromtimestamp(opened, timezone.utc).isoformat().replace("+00:00", "Z")
    pub = FakePublic(); pub.markets[m["ticker"]] = m
    e = Engine(cfg, public=pub, price_fn=make_price_fn(), btc=comp)
    e.cycle(); e.cycle()
    hist = db.get(e.c, "btc_accuracy")
    assert len(hist) == 1 and hist[0]["diff"] == 6.0           # checked once per window
    st = db.get(e.c, "state")
    assert st["accuracy"]["BTC"]["mae"] == 6.0
    btc_gate = e.gate_status["BTC"]["checks"][0]
    assert btc_gate["name"] == "Price matches Kalshi" and not btc_gate["ok"]   # needs 40 checks


def test_google_sheet_feed(cfg):
    e, pub, _ = run(cfg, [mk("KXBTC15M-S", 99_900, 0.55, 0.50)])
    pub.results["KXBTC15M-S"] = "yes"
    e.cycle()
    import dashboard.app as dapp
    dapp.conn = db.connect(cfg.db_path)
    dapp.cfg.dash_password = ""
    c = dapp.app.test_client()
    url = c.get("/api/sheet-link").get_json()["url"]
    path = url.split("://", 1)[1].split("/", 1)[1]
    assert c.get("/sheet/wrong-token.csv").status_code == 404
    body = c.get("/" + path).data.decode()
    lines = body.strip().splitlines()
    assert lines[0] == "Time,Mode,Market,Call,Bought,Qty,Price,Odds %,Edge,Time left,Result,P&L,Momentum,Price source,Day,Day P&L"
    assert ",PAPER,BTC,UP,YES,1,0.56,75," in lines[1] and ",WON,0.42," in lines[1]   # 1 - 0.56 entry - 0.02 fee
    assert " · " in lines[1].split(",")[0] and "m " in lines[1].split(",")[9]
    new = c.get("/api/sheet-link?new=1").get_json()["url"]
    assert new != url and c.get("/" + path).status_code == 404     # old link dead after rotate



# ---------------- side-by-side strategy test ----------------
def test_variants_trade_same_window_with_their_own_rules(cfg):
    from scanner import variants
    # 80¢ is above the new 75¢ cap: only B (old rules, up to 90¢) may trade it; the main bot must skip it
    e, pub, _ = run(cfg, [mk("KXBTC15M-V", 99_700, 0.80, 0.75, minutes=6)])
    got = {r["variant"] for r in db.shadow_all(e.c)}
    assert got == {"B"} and db.recent(e.c) == []
    # 55¢ is inside the band: A trades it and mirrors the main bot exactly
    e2, pub2, _ = run(cfg, [mk("KXBTC15M-W", 99_900, 0.55, 0.50, minutes=6)])
    rows = db.shadow_all(e2.c)
    a = next(r for r in rows if r["variant"] == "A")
    main = db.recent(e2.c)
    assert main and main[0]["side"] == a["side"] and abs(main[0]["price"] - a["price"]) < 1e-9
    e2.cycle()
    assert len(db.shadow_all(e2.c)) == len(rows)           # still one trade per variant per window
    pub2.results["KXBTC15M-W"] = "yes"
    e2.cycle()
    st = {v["id"]: v for v in db.get(e2.c, "state")["variants"]}
    assert st["A"]["trades"] == 1 and st["A"]["win_rate"] == 100.0 and st["A"]["pnl"] > 0
    assert len(st) == len(variants.VARIANTS)


def test_variants_skip_outside_window_and_never_touch_gates(cfg):
    e, _, _ = run(cfg, [mk("KXBTC15M-W", 99_900, 0.55, 0.50, minutes=10)])   # 10 min left: too early
    assert db.shadow_all(e.c) == []
    e2, _, _ = run(cfg, [mk("KXBTC15M-X", 99_900, 0.55, 0.50, minutes=6)])
    assert db.shadow_all(e2.c)
    assert len(db.settled(e2.c, "paper", "BTC")) == 0      # gates read only the main bot's trades


def test_edge_variant_is_stricter():
    from scanner import variants
    from scanner.config import Config
    c = Config()
    m = {"strike_type": "greater_or_equal", "floor_strike": 100_000}
    q = {"yes_ask": 0.44, "yes_ask_size": 50, "no_ask": 0.60, "no_ask_size": 50}
    px = {"price": 100_010.0, "sigma_1m": 0.0008}
    mom = {"dir": "UP", "z": 1.0, "r1": 0.0001, "drift_1m": 0.0001}
    res = {v["id"]: variants.decide(variants.params(c, v), m, q, px, mom, 6, 5.33, "yes", 1.0)
           for v in variants.VARIANTS}
    edge = res["A"]["edge"]
    assert 0.05 <= edge < 0.08 and res["C"] is None          # A trades a 5-8¢ edge, C refuses
    assert res["D"]["edge"] < res["A"]["edge"]                 # calmer odds = less confident



# ---------------- GOLD via PAXG ----------------
def test_paxg_composite_reads_exchanges():
    from scanner.prices import GoldComposite
    sess = FakeSession({
        "coinbase": {"bid": "4100.10", "ask": "4100.90", "price": "4100.5"},
        "kraken":   {"error": [], "result": {"PAXGUSD": {"b": ["4101.0", "1", "1"], "a": ["4101.6", "1", "1"], "c": ["4101.3", "1"]}}},
    })
    comp = GoldComposite(session=sess, start=False)
    got = comp.sample_once(ts=10)
    assert got["Coinbase"] and got["Kraken"] and got["Gemini"] is None     # one exchange down is fine
    ts, px, n, spread = comp.latest()
    assert n == 2 and 4100.4 < px < 4101.4
    solo = GoldComposite(session=FakeSession({"coinbase": {"bid": "4100", "ask": "4101"}}), start=False)
    solo.sample_once()
    assert solo.latest()[2] == 1                                          # gold accepts a single live source


def _gold_engine(cfg, gap, windows=4):
    """Engine with a fake PAXG sampler that sits `gap` dollars below Kalshi's gold target every window."""
    from scanner.prices import GoldComposite
    comp = GoldComposite(session=FakeSession({}), start=False)
    pub = FakePublic()
    pub.discover = lambda: {"KXGOLD15M": {"asset": "GOLD", "title": "Gold 15 min", "fee_multiplier": 1.0}}
    now = time.time()
    for w in range(windows):
        opened = now - 60 * (15 * (windows - w))
        for i in range(30):
            comp.add(opened - 59 + i * 2, 4100.0)
        m = mk(f"KXGOLD15M-G{w}", 4100.0 + gap, 0.5, 0.45, minutes=10)
        m["open_time"] = datetime.fromtimestamp(opened, timezone.utc).isoformat().replace("+00:00", "Z")
        pub.markets[m["ticker"]] = m
    e = Engine(cfg, public=pub, price_fn=lambda f, s, l: {"price": 4100.0, "age_sec": 0, "sigma_1m": 0.0004,
                                                          "closes": [4100.0] * 61, "source": "PAXG",
                                                          "sources": 2, "max_sources": 3},
               btc=None)
    e._comp_override["GOLD"] = comp
    return e, pub


def test_gold_gap_is_learned_and_corrected(cfg):
    e, pub = _gold_engine(cfg, gap=12.0)
    e.cycle()
    hist = db.get(e.c, "gold_accuracy")
    assert len(hist) == 4 and all(h["diff"] == -12.0 for h in hist)
    assert e.basis_offset("GOLD") == 12.0                       # PAXG is $12 under Kalshi's gold price
    px = e.prices()["GOLD"]
    assert px["price"] == 4112.0 and px["offset"] == 12.0       # shifted onto Kalshi's level
    acc = db.get(e.c, "state")["accuracy"]["GOLD"]
    assert acc["gap"] == 12.0 and acc["mae"] == 0.0             # steady gap -> zero error after correction
    assert e.basis_offset("BTC") == 0.0                         # BTC is never shifted
    gate = e.gate_status["GOLD"]["checks"][0]
    assert gate["name"] == "Price matches Kalshi" and "0.05%" in gate["target"]


def test_gold_trade_records_price_source(cfg):
    e, pub = _gold_engine(cfg, gap=0.0, windows=0)
    pub.markets["KXGOLD15M-T"] = dict(mk("KXGOLD15M-T", 4095.0, 0.40, 0.35, minutes=6))
    e.price_fn = lambda f, s, l: dict(feed(4.0, 4096.0, 4100.0), source="PAXG", sources=2, max_sources=3)
    e.cycle()
    t = db.recent(e.c)
    assert t and t[0]["asset"] == "GOLD" and t[0]["src"].startswith("PAXG 2/3 ex")


def test_settlement_rules_read_from_kalshi(cfg):
    e, pub = _gold_engine(cfg, gap=0.0, windows=0)
    m = mk("KXGOLD15M-R", 4095.0, 0.40, 0.35, minutes=12)
    m["rules_primary"] = "If the simple average of the gold price in the 60 seconds before close is above the target, YES."
    pub.markets[m["ticker"]] = m
    e.cycle()
    assert e.settle_avg_min("GOLD", "KXGOLD15M") == 1.0
    assert "simple average" in db.get(e.c, "state")["rules"]["GOLD"]
    assert e.settle_avg_min("BTC", "KXBTC15M") == 1.0           # BTC set explicitly in config



# ---------------- new rules, settings panel, 24h scorecard, daily P&L ----------------
def test_new_rules_seeded_once_and_override_env(cfg):
    e, _, _ = run(cfg, [])
    assert e.cfg.mom_min_z == 0.8 and e.cfg.min_price == 0.40 and e.cfg.max_price == 0.75
    assert e.base_cfg.mom_min_z == 0.5                          # .env value untouched, overridden
    t0 = db.get(e.c, "rules_changed_at")
    db.put(e.c, "settings", {"mom_min_z": 1.2})                 # later change from the dashboard
    e2 = Engine(cfg, public=FakePublic(), price_fn=make_price_fn(), conn=e.c)
    assert e2.cfg.mom_min_z == 1.2 and db.get(e.c, "rules_changed_at") == t0   # seed not re-applied


def test_weak_and_spike_momentum_blocked(cfg):
    weak = feed(14, 99_986, 100_000)                            # momentum ~0.6: allowed before, blocked now
    e, _, rows = run(cfg, [mk("KXBTC15M-M", 99_900, 0.55, 0.50, minutes=6)], btc=weak)
    r = rows["KXBTC15M-M"]
    assert r["status"] == "SETUP FORMING" and "momentum weak" in r["note"] and "/ 0.8" in r["note"]
    db.put(e.c, "settings", {"mom_max_z": 1.0})
    rows = {x["ticker"]: x for x in e.cycle()}
    big = run(cfg, [mk("KXBTC15M-S", 99_900, 0.55, 0.50, minutes=6)])[0]
    db.put(big.c, "settings", {"mom_max_z": 1.0})
    r2 = {x["ticker"]: x for x in big.cycle()}["KXBTC15M-S"]
    assert "too big" in r2["note"] or r2["status"] == "IN TRADE"


def test_settings_api_validates_and_restarts_scorecard(cfg):
    e, _, _ = run(cfg, [])
    import dashboard.app as dapp
    dapp.conn = e.c
    dapp.cfg.dash_password = ""
    c = dapp.app.test_client()
    t0 = db.get(e.c, "rules_changed_at")
    assert c.post("/api/settings", json={"min_price": 0.8, "max_price": 0.6}).status_code == 400   # min > max
    assert c.post("/api/settings", json={"mom_min_z": 99}).status_code == 400                       # out of range
    r = c.post("/api/settings", json={"mom_min_z": 1.0, "note": "test"})
    assert r.get_json()["ok"] and db.get(e.c, "settings")["mom_min_z"] == 1.0
    assert db.get(e.c, "rules_changed_at") > t0
    e.cycle()
    assert e.cfg.mom_min_z == 1.0


def test_rule_test_scorecard_before_after(cfg):
    e, _, _ = run(cfg, [])
    t0 = time.time() - 1000
    db.put(e.c, "rules_changed_at", t0)
    for i, (dt, res) in enumerate([(-3600, "no"), (-1800, "no"), (-900, "yes"), (600, "yes"), (700, "yes")]):
        db.record_trade(e.c, ts=t0 + dt, mode="paper", asset="BTC", ticker=f"RT{i}", side="yes",
                        price=0.5, contracts=1, fee=0.02, close_ts=9000 + i)
    for t in db.open_trades(e.c):
        db.settle(e.c, t["id"], "yes" if t["ticker"] in ("RT2", "RT3", "RT4") else "no")
    rt = e.rule_test()
    assert rt["before"]["trades"] == 3 and rt["before"]["wins"] == 1
    assert rt["after"]["trades"] == 2 and rt["after"]["win_rate"] == 100.0 and rt["after"]["pnl"] > 0


def test_sheet_daily_pnl_midnight_to_midnight(cfg):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Chicago")
    e, _, _ = run(cfg, [])
    day1 = datetime(2026, 10, 24, 23, 30, tzinfo=tz).timestamp()
    day2 = datetime(2026, 10, 25, 0, 30, tzinfo=tz).timestamp()
    for i, (ts, res) in enumerate([(day1, "yes"), (day1 + 60, "no"), (day2, "yes"), (day2 + 60, "yes")]):
        db.record_trade(e.c, ts=ts, mode="paper", asset="BTC", ticker=f"D{i}", side="yes",
                        price=0.5, contracts=1, fee=0.02, close_ts=8000 + i)
    for i, t in enumerate(sorted(db.open_trades(e.c), key=lambda t: t["ts"])):
        db.settle(e.c, t["id"], ["yes", "no", "yes", "yes"][i])
    import dashboard.app as dapp
    dapp.conn = e.c
    dapp.cfg.dash_password = ""
    c = dapp.app.test_client()
    path = c.get("/api/sheet-link").get_json()["url"].split("://", 1)[1].split("/", 1)[1]
    lines = c.get("/" + path).data.decode().strip().splitlines()
    head = lines[0].split(",")
    assert head[-2:] == ["Day", "Day P&L"]
    rows = [l.split(",") for l in lines[1:]]
    assert rows[0][-2] == "Sun Oct 25" and rows[0][-1] == "0.96"      # newest row = Sunday total (0.48 + 0.48)
    assert rows[2][-2] == "Sat Oct 24" and rows[2][-1] == "-0.04"     # Saturday: +0.48 - 0.52
    daily = c.get("/" + path.replace(".csv", "/daily.csv")).data.decode().strip().splitlines()
    assert daily[0].startswith("Day,Trades,Wins,Win %,P&L") and daily[1].startswith("Sun Oct 25,2,2,100")


# ---------------- Schwab live futures (Silver / Oil / Nat Gas) ----------------
class FakeResp:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.text = data, code, str(data)

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSchwab:
    def __init__(self):
        self.posts, self.n = [], 0
        self.px = {"/SI": 54.0, "/CL": 61.5, "/NG": 3.105}

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append(data)
        self.n += 1
        return FakeResp({"access_token": f"A{self.n}", "refresh_token": "R1", "expires_in": 1800})

    def get(self, url, params=None, headers=None, timeout=None):
        assert headers["Authorization"].startswith("Bearer A")
        now = int(time.time() * 1000)
        return FakeResp({s: {"realtime": True, "quote": {"bidPrice": p - 0.005, "askPrice": p + 0.005,
                                                         "lastPrice": p, "quoteTime": now}}
                         for s, p in self.px.items() if s in params["symbols"].split(",")})


def test_silver_classified_and_always_watched():
    assert classify_asset("KXSILVER15M Silver 15 min") == "SILVER"
    os.environ["SERIES"] = "KXBTC15M,KXGOLD15M"            # an older .env list
    try:
        assert "KXSILVER15M" in Config().series
    finally:
        del os.environ["SERIES"]


def test_schwab_login_refresh_and_quotes(tmp_path):
    from scanner import schwab
    kd, fs = str(tmp_path), FakeSchwab()
    assert schwab.status(kd) == {"app": False, "connected": False, "days_left": None}
    schwab.save_app(kd, "K" * 32, "S" * 16, "https://x.sslip.io/schwab/callback")
    assert "client_id=" + "K" * 32 in schwab.auth_url(kd)
    st = schwab.exchange_code(kd, "CODE@", fs)
    assert st["connected"] and 6.9 < st["days_left"] <= 7
    assert oct(os.stat(os.path.join(kd, "schwab_tokens.json")).st_mode)[-3:] == "600"
    assert schwab.access_token(kd, fs) == "A1" and fs.n == 1               # still fresh: no refresh
    tok = schwab._read(os.path.join(kd, "schwab_tokens.json"))
    tok["access_exp"] = time.time() - 1
    schwab._write(os.path.join(kd, "schwab_tokens.json"), tok)
    assert schwab.access_token(kd, fs) == "A2" and fs.posts[-1]["grant_type"] == "refresh_token"
    q = schwab.quotes(kd, ["/SI", "/NG"], fs)
    assert q["/SI"]["price"] == pytest.approx(54.0) and q["/NG"]["realtime"]
    tok = schwab._read(os.path.join(kd, "schwab_tokens.json"))
    tok["refresh_exp"] = time.time() - 1                                    # 7 days passed
    schwab._write(os.path.join(kd, "schwab_tokens.json"), tok)
    assert schwab.quotes(kd, ["/SI"], fs) is None and not schwab.status(kd)["connected"]


def test_schwab_feed_builds_history_and_blocks_until_ready(tmp_path, cfg):
    from scanner import prices, schwab
    kd, fs = str(tmp_path), FakeSchwab()
    schwab.save_app(kd, "K" * 32, "S" * 16, "https://x/schwab/callback")
    schwab.exchange_code(kd, "C", fs)
    feed = prices.SchwabFeed(key_dir=kd, start=False, session=fs)
    prices._comps.pop("SILVER", None)
    store = feed.register("SILVER", "/SI")
    assert feed.poll_once() and feed.status == "live"
    now = time.time()
    for k in range(20, 0, -1):                       # 20 minutes of history, gently rising
        store.add(now - k * 60, 53.5 + 0.02 * (20 - k))
    old = prices._schwab
    prices._schwab = feed
    try:
        px = prices.schwab("/SI", 15)
    finally:
        prices._schwab = old
    assert px["source"] == "Schwab /SI" and px["sigma_1m"] and px["age_sec"] < 5 and len(px["closes"]) >= 15
    store.realtime = False                           # Schwab says delayed -> never tradeable
    prices._schwab = feed
    try:
        assert prices.schwab("/SI", 15)["age_sec"] >= 600
    finally:
        prices._schwab = old
        prices._comps.pop("SILVER", None)


def test_futures_card_says_connect_schwab(cfg, tmp_path):
    import dataclasses
    c2 = dataclasses.replace(cfg, key_dir=str(tmp_path))
    e = Engine(c2, public=FakePublic(), price_fn=lambda *a: None, conn=db.connect(":memory:"))
    m = {"ticker": "KXSILVER15M-X", "close_time": iso(5), "floor_strike": 54.0, "strike_type": "greater",
         "yes_ask_dollars": "0.50", "no_ask_dollars": "0.52", "yes_ask_size_fp": "50", "no_ask_size_fp": "50"}
    r = e.evaluate(m, "KXSILVER15M", {"asset": "SILVER"}, None)
    assert r["status"] == "BLOCKED" and "Connect Schwab" in r["note"]


def test_variant_f_skips_too_good_to_be_true():
    from scanner import variants
    c = Config()
    m = {"strike_type": "greater_or_equal", "floor_strike": 100_000}
    q = {"yes_ask": 0.40, "yes_ask_size": 50, "no_ask": 0.62, "no_ask_size": 50}
    px = {"price": 100_150.0, "sigma_1m": 0.0008}                 # far above target -> huge "edge"
    mom = {"dir": "UP", "z": 1.0, "r1": 0.0001, "drift_1m": 0.0001}
    p = lambda vid: variants.params(c, next(v for v in variants.VARIANTS if v["id"] == vid))
    a = variants.decide(p("A"), m, q, px, mom, 6, 5.33, "yes", 1.0)
    f = variants.decide(p("F"), m, q, px, mom, 6, 5.33, "yes", 1.0)
    assert a and a["edge"] > 0.20 and f is None


def test_gold_gap_follows_a_moving_gap(cfg):
    e, _ = _gold_engine(cfg, gap=0.0, windows=0)
    hist = [{"ts": i, "ticker": f"T{i}", "ours": 0, "kalshi": 4100.0, "diff": d, "pct": 0, "n": 30}
            for i, d in enumerate([-5.0] * 10 + [1.0] * 6)]       # gap jumps from $5 to -$1
    db.put(e.c, "gold_accuracy", hist)
    assert e.basis_offset("GOLD") == -1.0                         # caught up after 6 windows
    acc = e.accuracy_stats("GOLD")
    assert acc["gap"] == -1.0 and acc["worst"] == 6.0             # the jump is counted as real error


def test_schwab_contract_keys_map_back_to_root(tmp_path):
    from scanner import schwab
    kd, fs = str(tmp_path), FakeSchwab()
    schwab.save_app(kd, "K" * 32, "S" * 16, "https://x/schwab/callback")
    schwab.exchange_code(kd, "C", fs)
    real_get = fs.get

    def get(url, params=None, headers=None, timeout=None):     # Schwab answers with exact contracts
        r = real_get(url, params, headers, timeout)
        r._d = {k + "Z26": dict(v, symbol=k + "Z26") for k, v in r._d.items()}
        return r
    fs.get = get
    q = schwab.quotes(kd, ["/SI", "/CL"], fs)
    assert set(q) == {"/SI", "/CL"} and q["/SI"]["contract"] == "/SIZ26"
    fs.get = lambda *a, **k: FakeResp({"errors": {"invalidSymbols": ["/SI"]}})
    with pytest.raises(RuntimeError, match="no prices for /SI"):
        schwab.quotes(kd, ["/SI"], fs)


def test_variant_g_calibrated_odds_needs_a_bigger_gap():
    from scanner import variants
    c = Config()
    p = lambda vid: variants.params(c, next(v for v in variants.VARIANTS if v["id"] == vid))
    m = {"strike_type": "greater_or_equal", "floor_strike": 100_000}
    mom = {"dir": "UP", "z": 1.0, "r1": 0.0001, "drift_1m": 0.0001}
    q = {"yes_ask": 0.50, "yes_ask_size": 50, "no_ask": 0.52, "no_ask_size": 50}
    small = {"price": 100_040.0, "sigma_1m": 0.0008}       # modest gap: A trades, G does not
    a, g = (variants.decide(p(v), m, q, small, mom, 6, 5.33, "yes", 1.0) for v in "AG")
    assert a and g is None
    big = {"price": 100_400.0, "sigma_1m": 0.0008}         # large gap survives calibration
    g = variants.decide(p("G"), m, q, big, mom, 6, 5.33, "yes", 1.0)
    assert g and g["fair"] < 0.99 and g["edge"] >= 0.05
