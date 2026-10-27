"""Underlying price feeds. Each returns {"price", "age_sec", "sigma_1m"} or None.

closes   = last 1-minute closing prices, oldest first (used for momentum).
sigma_1m = standard deviation of 1-minute log returns over the lookback window.
age_sec  = how old the latest price is. Old prices are rejected by the engine.
"""
import math
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (kalshi-scanner)"}


def _sigma(closes):
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a and b]
    if len(rets) < 10:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def coinbase(symbol, lookback_min, session=None):
    s = session or requests
    t = s.get(f"https://api.exchange.coinbase.com/products/{symbol}/ticker", headers=UA, timeout=8).json()
    c = s.get(f"https://api.exchange.coinbase.com/products/{symbol}/candles",
              params={"granularity": 60}, headers=UA, timeout=8).json()
    # candles: [time, low, high, open, close, volume], newest first
    closes = [row[4] for row in reversed(c[:lookback_min + 1])]
    return {"price": float(t["price"]), "age_sec": 0.0, "sigma_1m": _sigma(closes), "closes": closes}


def yahoo(symbol, lookback_min, session=None):
    s = session or requests
    r = s.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
              params={"interval": "1m", "range": "1d"}, headers=UA, timeout=8).json()
    res = r["chart"]["result"][0]
    meta = res["meta"]
    closes = [x for x in res["indicators"]["quote"][0]["close"] if x]
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    if not price:
        return None
    age = time.time() - float(meta.get("regularMarketTime", 0))
    closes = closes[-(lookback_min + 1):]
    return {"price": float(price), "age_sec": age, "sigma_1m": _sigma(closes), "closes": closes}


# ---------------------------------------------------------------------------
# BRTI-style Bitcoin price
# Kalshi settles BTC 15-min markets on CF Benchmarks' BRTI (a paid, real-time blend of the
# major exchanges) averaged over the 60 seconds before open and before close.
# We rebuild a free copy: the median mid-price of Coinbase, Kraken, Bitstamp and Gemini,
# sampled every 2 seconds, with the same 60-second averaging.
# ---------------------------------------------------------------------------
import statistics
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor


def _mid(bid, ask, last=None):
    b, a = float(bid or 0), float(ask or 0)
    if b > 0 and a > 0 and a >= b:
        return (a + b) / 2
    return float(last) if last else None


BTC_SOURCES = {
    "Coinbase": ("https://api.exchange.coinbase.com/products/BTC-USD/ticker",
                 lambda j: _mid(j.get("bid"), j.get("ask"), j.get("price"))),
    "Kraken":   ("https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
                 lambda j: (lambda r: _mid(r["b"][0], r["a"][0], r["c"][0]))(next(iter(j["result"].values())))),
    "Bitstamp": ("https://www.bitstamp.net/api/v2/ticker/btcusd/",
                 lambda j: _mid(j.get("bid"), j.get("ask"), j.get("last"))),
    "Gemini":   ("https://api.gemini.com/v1/pubticker/btcusd",
                 lambda j: _mid(j.get("bid"), j.get("ask"), j.get("last"))),
}


# Live gold: PAXG is a token backed 1:1 by one troy ounce of gold, traded 24/7.
# It tracks spot gold in real time for free. The engine measures and removes the gap
# between PAXG and the price Kalshi actually settles gold on (see Engine.basis_offset).
PAXG_SOURCES = {
    "Coinbase": ("https://api.exchange.coinbase.com/products/PAXG-USD/ticker",
                 lambda j: _mid(j.get("bid"), j.get("ask"), j.get("price"))),
    "Kraken":   ("https://api.kraken.com/0/public/Ticker?pair=PAXGUSD",
                 lambda j: (lambda r: _mid(r["b"][0], r["a"][0], r["c"][0]))(next(iter(j["result"].values())))),
    "Gemini":   ("https://api.gemini.com/v1/pubticker/paxgusd",
                 lambda j: _mid(j.get("bid"), j.get("ask"), j.get("last"))),
}


class Composite:
    """Background price sampler: median of several exchanges every `interval` seconds.
    One per asset per process. Thread-safe reads."""

    def __init__(self, sources, session=None, interval=2.0, start=True, min_sources=2,
                 outlier_pct=0.003, min_price=1000, name="composite"):
        self.sources = sources
        self.s = session or requests.Session()
        self.interval = interval
        self.min_sources = min_sources
        self.outlier_pct = outlier_pct
        self.min_price = min_price
        self.samples = deque(maxlen=int(3 * 3600 / interval))   # 3 hours of history
        self.last_sources = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=len(sources))
        if start:
            threading.Thread(target=self._loop, daemon=True, name=name).start()

    def _fetch(self, name):
        url, parse = self.sources[name]
        try:
            j = self.s.get(url, headers=UA, timeout=1.8).json()
            v = parse(j)
            return name, (v if v and v > self.min_price else None)
        except Exception:
            return name, None

    def sample_once(self, ts=None):
        got = dict(self.pool.map(self._fetch, self.sources))
        prices = [v for v in got.values() if v]
        if len(prices) >= self.min_sources:
            med = statistics.median(prices)
            # drop any exchange too far from the others (bad tick / outage)
            good = [p for p in prices if abs(p / med - 1) < self.outlier_pct] or prices
            px = statistics.median(good)
            with self.lock:
                self.samples.append((ts or time.time(), px, len(good), max(good) - min(good)))
                self.last_sources = got
        return got

    def _loop(self):
        while True:
            t0 = time.time()
            try:
                self.sample_once()
            except Exception as e:
                print(f"[prices] sampler error: {e}")
            time.sleep(max(0.2, self.interval - (time.time() - t0)))

    def add(self, ts, price, n=4, spread=0.0):          # used by tests
        with self.lock:
            self.samples.append((ts, price, n, spread))

    def avg(self, end_ts, seconds=60):
        """Simple average of samples in (end_ts - seconds, end_ts] — the Kalshi settlement method."""
        with self.lock:
            xs = [p for t, p, _, _ in self.samples if end_ts - seconds < t <= end_ts]
        return (sum(xs) / len(xs), len(xs)) if xs else (None, 0)

    def minute_closes(self, minutes):
        """Last price in each of the past `minutes` minutes, oldest first."""
        now = time.time()
        with self.lock:
            snap = list(self.samples)
        out = []
        for k in range(minutes, -1, -1):
            edge = now - k * 60
            before = [p for t, p, _, _ in snap if t <= edge]
            if before:
                out.append(before[-1])
        return out

    def latest(self):
        with self.lock:
            return self.samples[-1] if self.samples else None


class BtcComposite(Composite):
    def __init__(self, session=None, interval=2.0, start=True):
        super().__init__(BTC_SOURCES, session, interval, start, min_sources=2, name="btc-composite")


class GoldComposite(Composite):
    # PAXG trades on fewer exchanges, so one live source is accepted (accuracy is still checked every window)
    def __init__(self, session=None, interval=2.0, start=True):
        super().__init__(PAXG_SOURCES, session, interval, start, min_sources=1, outlier_pct=0.004,
                         name="gold-composite")


_comps = {}
_FACTORY = {"BTC": BtcComposite, "GOLD": GoldComposite}


def composite(asset, start=True):
    """Running sampler for an asset (started on first use), or None if the asset has none."""
    if asset not in _FACTORY:
        return None
    if asset not in _comps and start:
        _comps[asset] = _FACTORY[asset]()
    return _comps.get(asset)


def btc_composite():
    return composite("BTC")


def _composite_feed(asset, candle_symbol, label, lookback_min, session=None):
    """Price for the engine from a composite + volatility/momentum history.
    Until we have our own history, Coinbase 1-min candles are shifted onto the composite level."""
    comp = composite(asset)
    last = comp.latest()
    if not last:
        time.sleep(2.5)                      # first call right after start-up
        last = comp.latest()
    if not last:
        return None
    ts, px, n_src, spread = last
    own = comp.minute_closes(lookback_min)
    if len(own) >= lookback_min:             # enough of our own history
        closes = own
    else:
        cb = coinbase(candle_symbol, lookback_min, session)
        shift = px - cb["price"]
        closes = [c + shift for c in cb["closes"]]
    closes = closes[:-1] + [px] if closes else [px]
    avg60, _ = comp.avg(time.time(), 60)
    return {"price": px, "age_sec": time.time() - ts, "sigma_1m": _sigma(closes), "closes": closes,
            "sources": n_src, "max_sources": len(comp.sources), "spread": spread, "avg60": avg60,
            "source": label}


def brti(symbol, lookback_min, session=None):
    return _composite_feed("BTC", "BTC-USD", "BRTI est.", lookback_min, session)


def paxg(symbol, lookback_min, session=None):
    return _composite_feed("GOLD", "PAXG-USD", "PAXG", lookback_min, session)


# ---------------------------------------------------------------------------
# Schwab: live CME futures (Oil /CL, Nat Gas /NG, Silver /SI). One background thread asks
# Schwab for all symbols every 2 seconds and keeps our own history (same averaging and
# accuracy checks as BTC/Gold). History needs ~11 minutes after a restart before trading.
# ---------------------------------------------------------------------------
import os

from . import schwab as schwab_api


class Store(Composite):
    """Price history filled from outside (no exchange polling of its own)."""

    def __init__(self, name="store"):
        self.sources = {name: None}
        self.samples = deque(maxlen=int(3 * 3600 / 2))
        self.last_sources = {}
        self.lock = threading.Lock()
        self.quote_ts = 0.0
        self.realtime = None


class SchwabFeed:
    def __init__(self, key_dir=None, interval=2.0, start=True, session=None):
        self.key_dir = key_dir or os.getenv("KEY_DIR", "keys")
        self.interval = interval
        self.s = session or requests.Session()
        self.symbols = {}                 # "/CL" -> asset
        self.status = "not connected"
        self.last_error = None
        self._err_ts = 0
        if start:
            threading.Thread(target=self._loop, daemon=True, name="schwab-feed").start()

    def register(self, asset, symbol):
        if asset not in _comps:
            _comps[asset] = Store(name="Schwab")
        self.symbols[symbol.upper()] = asset
        return _comps[asset]

    def poll_once(self):
        if not self.symbols:
            return
        got = schwab_api.quotes(self.key_dir, list(self.symbols), self.s)
        if got is None:
            self.status = "not connected"
            return False
        now = time.time()
        for sym, q in got.items():
            asset = self.symbols.get(sym)
            st = _comps.get(asset)
            if not st:
                continue
            spread = (q["ask"] - q["bid"]) if q.get("ask") and q.get("bid") else 0.0
            st.add(now, q["price"], 1, spread)
            st.quote_ts, st.realtime = q["ts"], q["realtime"]
        self.status = "live" if got else "no quotes"
        return True

    def _loop(self):
        while True:
            t0 = time.time()
            ok = None
            try:
                ok = self.poll_once()
                self.last_error = None
            except Exception as e:
                self.status, self.last_error = "error", str(e)[:200]
                if time.time() - self._err_ts > 300:
                    print(f"[prices] schwab error: {e}")
                    self._err_ts = time.time()
            wait = self.interval if ok else 10
            time.sleep(max(0.2, wait - (time.time() - t0)))


_schwab = None


def schwab_feed(start=True):
    global _schwab
    if _schwab is None and start:
        _schwab = SchwabFeed()
    return _schwab


def schwab(symbol, lookback_min, session=None):
    from .config import ASSETS
    asset = next((a for a, s in ASSETS.items() if s.get("symbol", "").upper() == symbol.upper()), symbol)
    store = schwab_feed().register(asset, symbol)
    last = store.latest()
    if not last:
        return None
    ts, px, _, spread = last
    closes = store.minute_closes(lookback_min)
    closes = closes[:-1] + [px] if closes else [px]
    avg60, _ = store.avg(time.time(), 60)
    age = time.time() - (store.quote_ts or ts)
    if store.realtime is False:
        age = max(age, 600)               # Schwab says delayed: never trade on it
    return {"price": px, "age_sec": age, "sigma_1m": _sigma(closes), "closes": closes, "spread": spread,
            "avg60": avg60, "source": f"Schwab {symbol.upper()}"}


FEEDS = {"coinbase": coinbase, "yahoo": yahoo, "brti": brti, "paxg": paxg, "schwab": schwab}


def get_price(feed, symbol, lookback_min, session=None):
    try:
        return FEEDS[feed](symbol, lookback_min, session)
    except Exception as e:
        print(f"[prices] {feed}:{symbol} failed: {e}")
        return None
