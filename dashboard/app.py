"""Dashboard: black, simple, phone-installable, password-protected. Run: python -m dashboard.app"""
import csv
import hmac
import io
import os
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

from scanner import db
from scanner.config import ASSET_LABEL, Config

cfg = Config()
TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Chicago"))
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.json.sort_keys = False      # keep market order BTC, Gold, Oil, Nat Gas
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)   # behind Caddy (HTTPS)
conn = db.connect(cfg.db_path)

# Session secret is created once and kept in the database, so logins survive restarts.
_secret = db.get(conn, "session_secret")
if not _secret:
    _secret = secrets.token_hex(32)
    db.put(conn, "session_secret", _secret)
app.secret_key = _secret
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))

_fails = {}   # ip -> [count, first_fail_ts]   simple brute-force lockout


def _locked(ip):
    n, t0 = _fails.get(ip, (0, 0))
    if time.time() - t0 > 600:
        _fails.pop(ip, None)
        return False
    return n >= 5


def auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if cfg.dash_password and not session.get("ok"):
            if request.path.startswith("/api/") or request.path.endswith(".csv"):
                return jsonify(error="login required"), 401
            return redirect("/login")
        return f(*a, **kw)
    return wrap


def _login_page(error=""):
    html = open(os.path.join(app.static_folder, "login.html"), encoding="utf-8").read()
    return html.replace("{{ERROR}}", error)


@app.get("/login")
def login_form():
    if not cfg.dash_password or session.get("ok"):
        return redirect("/")
    return _login_page()


@app.post("/login")
def login():
    ip = request.remote_addr or "?"
    if _locked(ip):
        return _login_page("Too many attempts. Try again in 10 minutes."), 429
    u, p = request.form.get("username", ""), request.form.get("password", "")
    if hmac.compare_digest(u, cfg.dash_user) and hmac.compare_digest(p, cfg.dash_password or ""):
        _fails.pop(ip, None)
        session.clear()
        session["ok"] = True
        session.permanent = bool(request.form.get("remember"))
        app.config["SESSION_COOKIE_SECURE"] = request.scheme == "https"
        return redirect("/")
    n, t0 = _fails.get(ip, (0, time.time()))
    _fails[ip] = (n + 1, t0)
    return _login_page("Wrong username or password."), 401


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


# App files: public on purpose (no data in them) so the phone can install the app.
@app.get("/manifest.json")
def manifest():
    return send_from_directory(app.static_folder, "manifest.json", mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    r = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/icons/<path:name>")
def icons(name):
    return send_from_directory(os.path.join(app.static_folder, "icons"), name)


def summary(mode):
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE mode=?", (mode,))]
    done = [r for r in rows if r["status"] in ("won", "lost")]
    today = [r for r in done if (r["settled_ts"] or 0) >= start]
    wins = sum(1 for r in done if r["status"] == "won")
    return {
        "pnl_all": round(sum(r["pnl"] for r in done), 2),
        "pnl_today": round(sum(r["pnl"] for r in today), 2),
        "trades": len(done),
        "open": sum(1 for r in rows if r["status"] == "open"),
        "win_rate": round(100 * wins / len(done), 1) if done else None,
        "signals_today": sum(1 for r in rows if r["ts"] >= start),
    }


@app.get("/")
@auth
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
@auth
def state():
    s = db.get(conn, "state", {}) or {}
    s["killed"] = bool(db.get(conn, "kill", False))
    s["kill_reason"] = db.get(conn, "kill_reason", "")
    s["paper"] = summary("paper")
    s["live"] = summary("live")
    s["recent"] = db.recent(conn, 20)
    s["server_ts"] = time.time()
    s["labels"] = ASSET_LABEL
    s["mode"] = db.get(conn, "trading_mode") or s.get("mode") or cfg.trading_mode   # switch shows instantly
    k = db.get(conn, "kalshi_key")
    s["kalshi"] = ({"connected": True, "key_hint": "…" + k["key_id"][-4:], "demo": bool(k.get("demo")),
                    "connected_at": k.get("connected_at"), "balance": k.get("balance")}
                   if k else {"connected": bool(cfg.api_key_id and cfg.private_key_path), "key_hint": "from .env"
                              if cfg.api_key_id else None, "demo": cfg.demo})
    return jsonify(s)


@app.post("/api/mode")
@auth
def set_mode():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    if mode not in ("paper", "live"):
        return jsonify(ok=False, error="mode must be paper or live"), 400
    if mode == "live":
        if (body.get("confirm") or "").strip().upper() != "LIVE":
            return jsonify(ok=False, error='Type LIVE to confirm'), 400
        k = db.get(conn, "kalshi_key")
        if not k and not (cfg.api_key_id and cfg.private_key_path):
            return jsonify(ok=False, error="Connect your Kalshi account first"), 400
    db.put(conn, "trading_mode", mode)
    return jsonify(ok=True, mode=mode)


def _verify_key(key_id, pem_path, demo):
    """Make one signed balance call to prove the key works. Returns balance in dollars."""
    import dataclasses
    from scanner.kalshi import KalshiTrader
    kcfg = dataclasses.replace(cfg, api_key_id=key_id, private_key_path=pem_path, demo=demo)
    return KalshiTrader(kcfg).balance()


@app.post("/api/kalshi-key")
@auth
def save_key():
    from cryptography.hazmat.primitives import serialization
    body = request.get_json(silent=True) or {}
    key_id = (body.get("key_id") or "").strip()
    pem = (body.get("pem") or "").strip()
    demo = bool(body.get("demo"))
    if not key_id or len(key_id) < 8:
        return jsonify(ok=False, error="Paste the Key ID from Kalshi"), 400
    if "PRIVATE KEY" not in pem:
        return jsonify(ok=False, error="Paste the whole private key, including the BEGIN and END lines"), 400
    try:
        serialization.load_pem_private_key(pem.encode(), password=None)
    except Exception:
        return jsonify(ok=False, error="That private key could not be read. Copy it again exactly."), 400

    os.makedirs(cfg.key_dir, mode=0o700, exist_ok=True)
    final = os.path.join(cfg.key_dir, "kalshi_key.pem")
    tmp = final + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pem + "\n")
    try:
        balance = _verify_key(key_id, tmp, demo)
    except Exception as e:
        os.remove(tmp)
        msg = str(e)
        hint = "Kalshi rejected the key — check the Key ID matches this private key"
        if "demo" not in msg and demo:
            hint += ", and that it is a DEMO key (demo.kalshi.co)"
        return jsonify(ok=False, error=f"{hint}. ({msg[:120]})"), 400
    os.replace(tmp, final)
    db.put(conn, "kalshi_key", {"key_id": key_id, "path": os.path.abspath(final), "demo": demo,
                                "version": str(time.time()), "connected_at": time.time(), "balance": balance})
    return jsonify(ok=True, balance=balance, demo=demo)


@app.delete("/api/kalshi-key")
@auth
def delete_key():
    path = os.path.join(cfg.key_dir, "kalshi_key.pem")
    if os.path.exists(path):
        os.remove(path)
    db.put(conn, "kalshi_key", None)
    db.put(conn, "trading_mode", "paper")      # no key = no live trading
    return jsonify(ok=True)


@app.post("/api/kill")
@auth
def kill():
    db.put(conn, "kill", True)
    db.put(conn, "kill_reason", "stopped from dashboard")
    return jsonify(ok=True)


@app.post("/api/resume")
@auth
def resume():
    db.put(conn, "kill", False)
    db.put(conn, "kill_reason", "")
    return jsonify(ok=True)


@app.get("/export.csv")
@auth
def export():
    rows = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY ts")]
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            r["ts"] = datetime.fromtimestamp(r["ts"], TZ).strftime("%Y-%m-%d %H:%M:%S")
            if r["settled_ts"]:
                r["settled_ts"] = datetime.fromtimestamp(r["settled_ts"], TZ).strftime("%Y-%m-%d %H:%M:%S")
            w.writerow(r)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=kalshi_trades.csv"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=cfg.dash_port)
