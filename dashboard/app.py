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
from scanner.config import Config

cfg = Config()
TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Chicago"))
app = Flask(__name__, static_folder="static", static_url_path="/static")
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
    return jsonify(s)


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
