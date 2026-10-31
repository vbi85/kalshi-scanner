"""SQLite storage shared by the engine and the dashboard."""
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,              -- paper | live
    asset TEXT, series TEXT, ticker TEXT NOT NULL, side TEXT NOT NULL,
    price REAL NOT NULL,             -- entry price per contract
    fair REAL, fair_base REAL,       -- fair = with momentum, fair_base = without (proves momentum's value)
    direction TEXT, mom_z REAL, close_ts REAL,
    edge REAL, minutes_left REAL, spot REAL, strike TEXT,
    contracts REAL NOT NULL DEFAULT 1,
    fee REAL NOT NULL DEFAULT 0,
    order_id TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- open | won | lost | void
    pnl REAL, settled_ts REAL,
    UNIQUE(mode, ticker, side)
);
CREATE INDEX IF NOT EXISTS ix_trades_status ON trades(status);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS shadow (            -- side-by-side strategy test (paper only)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, variant TEXT NOT NULL, asset TEXT, ticker TEXT NOT NULL, close_ts REAL,
    side TEXT NOT NULL, direction TEXT, price REAL NOT NULL, fee REAL NOT NULL DEFAULT 0,
    fair REAL, edge REAL, mom_z REAL, minutes_left REAL,
    status TEXT NOT NULL DEFAULT 'open', pnl REAL, settled_ts REAL,
    UNIQUE(variant, asset, close_ts)            -- one trade per variant per market window
);
CREATE INDEX IF NOT EXISTS ix_shadow_status ON shadow(status);
CREATE TABLE IF NOT EXISTS exits (             -- early-exit test: "what if we had sold?" (record only)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL, rule TEXT NOT NULL, ts REAL NOT NULL,
    sell_price REAL NOT NULL, fee REAL NOT NULL, pnl REAL NOT NULL,
    hold_odds REAL, minutes_left REAL, reason TEXT,
    UNIQUE(trade_id, rule)                      -- first exit signal only
);
"""


def connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite3.connect(path, timeout=15, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
    if "src" not in cols:                       # price source used for the trade (added later)
        c.execute("ALTER TABLE trades ADD COLUMN src TEXT")
        c.commit()
    return c


def put(c, k, v):
    c.execute("INSERT INTO kv(k,v,ts) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts=excluded.ts",
              (k, json.dumps(v), time.time()))
    c.commit()


def get(c, k, default=None):
    r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def record_trade(c, **t):
    cols = ",".join(t.keys())
    q = ",".join("?" for _ in t)
    try:
        c.execute(f"INSERT INTO trades({cols}) VALUES({q})", tuple(t.values()))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False   # already have a position on this market+side


def has_position(c, mode, ticker):
    return c.execute("SELECT 1 FROM trades WHERE mode=? AND ticker=?", (mode, ticker)).fetchone() is not None


def has_window(c, mode, asset, close_ts):
    """One trade per asset per 15-minute window."""
    return c.execute("SELECT 1 FROM trades WHERE mode=? AND asset=? AND close_ts=?",
                     (mode, asset, close_ts)).fetchone() is not None


def window_trade(c, mode, ticker):
    r = c.execute("SELECT * FROM trades WHERE mode=? AND ticker=? ORDER BY ts DESC LIMIT 1", (mode, ticker)).fetchone()
    return dict(r) if r else None


def open_trades(c):
    return [dict(r) for r in c.execute("SELECT * FROM trades WHERE status='open'")]


def settle(c, trade_id, result):
    t = c.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not t:
        return
    won = (result == t["side"])
    n = t["contracts"]
    pnl = (n * (1 - t["price"]) if won else -n * t["price"]) - t["fee"]
    c.execute("UPDATE trades SET status=?, pnl=?, settled_ts=? WHERE id=?",
              ("won" if won else "lost", round(pnl, 4), time.time(), trade_id))
    c.commit()


def void(c, trade_id):
    c.execute("UPDATE trades SET status='void', pnl=0, settled_ts=? WHERE id=?", (time.time(), trade_id))
    c.commit()


def settled(c, mode=None, asset=None):
    q, a = "SELECT * FROM trades WHERE status IN ('won','lost')", []
    if mode:
        q += " AND mode=?"; a.append(mode)
    if asset:
        q += " AND asset=?"; a.append(asset)
    return [dict(r) for r in c.execute(q + " ORDER BY settled_ts", a)]


def recent(c, limit=50):
    return [dict(r) for r in c.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,))]


# ---------------- side-by-side strategy test ----------------
def shadow_record(c, **t):
    try:
        c.execute(f"INSERT INTO shadow({','.join(t)}) VALUES({','.join('?' for _ in t)})", tuple(t.values()))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def shadow_open(c):
    return [dict(r) for r in c.execute("SELECT * FROM shadow WHERE status='open'")]


def shadow_settle(c, row_id, result):
    t = c.execute("SELECT * FROM shadow WHERE id=?", (row_id,)).fetchone()
    if not t:
        return
    won = result == t["side"]
    pnl = ((1 - t["price"]) if won else -t["price"]) - t["fee"]
    c.execute("UPDATE shadow SET status=?, pnl=?, settled_ts=? WHERE id=?",
              ("won" if won else "lost", round(pnl, 4), time.time(), row_id))
    c.commit()


def shadow_void(c, row_id):
    c.execute("UPDATE shadow SET status='void', pnl=0, settled_ts=? WHERE id=?", (time.time(), row_id))
    c.commit()


def shadow_all(c):
    return [dict(r) for r in c.execute("SELECT * FROM shadow ORDER BY ts")]


# ---------------- early-exit test ----------------
def exit_record(c, **e):
    try:
        c.execute(f"INSERT INTO exits({','.join(e)}) VALUES({','.join('?' for _ in e)})", tuple(e.values()))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def exits_all(c):
    return [dict(r) for r in c.execute("SELECT * FROM exits")]
