"""Main loop for the 15-minute UP/DOWN bot.

Every cycle, for each market (BTC, Gold, Oil):
  1. Read the current 15-minute window: target price, time left, Kalshi prices.
  2. Measure momentum over the last 5 minutes -> predict UP or DOWN.
  3. Price the odds of that direction winning (momentum-adjusted).
  4. Only between 7:30 and 2:00 left: if momentum is strong, still pushing, and the odds
     beat Kalshi's price after fees -> TRADE (paper always; live only if gates passed).
  5. Max one trade per asset per window. Settle results, update gates, publish to dashboard.
"""
import dataclasses
import math
import os
import time
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db, gates
from .alerts import Alerter
from .config import ASSET_LABEL, ASSET_ORDER, ASSETS, Config
from .kalshi import KalshiPublic, KalshiTrader, parse_time, quote
from .model import edges, fair_yes, momentum, taker_fee, up_side
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


def mmss(minutes):
    s = max(0, int(round(minutes * 60)))
    return f"{s // 60}:{s % 60:02d}"


class Engine:
    def __init__(self, cfg: Config, public=None, trader=None, price_fn=None, conn=None):
        self.cfg = cfg
        self.pub = public or KalshiPublic(cfg)
        self.trader = trader
        self._injected = trader is not None      # tests pass a fake trader
        self._key_version = None
        self.demo = cfg.demo
        self._last_mode = None
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
        open_rows = self.c.execute("SELECT * FROM trades WHERE mode='live' AND status='open'").fetchall()
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

    def mode(self):
        """Paper/Live switch from the dashboard wins; .env TRADING_MODE is only the default."""
        m = db.get(self.c, "trading_mode") or self.cfg.trading_mode
        return "live" if m == "live" else "paper"

    def refresh_trader(self):
        """Load (or reload) the Kalshi API key saved from the dashboard, without restarting."""
        if self._injected:
            return
        info = db.get(self.c, "kalshi_key")
        if not info and self.cfg.api_key_id and self.cfg.private_key_path:
            info = {"key_id": self.cfg.api_key_id, "path": self.cfg.private_key_path,
                    "demo": self.cfg.demo, "version": "env"}
        version = info.get("version") if info else None
        if version == self._key_version:
            return
        self._key_version = version
        self.trader = None
        if not info:
            return
        try:
            kcfg = dataclasses.replace(self.cfg, api_key_id=info["key_id"], private_key_path=info["path"],
                                       demo=bool(info.get("demo")))
            self.trader = KalshiTrader(kcfg)
            self.demo = kcfg.demo
            print(f"[engine] Kalshi key loaded ({'demo' if self.demo else 'real money'} account)")
        except Exception as e:
            self.err(f"Kalshi key could not be loaded: {e}")

    def watch_mode(self):
        m = self.mode()
        if self._last_mode is not None and m != self._last_mode:
            self.alerts.send(f"Bot switched to {m.upper()} mode."
                             + (" Real orders only for approved markets." if m == "live" else ""), force=True)
        self._last_mode = m

    def live_enabled(self, asset):
        return (self.mode() == "live" and self.trader is not None
                and not self.killed() and self.gate_status.get(asset, {}).get("passed", False))

    # ---------------- cycle steps ----------------
    def discover(self):
        if time.time() - self.last_discover < self.cfg.discover_every_min * 60 and self.series:
            return
        found = self.pub.discover()
        if found:
            self.series = found
            self.last_discover = time.time()
            print(f"[engine] watching {len(found)} markets: {', '.join(sorted(found))}")

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
        usd = up_side(m)
        target = m.get("floor_strike") if usd == "yes" else m.get("cap_strike")
        pos = db.window_trade(self.c, "paper", m["ticker"])
        row = {"asset": info["asset"], "series": series_ticker, "ticker": m["ticker"],
               "strike": strike_text(m), "target": target,
               "close_ts": close.timestamp() if close else None, "minutes_left": mins,
               "spot": px["price"] if px else None, "yes_ask": q["yes_ask"], "no_ask": q["no_ask"],
               "up_side": usd,
               "direction": None, "mom_z": None, "conf": None, "fair": None, "fair_base": None,
               "side": None, "price": None, "size": None, "edge": None,
               "status": "WAITING", "note": "", "signal": False,
               "position": ({"side": pos["side"], "price": pos["price"], "status": pos["status"],
                             "pnl": pos["pnl"]} if pos else None)}

        def blocked(note):
            row["status"] = "IN TRADE" if pos else "BLOCKED"
            row["note"] = note
            return row

        if mins is None:
            return blocked("no close time")
        if usd is None:
            return blocked("not an up/down market")
        if target is None:
            return blocked("target price not set yet")
        if not px or not px.get("sigma_1m"):
            return blocked("price feed down")
        if px["age_sec"] > cfg.max_price_age_sec:
            return blocked(f"price {px['age_sec'] / 60:.0f} min delayed — unsafe")
        mom = momentum(px["price"], px.get("closes"), px["sigma_1m"], cfg.mom_lookback_min)
        if not mom:
            return blocked("not enough price history")

        drift = cfg.mom_weight * mom["drift_1m"]
        fair = fair_yes(m, px["price"], px["sigma_1m"], mins, cfg.vol_mult, drift)
        base = fair_yes(m, px["price"], px["sigma_1m"], mins, cfg.vol_mult, 0.0)
        if fair is None:
            return blocked("cannot price this market")
        side = usd if mom["dir"] == "UP" else ("no" if usd == "yes" else "yes")
        e = edges(fair, q, info.get("fee_multiplier", 1.0), cfg.slippage).get(side)
        row.update(direction=mom["dir"], mom_z=mom["z"], fair=fair, fair_base=base, side=side,
                   conf=fair if side == "yes" else 1 - fair,
                   price=q[f"{side}_ask"] or None, size=q[f"{side}_ask_size"], edge=e)

        if pos:
            row["status"], row["note"] = "IN TRADE", f"holding {pos['side'].upper()} @ {pos['price']:.2f}"
        elif mins > cfg.entry_start_min:
            row["status"], row["note"] = "WAITING", f"trading opens in {mmss(mins - cfg.entry_start_min)}"
        elif mins < cfg.entry_end_min:
            row["status"], row["note"] = "CLOSED", f"no new trades under {mmss(cfg.entry_end_min)}"
        elif abs(mom["z"]) < cfg.mom_min_z:
            row["status"], row["note"] = "SETUP FORMING", f"momentum weak ({abs(mom['z']):.2f} / {cfg.mom_min_z:g})"
        elif mom["r1"] * mom["drift_1m"] < 0:
            row["status"], row["note"] = "SETUP FORMING", "momentum fading — last minute reversed"
        elif e is None or not row["price"]:
            row["status"], row["note"] = "SETUP FORMING", "no sellers on that side"
        elif not (cfg.min_price <= row["price"] <= cfg.max_price):
            row["status"], row["note"] = "SETUP FORMING", f"price {row['price']:.2f} outside {cfg.min_price:g}–{cfg.max_price:g}"
        elif row["size"] < cfg.min_ask_size:
            row["status"], row["note"] = "SETUP FORMING", "too few contracts offered"
        elif e < cfg.min_edge:
            row["status"], row["note"] = "SETUP FORMING", f"price too high for the odds (edge {e:+.2f})"
        else:
            row["status"], row["signal"] = "TRADE", True
            row["note"] = f"{mom['dir']} setup confirmed, edge {e:+.2f}"
        return row

    def act(self, r, info):
        fee_mult = info.get("fee_multiplier", 1.0)
        base = dict(ts=time.time(), asset=r["asset"], series=r["series"], ticker=r["ticker"], side=r["side"],
                    fair=round(r["fair"], 4), fair_base=round(r["fair_base"], 4), direction=r["direction"],
                    mom_z=round(r["mom_z"], 3), close_ts=r["close_ts"], edge=round(r["edge"], 4),
                    minutes_left=round(r["minutes_left"], 2), spot=r["spot"], strike=r["strike"])
        live = self.live_enabled(r["asset"])
        if not db.has_window(self.c, "paper", r["asset"], r["close_ts"]):
            price = round(min(0.99, r["price"] + self.cfg.slippage), 4)  # paper assumes 1c worse fill
            if db.record_trade(self.c, mode="paper", price=price, contracts=1,
                               fee=taker_fee(price, 1, fee_mult), **base):
                arrow = "▲" if r["direction"] == "UP" else "▼"
                self.alerts.send(f"{arrow} {r['asset']} {r['direction']}: buy {r['side'].upper()} @{r['price']:.2f} | "
                                 f"odds {r['conf'] * 100:.0f}% | {mmss(r['minutes_left'])} left | "
                                 f"target {r['target']:,} now {r['spot']:,.2f} [{'LIVE' if live else 'paper'}]",
                                 key=f"{r['asset']}:{r['close_ts']}")
        if not live or db.has_window(self.c, "live", r["asset"], r["close_ts"]):
            return
        cfg = self.cfg
        n = int(min(cfg.max_contracts_per_trade, math.floor(cfg.max_dollars_per_trade / r["price"]), r["size"]))
        if n < 1:
            return
        cost = n * r["price"] + taker_fee(r["price"], n, fee_mult)
        ok, why = self.live_risk_ok(cost)
        if not ok:
            r["note"] += f" · live blocked: {why}"
            return
        try:
            fill = self.trader.buy(r["ticker"], r["side"], r["price"], n)
        except Exception as e:
            self.err(f"order failed {r['ticker']}: {e}")
            return
        if fill["filled"] >= 1:
            db.record_trade(self.c, mode="live", price=fill["fill_price"], contracts=fill["filled"],
                            fee=round(fill["fee"], 4), order_id=fill["order_id"], **base)
            self.alerts.send(f"LIVE FILLED: {r['asset']} {r['direction']} {r['side'].upper()} "
                             f"{fill['filled']:.0f} @ {fill['fill_price']:.2f}", force=True)
        else:
            r["note"] += " · live order not filled (price moved)"

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
                won = res == t["side"]
                if t["mode"] == "live":
                    self.alerts.send(f"LIVE {'WON' if won else 'LOST'}: {t['asset']} {t['direction'] or ''} "
                                     f"{t['side'].upper()} @{t['price']:.2f}", force=True)
            elif m.get("status") in ("finalized", "settled"):
                db.void(self.c, t["id"])
        self.check_loss_streak()

    def update_gates(self):
        for asset in [a for a in ASSET_ORDER if a in {v["asset"] for v in self.series.values()} | {"BTC"}]:
            passed, checks, s = gates.evaluate(db.settled(self.c, "paper", asset), self.cfg)
            was = self.gate_status.get(asset, {}).get("passed")
            self.gate_status[asset] = {"passed": passed, "checks": checks, "stats": s}
            if passed and was is False:
                self.alerts.send(f"{asset} PASSED all approval gates. "
                                 f"{'Live trading active.' if self.mode() == 'live' else 'Flip the switch to LIVE on the dashboard to trade it.'}",
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
        # one card per asset: the window closing soonest
        cards = {}
        live_rows = [r for r in rows if r["minutes_left"] is not None and r["minutes_left"] > 0.05]
        for r in sorted(live_rows, key=lambda r: r["minutes_left"]):
            cards.setdefault(r["asset"], r)
        for asset in sorted({v["asset"] for v in self.series.values()}):
            cards.setdefault(asset, {"asset": asset, "status": "NO MARKET",
                                     "note": "no 15-min window open (market hours?)",
                                     "spot": (prices.get(asset) or {}).get("price")})
        order = {a: i for i, a in enumerate(ASSET_ORDER)}
        for c in cards.values():
            c["label"] = ASSET_LABEL.get(c["asset"], c["asset"])
        db.put(self.c, "state", {
            "ts": time.time(),
            "mode": self.mode(),
            "demo": self.demo,
            "live_ready": self.trader is not None,
            "killed": self.killed(),
            "kill_reason": db.get(self.c, "kill_reason", ""),
            "balance": balance,
            "cards": sorted(cards.values(), key=lambda c: order.get(c["asset"], 9)),
            "gates": self.gate_status,
            "prices": {a: ({"price": p["price"], "age_sec": round(p["age_sec"])} if p else None)
                       for a, p in prices.items()},
            "errors": self.errors,
            "settings": {"entry_start": self.cfg.entry_start_min, "entry_end": self.cfg.entry_end_min,
                         "min_edge": self.cfg.min_edge, "mom_min_z": self.cfg.mom_min_z,
                         "max_dollars": self.cfg.max_dollars_per_trade, "max_daily_loss": self.cfg.max_daily_loss},
        })

    def cycle(self):
        self.refresh_trader()
        self.watch_mode()
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
        self.refresh_trader()
        print(f"[engine] mode={self.mode()} demo={self.demo} kalshi_connected={self.trader is not None}")
        self.alerts.send(f"Kalshi bot started ({self.mode()} mode).", force=True)
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
