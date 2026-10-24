"""Main loop: discover markets -> price them -> paper trade every signal ->
live trade only assets that passed every approval gate -> settle -> publish state."""
import math
import os
import time
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db, gates
from .alerts import Alerter
from .config import ASSETS, Config
from .kalshi import KalshiPublic, KalshiTrader, parse_time, quote
from .model import edges, fair_yes, taker_fee
from .prices import get_price

TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Chicago"))


def strike_text(m):
    st, lo, hi = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")
    if st in ("greater", "greater_or_equal"):
        return f"≥ {lo:,}" if lo is not None else "?"
    if st in ("less", "less_or_equal"):
        return f"< {hi:,}" if hi is not None else "?"
    if st == "between":
        return f"{lo:,} – {hi:,}"
    return st or "?"


def day_start_ts():
    now = datetime.now(TZ)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Engine:
    def __init__(self, cfg: Config, public=None, trader=None, price_fn=None, conn=None):
        self.cfg = cfg
        self.pub = public or KalshiPublic(cfg)
        self.trader = trader
        if self.trader is None and cfg.trading_mode == "live" and cfg.api_key_id and cfg.private_key_path:
            try:
                self.trader = KalshiTrader(cfg)
            except Exception as e:
                print(f"[engine] live trader not available: {e}")
        self.price_fn = price_fn or get_price
        self.c = conn or db.connect(cfg.db_path)
        self.alerts = Alerter(cfg)
        self.series = {}
        self.last_discover = 0
        self.gate_status = {}
        self.errors = []

    # ---------------- helpers ----------------
    def err(self, msg):
        print(f"[engine] {msg}")
        self.errors = (self.errors + [f"{datetime.now(TZ):%H:%M:%S} {msg}"])[-10:]

    def killed(self):
        return bool(db.get(self.c, "kill", False))

    def kill(self, reason):
        if not self.killed():
            db.put(self.c, "kill", True)
            db.put(self.c, "kill_reason", reason)
            self.alerts.send(f"KILL SWITCH ON: {reason}. Live trading stopped.", force=True)

    # ---------------- risk ----------------
    def live_risk_ok(self, cost):
        start = day_start_ts()
        rows = self.c.execute("SELECT * FROM trades WHERE mode='live' AND ts>=?", (start,)).fetchall()
        realized = sum(r["pnl"] or 0 for r in rows if r["status"] in ("won", "lost"))
        open_rows = [r for r in self.c.execute("SELECT * FROM trades WHERE mode='live' AND status='open'")]
        open_cost = sum(r["price"] * r["contracts"] + r["fee"] for r in open_rows)
        if realized <= -self.cfg.max_daily_loss:
            self.kill(f"daily loss limit ${self.cfg.max_daily_loss:g} hit")
            return False, "daily loss limit"
        if len(rows) >= self.cfg.max_trades_per_day:
            return False, "max trades today"
        if len(open_rows) >= self.cfg.max_open_positions:
            return False, "max open positions"
        if realized - open_cost - cost < -self.cfg.max_daily_loss:
            return False, "would exceed daily loss budget"
        return True, ""

    def check_loss_streak(self):
        n = self.cfg.live_loss_streak_kill
        last = self.c.execute("SELECT status FROM trades WHERE mode='live' AND status IN ('won','lost') "
                              "ORDER BY settled_ts DESC LIMIT ?", (n,)).fetchall()
        if len(last) == n and all(r["status"] == "lost" for r in last):
            self.kill(f"{n} live losses in a row")

    def live_enabled(self, asset):
        return (self.cfg.trading_mode == "live" and self.trader is not None
                and not self.killed() and self.gate_status.get(asset, {}).get("passed", False))

    # ---------------- cycle steps ----------------
    def discover(self):
        if time.time() - self.last_discover < self.cfg.discover_every_min * 60 and self.series:
            return
        found = self.pub.discover()
        if found:
            self.series = found
            self.last_discover = time.time()
            print(f"[engine] scanning {len(found)} series: {', '.join(sorted(found))}")

    def prices(self):
        out = {}
        for asset in {v["asset"] for v in self.series.values()}:
            spec = ASSETS[asset]
            out[asset] = self.price_fn(spec["feed"], spec["symbol"], self.cfg.vol_lookback_min)
        return out

    def evaluate(self, m, series_ticker, info, px):
        cfg = self.cfg
        close = parse_time(m.get("close_time"))
        mins = (close - datetime.now(timezone.utc)).total_seconds() / 60 if close else None
        q = quote(m)
        row = {"asset": info["asset"], "series": series_ticker,
               "ticker": m["ticker"], "strike": strike_text(m), "minutes_left": mins,
               "yes_ask": q["yes_ask"], "no_ask": q["no_ask"], "spot": px["price"] if px else None,
               "fair": None, "side": None, "edge": None, "signal": False, "note": ""}
        if mins is None or mins < cfg.min_minutes_to_close:
            row["note"] = "too close to expiry"; return row
        if not px or not px.get("sigma_1m"):
            row["note"] = "no price feed"; return row
        max_age = max(cfg.max_price_age_sec, mins * 60 * 0.05)
        if px["age_sec"] > max_age:
            row["note"] = f"price {px['age_sec']/60:.0f}m old"; return row
        fair = fair_yes(m, px["price"], px["sigma_1m"], mins, cfg.vol_mult)
        if fair is None:
            row["note"] = "strike type not modeled"; return row
        row["fair"] = fair
        e = edges(fair, q, info.get("fee_multiplier", 1.0), cfg.slippage)
        if not e:
            row["note"] = "no offers"; return row
        side = max(e, key=e.get)
        price = q[f"{side}_ask"]
        size = q[f"{side}_ask_size"]
        row.update(side=side, edge=e[side], price=price, size=size)
        if not (cfg.min_price <= price <= cfg.max_price):
            row["note"] = "price outside range"
        elif size < cfg.min_ask_size:
            row["note"] = "thin book"
        elif e[side] < cfg.min_edge:
            row["note"] = "edge too small"
        else:
            row["signal"] = True
        return row

    def act(self, r, info):
        fee_mult = info.get("fee_multiplier", 1.0)
        base = dict(ts=time.time(), asset=r["asset"], series=r["series"], ticker=r["ticker"], side=r["side"],
                    fair=round(r["fair"], 4), edge=round(r["edge"], 4), minutes_left=round(r["minutes_left"], 2),
                    spot=r["spot"], strike=r["strike"])
        price = round(min(0.99, r["price"] + self.cfg.slippage), 4)  # paper assumes 1c worse fill
        new_paper = not db.has_position(self.c, "paper", r["ticker"]) and db.record_trade(self.c, mode="paper", price=price, contracts=1,
                                    fee=taker_fee(price, 1, fee_mult), **base)
        if new_paper:
            mode = "LIVE" if self.live_enabled(r["asset"]) else "paper"
            self.alerts.send(f"{r['asset']} {r['side'].upper()} @{r['price']:.2f} fair {r['fair']:.2f} "
                             f"edge +{r['edge']:.2f} | {r['minutes_left']:.0f}m left | {r['strike']} [{mode}]",
                             key=f"{r['ticker']}:{r['side']}")
        if not self.live_enabled(r["asset"]) or db.has_position(self.c, "live", r["ticker"]):
            return
        cfg = self.cfg
        n = int(min(cfg.max_contracts_per_trade, math.floor(cfg.max_dollars_per_trade / r["price"]), r["size"]))
        if n < 1:
            return
        cost = n * r["price"] + taker_fee(r["price"], n, fee_mult)
        ok, why = self.live_risk_ok(cost)
        if not ok:
            r["note"] = f"live blocked: {why}"
            return
        try:
            fill = self.trader.buy(r["ticker"], r["side"], r["price"], n)
        except Exception as e:
            self.err(f"order failed {r['ticker']}: {e}")
            return
        if fill["filled"] >= 1:
            db.record_trade(self.c, mode="live", price=fill["fill_price"], contracts=fill["filled"],
                            fee=round(fill["fee"], 4), order_id=fill["order_id"], **base)
            self.alerts.send(f"LIVE FILLED: {r['asset']} {r['side'].upper()} {fill['filled']:.0f} @ "
                             f"{fill['fill_price']:.2f} | {r['ticker']}", force=True)
        else:
            r["note"] = "live order not filled (price moved)"

    def settle_open(self):
        open_t = db.open_trades(self.c)
        if not open_t:
            return
        tickers = sorted({t["ticker"] for t in open_t})
        try:
            mk = {m["ticker"]: m for m in self.pub.markets_by_tickers(tickers)}
        except Exception as e:
            self.err(f"settlement check failed: {e}")
            return
        for t in open_t:
            m = mk.get(t["ticker"])
            if not m:
                continue
            res = (m.get("result") or "").lower()
            if res in ("yes", "no"):
                db.settle(self.c, t["id"], res)
            elif m.get("status") in ("finalized", "settled") and res not in ("yes", "no"):
                db.void(self.c, t["id"])
        self.check_loss_streak()

    def update_gates(self):
        for asset in sorted({v["asset"] for v in self.series.values()} | {"BTC"}):
            passed, checks, s = gates.evaluate(db.settled(self.c, "paper", asset), self.cfg)
            was = self.gate_status.get(asset, {}).get("passed")
            self.gate_status[asset] = {"passed": passed, "checks": checks, "stats": s}
            if passed and was is False:
                self.alerts.send(f"{asset} PASSED all approval gates. "
                                 f"{'Live trading active.' if self.cfg.trading_mode == 'live' else 'Set TRADING_MODE=live to go live.'}",
                                 force=True)
            if was and not passed:
                self.alerts.send(f"{asset} FAILED a gate. Back to paper only.", force=True)

    def publish(self, rows, prices):
        balance = None
        if self.trader:
            try:
                balance = self.trader.balance()
            except Exception as e:
                self.err(f"balance: {e}")
        rows = sorted(rows, key=lambda r: (not r["signal"], -(r["edge"] if r["edge"] is not None else -9)))
        db.put(self.c, "state", {
            "ts": time.time(),
            "mode": self.cfg.trading_mode,
            "demo": self.cfg.demo,
            "live_ready": self.trader is not None,
            "killed": self.killed(),
            "kill_reason": db.get(self.c, "kill_reason", ""),
            "balance": balance,
            "series": {k: v["asset"] for k, v in self.series.items()},
            "prices": {a: ({"price": p["price"], "age_sec": round(p["age_sec"]), "sigma_1m": p["sigma_1m"]} if p else None)
                       for a, p in prices.items()},
            "gates": self.gate_status,
            "opportunities": rows[:40],
            "errors": self.errors,
            "settings": {"min_edge": self.cfg.min_edge, "max_contracts": self.cfg.max_contracts_per_trade,
                         "max_dollars": self.cfg.max_dollars_per_trade, "max_daily_loss": self.cfg.max_daily_loss,
                         "poll_seconds": self.cfg.poll_seconds},
        })

    def cycle(self):
        self.discover()
        px = self.prices()
        self.settle_open()
        self.update_gates()
        rows = []
        for st, info in self.series.items():
            try:
                markets = self.pub.open_markets(st, self.cfg.max_minutes_to_close)
            except Exception as e:
                self.err(f"{st}: {e}")
                continue
            for m in markets:
                r = self.evaluate(m, st, info, px.get(info["asset"]))
                if r["signal"]:
                    self.act(r, info)
                rows.append(r)
        self.publish(rows, px)
        return rows

    def run(self):
        print(f"[engine] mode={self.cfg.trading_mode} demo={self.cfg.demo} live_ready={self.trader is not None}")
        self.alerts.send(f"Scanner started ({self.cfg.trading_mode} mode).", force=True)
        while True:
            t0 = time.time()
            try:
                self.cycle()
            except Exception as e:
                self.err(f"cycle error: {e}")
                traceback.print_exc()
            time.sleep(max(1, self.cfg.poll_seconds - (time.time() - t0)))


if __name__ == "__main__":
    Engine(Config()).run()
