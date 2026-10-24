"""Dashboard: black, simple, password-protected. Run: python -m dashboard.app"""
import csv
import io
import os
import time
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, request, send_from_directory

from scanner import db
from scanner.config import Config

cfg = Config()
TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Chicago"))
app = Flask(__name__, static_folder="static")
conn = db.connect(cfg.db_path)


def auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if cfg.dash_password:
            a_ = request.authorization
            if not a_ or a_.username != cfg.dash_user or a_.password != cfg.dash_password:
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Scanner"'})
        return f(*a, **kw)
    return wrap


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
