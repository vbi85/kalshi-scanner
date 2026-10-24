"""Kalshi API client: public market data (no key) + signed trading calls (key required)."""
import base64
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from .config import Config, classify_asset


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_time(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class KalshiPublic:
    def __init__(self, cfg: Config, session=None):
        self.cfg = cfg
        self.s = session or requests.Session()
        self.s.headers.update({"Accept": "application/json", "User-Agent": "kalshi-scanner/1.0"})

    def _get(self, path, params=None):
        for attempt in range(3):
            r = self.s.get(self.cfg.data_base + path, params=params, timeout=10)
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def series_list(self, category=None):
        params = {"category": category} if category else None
        return self._get("/series", params).get("series", []) or []

    def series(self, ticker):
        return self._get(f"/series/{ticker}").get("series", {})

    def discover(self):
        """Return {series_ticker: {"asset":..., "title":..., "fee_multiplier":...}}"""
        found = {}
        for t in self.cfg.series:
            try:
                s = self.series(t)
                asset = classify_asset(f"{t} {s.get('title', '')}")
                if asset:
                    found[t] = {"asset": asset, "title": s.get("title", t),
                                "fee_multiplier": _f(s.get("fee_multiplier"), 1.0) or 1.0}
            except Exception as e:
                print(f"[discover] could not load series {t}: {e}")
        if self.cfg.auto_discover:
            for cat in self.cfg.discover_categories:
                try:
                    for s in self.series_list(cat):
                        t = s.get("ticker")
                        asset = classify_asset(f"{t} {s.get('title', '')}")
                        if t and asset and t not in found:
                            found[t] = {"asset": asset, "title": s.get("title", t),
                                        "fee_multiplier": _f(s.get("fee_multiplier"), 1.0) or 1.0}
                except Exception as e:
                    print(f"[discover] category {cat} failed: {e}")
        return found

    def open_markets(self, series_ticker, max_minutes):
        now = int(time.time())
        params = {"series_ticker": series_ticker, "status": "open", "limit": 1000,
                  "max_close_ts": now + int(max_minutes * 60)}
        out, cursor = [], None
        for _ in range(5):
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params)
            out.extend(data.get("markets", []) or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def markets_by_tickers(self, tickers):
        out = []
        for i in range(0, len(tickers), 50):
            chunk = tickers[i:i + 50]
            data = self._get("/markets", {"tickers": ",".join(chunk), "limit": 1000})
            out.extend(data.get("markets", []) or [])
        return out


def quote(m):
    """Normalize a market's top of book. NO ask = 1 - YES bid (Kalshi has one book)."""
    yes_ask = _f(m.get("yes_ask_dollars"))
    yes_bid = _f(m.get("yes_bid_dollars"))
    no_ask = _f(m.get("no_ask_dollars")) or (round(1 - yes_bid, 4) if yes_bid > 0 else 0.0)
    return {
        "yes_ask": yes_ask, "yes_ask_size": _f(m.get("yes_ask_size_fp")),
        "no_ask": no_ask, "no_ask_size": _f(m.get("yes_bid_size_fp")),
        "volume": _f(m.get("volume_fp")),
    }


class KalshiTrader:
    """Signed requests. Only constructed when live trading is configured."""

    def __init__(self, cfg: Config, session=None):
        from cryptography.hazmat.primitives import serialization
        self.cfg = cfg
        self.s = session or requests.Session()
        with open(cfg.private_key_path, "rb") as f:
            self.key = serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, method, url):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa, ed25519
        ts = str(int(time.time() * 1000))
        path = urlparse(url).path
        msg = f"{ts}{method}{path}".encode()
        if isinstance(self.key, ed25519.Ed25519PrivateKey):
            sig = self.key.sign(msg)
        elif isinstance(self.key, rsa.RSAPrivateKey):
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        else:
            raise ValueError("Unsupported key type; use an RSA or Ed25519 key from Kalshi")
        return {"KALSHI-ACCESS-KEY": self.cfg.api_key_id,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "KALSHI-ACCESS-TIMESTAMP": ts}

    def _req(self, method, path, **kw):
        url = self.cfg.order_base + path
        r = self.s.request(method, url, headers=self._sign(method, url), timeout=10, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Kalshi {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def balance(self):
        return _f(self._req("GET", "/portfolio/balance").get("balance_dollars"))

    def buy(self, ticker, side, price, count):
        """Buy YES or NO with an immediate-or-cancel limit order (never rests on the book).
        Kalshi V2 orders are on the YES leg: buying NO at q == selling YES at (1 - q)."""
        if side == "yes":
            leg, px = "bid", price
        else:
            leg, px = "ask", round(1 - price, 4)
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": leg,
            "count": f"{int(count)}.00",
            "price": f"{px:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        }
        r = self._req("POST", "/portfolio/events/orders", json=body)
        filled = _f(r.get("fill_count"))
        avg = _f(r.get("average_fill_price"), px)
        # convert YES-leg fill price back to the side we actually bought
        fill_px = avg if side == "yes" else round(1 - avg, 4)
        return {"order_id": r.get("order_id"), "filled": filled,
                "fill_price": fill_px if filled else None,
                "fee": _f(r.get("average_fee_paid")) * filled if filled else 0.0}
