"""Schwab market data: live CME futures prices for Oil (/CL), Nat Gas (/NG) and Silver (/SI).

How it connects (all from the dashboard, nothing typed on the server):
  1. You register a free app at developer.schwab.com (callback: https://<dashboard>/schwab/callback).
  2. Dashboard "Connect Schwab": paste App Key + Secret -> saved to keys/schwab_app.json (owner-only).
  3. Tap "Log in to Schwab" -> Schwab sends you back to /schwab/callback -> tokens saved to
     keys/schwab_tokens.json. The engine refreshes the 30-minute access token by itself.
  4. Schwab requires a fresh login every 7 days (their rule). The dashboard shows the countdown and
     the bot texts you a day before it runs out.
"""
import base64
import json
import os
import threading
import time
from urllib.parse import urlencode

import requests

API = "https://api.schwabapi.com"
REFRESH_LIFE = 7 * 86400          # Schwab refresh tokens last 7 days
ACCESS_MARGIN = 120               # refresh the access token 2 minutes before it expires
_lock = threading.Lock()


def _paths(key_dir):
    return os.path.join(key_dir, "schwab_app.json"), os.path.join(key_dir, "schwab_tokens.json")


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _write(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def save_app(key_dir, app_key, app_secret, redirect_uri):
    _write(_paths(key_dir)[0], {"key": app_key.strip(), "secret": app_secret.strip(),
                                "redirect": redirect_uri, "saved": time.time()})


def app_info(key_dir):
    return _read(_paths(key_dir)[0])


def disconnect(key_dir):
    for p in _paths(key_dir):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def auth_url(key_dir):
    app = app_info(key_dir)
    if not app:
        return None
    return f"{API}/v1/oauth/authorize?" + urlencode({"client_id": app["key"], "redirect_uri": app["redirect"]})


def _basic(app):
    return "Basic " + base64.b64encode(f"{app['key']}:{app['secret']}".encode()).decode()


def _token_call(app, data, session=None):
    s = session or requests
    r = s.post(f"{API}/v1/oauth/token", data=data, timeout=15,
               headers={"Authorization": _basic(app), "Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code != 200:
        raise RuntimeError(f"Schwab said {r.status_code}: {r.text[:200]}")
    return r.json()


def exchange_code(key_dir, code, session=None):
    """Called by the dashboard when Schwab redirects back after you log in."""
    app = app_info(key_dir)
    if not app:
        raise RuntimeError("enter your Schwab App Key and Secret first")
    tok = _token_call(app, {"grant_type": "authorization_code", "code": code,
                            "redirect_uri": app["redirect"]}, session)
    now = time.time()
    _write(_paths(key_dir)[1], {"access": tok["access_token"], "refresh": tok["refresh_token"],
                                "access_exp": now + int(tok.get("expires_in", 1800)),
                                "refresh_exp": now + REFRESH_LIFE, "login_ts": now})
    return status(key_dir)


def status(key_dir):
    app, tok = app_info(key_dir), _read(_paths(key_dir)[1])
    out = {"app": bool(app), "connected": False, "days_left": None}
    if tok:
        left = tok["refresh_exp"] - time.time()
        out.update(connected=left > 0, days_left=round(left / 86400, 1), login_ts=tok.get("login_ts"))
    return out


def access_token(key_dir, session=None):
    """A valid access token, refreshing it when needed. None if not connected / login expired."""
    with _lock:
        app, tok = app_info(key_dir), _read(_paths(key_dir)[1])
        if not app or not tok or time.time() > tok["refresh_exp"]:
            return None
        if time.time() < tok["access_exp"] - ACCESS_MARGIN:
            return tok["access"]
        new = _token_call(app, {"grant_type": "refresh_token", "refresh_token": tok["refresh"]}, session)
        tok.update(access=new["access_token"], access_exp=time.time() + int(new.get("expires_in", 1800)))
        if new.get("refresh_token") and new["refresh_token"] != tok["refresh"]:
            tok["refresh"] = new["refresh_token"]      # refresh token's 7-day clock does not restart
        _write(_paths(key_dir)[1], tok)
        return tok["access"]


def _root(sym):
    """'/SIZ26' -> '/SI' (futures root); plain symbols unchanged."""
    sym = (sym or "").upper()
    if sym.startswith("/") and len(sym) > 3 and sym[-2:].isdigit() and sym[-3].isalpha():
        return sym[:-3]
    return sym


def quotes(key_dir, symbols, session=None):
    """{requested symbol: {"price", "bid", "ask", "ts", "realtime"}} for futures like /CL, /NG, /SI.
    Schwab may answer under the root (/SI) or the exact contract (/SIZ26); both map back to /SI."""
    tok = access_token(key_dir, session)
    if not tok:
        return None
    s = session or requests
    r = s.get(f"{API}/marketdata/v1/quotes", params={"symbols": ",".join(symbols), "fields": "quote"},
              headers={"Authorization": f"Bearer {tok}"}, timeout=5)
    if r.status_code != 200:
        raise RuntimeError(f"Schwab quotes {r.status_code}: {r.text[:160]}")
    data = r.json() or {}
    wanted = {x.upper(): x.upper() for x in symbols}
    out = {}
    for key, d in data.items():
        if not isinstance(d, dict) or "quote" not in d and "lastPrice" not in d:
            continue
        q = d.get("quote") or d
        bid, ask, last = q.get("bidPrice"), q.get("askPrice"), q.get("lastPrice") or q.get("mark")
        mid = (bid + ask) / 2 if bid and ask and ask >= bid else last
        ts = (q.get("quoteTime") or q.get("tradeTime") or 0) / 1000
        req = next((wanted[c] for c in (key.upper(), _root(key), _root(d.get("symbol")),
                                         (d.get("symbol") or "").upper()) if c in wanted), None)
        if mid and req:
            out[req] = {"price": float(mid), "bid": bid, "ask": ask, "ts": ts or time.time(),
                        "realtime": d.get("realtime", True), "contract": d.get("symbol") or key}
    missing = [x for x in wanted if x not in out]
    if missing and not out:
        bad = (data.get("errors") or {}) if isinstance(data.get("errors"), dict) else {}
        raise RuntimeError(f"no prices for {','.join(missing)}; Schwab sent keys={list(data)[:5]} {str(bad)[:80]}")
    return out
