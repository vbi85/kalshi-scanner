"""Underlying price feeds. Each returns {"price", "age_sec", "sigma_1m"} or None.

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
    return {"price": float(t["price"]), "age_sec": 0.0, "sigma_1m": _sigma(closes)}


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
    return {"price": float(price), "age_sec": age, "sigma_1m": _sigma(closes[-(lookback_min + 1):])}


FEEDS = {"coinbase": coinbase, "yahoo": yahoo}


def get_price(feed, symbol, lookback_min, session=None):
    try:
        return FEEDS[feed](symbol, lookback_min, session)
    except Exception as e:
        print(f"[prices] {feed}:{symbol} failed: {e}")
        return None
