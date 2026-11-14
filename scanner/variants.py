"""Side-by-side strategy test.

Every variant paper-trades the SAME markets in the SAME windows, so results are directly
comparable. Variants never place real orders and never affect the approval gates —
the bot's main strategy (and live trading) is untouched.
To change what is tested, edit VARIANTS below and run kalshi-update.
"""
import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from .model import edges, fair_yes, taker_fee

TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Chicago"))

# Change VARIANT_SET whenever the list changes: the dashboard then restarts the counts.
VARIANT_SET = "v2-2026-10-25"
VARIANTS = [
    {"id": "A", "name": "Current", "what": "Your live settings", "over": {}},
    {"id": "C", "name": "Edge 8¢", "what": "Current + minimum edge 8¢", "over": {"min_edge": 0.08}},
    {"id": "D", "name": "Calmer odds", "what": "Current + volatility ×1.5, momentum weight 0.15",
     "over": {"vol_mult": 1.5, "mom_weight": 0.15}},
    {"id": "G", "name": "Calibrated odds", "what": "Current, but odds = 65% Kalshi price + 35% bot model",
     "over": {"calib_k": 0.35}},
    {"id": "H", "name": "Daytime only", "what": "Current, but only trades 8 AM-8 PM Central",
     "over": {"day_hours": (8, 20)}},
    {"id": "P", "name": "Panic fade", "what": "Bet AGAINST sharp moves (momentum 1.5+), cheap side 25¢–55¢",
     "over": {"fade_z": 1.5, "fade_min": 0.25, "fade_max": 0.55}},
    # ---- added Nov 14 from other builders' rules ----
    {"id": "L", "name": "Late conviction", "what": "Last 5→2 min: buy the 70¢–90¢ favorite when price is clearly past the target and the move wasn't choppy",
     "over": {"mode": "late", "win": (2.0, 5.0), "fav_min": 0.70, "fav_max": 0.90, "min_dist": 0.8, "max_retrace": 0.30}},
    {"id": "Q", "name": "Delta + calm", "what": "Current + price clearly past the target (0.5σ) + skip choppy moves",
     "over": {"min_dist": 0.5, "max_retrace": 0.30}},
    {"id": "R", "name": "Cool-down", "what": "Current + sit out one window after a loss",
     "over": {"cooldown": True}},
    {"id": "K", "name": "Cheap early", "what": "8+ min left: buy a side at 15¢–35¢ moving our way; sell at 75¢ or at 2:00 left",
     "over": {"mode": "cheap", "win": (8.0, 14.0), "cheap_min": 0.15, "cheap_max": 0.35, "tp": 0.75, "time_exit": 2.0}},
]

KEYS = ("min_edge", "max_price", "min_price", "vol_mult", "mom_weight", "mom_min_z", "mom_max_z",
        "min_ask_size", "slippage", "max_edge", "calib_k", "day_hours", "fade_z", "fade_min", "fade_max",
        "mode", "win", "fav_min", "fav_max", "min_dist", "max_retrace", "cooldown", "cheap_min", "cheap_max",
        "tp", "time_exit", "entry_start_min", "entry_end_min")


def params(cfg, variant):
    p = {k: getattr(cfg, k, None) for k in KEYS}
    p["win"] = (cfg.entry_end_min, cfg.entry_start_min)     # default trade window: same as the bot
    p.update(variant["over"])
    return p


def distance_sigma(m, px, mins_eff):
    """How far price is past Kalshi's target, in expected moves for the time left (0 = right at it)."""
    k = m.get("floor_strike") or m.get("cap_strike")
    sig = px.get("sigma_1m")
    if not k or not sig or not px.get("price"):
        return 0.0
    return abs(math.log(px["price"] / float(k))) / (sig * math.sqrt(max(mins_eff, 0.5)))


def retrace(px, mins):
    """Share of this window's biggest move that has since been given back (0 = clean trend, 1 = all gone)."""
    n = max(2, int(15 - mins) + 1)
    seg = (px.get("closes") or [])[-n:]
    if len(seg) < 3:
        return 0.0
    moves = [x - seg[0] for x in seg]
    peak = max(abs(x) for x in moves)
    return 0.0 if not peak else max(0.0, (peak - abs(moves[-1])) / peak)


def decide(p, m, q, px, mom, mins, mins_eff, usd, fee_mult):
    """Return a trade dict if this variant would trade right now, else None.
    Caller has already checked: a live price and momentum are available."""
    lo, hi = p.get("win") or (p.get("entry_end_min") or 2.0, p.get("entry_start_min") or 7.5)
    if not (lo <= mins <= hi):
        return None
    if p.get("cooldown") and p.get("_lost_last"):
        return None
    if p.get("mode") == "late":
        return _late(p, m, q, px, mom, mins, mins_eff, usd, fee_mult)
    if p.get("mode") == "cheap":
        return _cheap(p, m, q, px, mom, mins, mins_eff, usd, fee_mult)
    if p.get("day_hours"):
        start, end = p["day_hours"]
        if not (start <= (p.get("_now") or datetime.now(TZ)).hour < end):
            return None
    if p.get("fade_z"):
        return _fade(p, m, q, px, mom, mins, mins_eff, usd, fee_mult)
    if abs(mom["z"]) < p["mom_min_z"] or mom["r1"] * mom["drift_1m"] < 0:
        return None
    if p.get("mom_max_z") and abs(mom["z"]) > p["mom_max_z"]:
        return None
    if p.get("min_dist") and distance_sigma(m, px, mins_eff) < p["min_dist"]:
        return None
    if p.get("max_retrace") is not None and retrace(px, mins) > p["max_retrace"]:
        return None
    drift = p["mom_weight"] * mom["drift_1m"]
    fair = fair_yes(m, px["price"], px["sigma_1m"], mins_eff, p["vol_mult"], drift)
    if fair is None:
        return None
    if p.get("calib_k") and q.get("yes_ask") and q.get("no_ask"):
        mid = (q["yes_ask"] + 1 - q["no_ask"]) / 2               # Kalshi's own YES probability
        fair = mid + p["calib_k"] * (fair - mid)
    side = usd if mom["dir"] == "UP" else ("no" if usd == "yes" else "yes")
    e = edges(fair, q, fee_mult, p["slippage"]).get(side)
    price, size = q[f"{side}_ask"], q[f"{side}_ask_size"]
    if e is None or not price or not (p["min_price"] <= price <= p["max_price"]):
        return None
    if size < p["min_ask_size"] or e < p["min_edge"]:
        return None
    if p.get("max_edge") and e > p["max_edge"]:
        return None
    fill = round(min(0.99, price + p["slippage"]), 4)          # same paper fill rule as the main bot
    return {"side": side, "price": fill, "fee": taker_fee(fill, 1, fee_mult), "fair": round(fair, 4),
            "edge": round(e, 4), "direction": mom["dir"], "mom_z": round(mom["z"], 3),
            "minutes_left": round(mins, 2)}


def _fade(p, m, q, px, mom, mins, mins_eff, usd, fee_mult):
    """Panic fade: after a sharp move, buy the cheap side betting it snaps back. No model edge needed."""
    if abs(mom["z"]) < p["fade_z"]:
        return None
    with_move = usd if mom["dir"] == "UP" else ("no" if usd == "yes" else "yes")
    side = "no" if with_move == "yes" else "yes"                       # the opposite side
    price, size = q[f"{side}_ask"], q[f"{side}_ask_size"]
    if not price or not (p["fade_min"] <= price <= p["fade_max"]) or size < p["min_ask_size"]:
        return None
    fair = fair_yes(m, px["price"], px["sigma_1m"], mins_eff, p["vol_mult"], 0.0)    # plain model, for reference
    fill = round(min(0.99, price + p["slippage"]), 4)
    return {"side": side, "price": fill, "fee": taker_fee(fill, 1, fee_mult),
            "fair": None if fair is None else round(fair, 4), "edge": 0.0,
            "direction": "DOWN" if mom["dir"] == "UP" else "UP", "mom_z": round(mom["z"], 3),
            "minutes_left": round(mins, 2)}


def _late(p, m, q, px, mom, mins, mins_eff, usd, fee_mult):
    """Late conviction: near the end, back the clear favorite when price is well past the target."""
    if distance_sigma(m, px, mins_eff) < p["min_dist"] or retrace(px, mins) > p["max_retrace"]:
        return None
    fair = fair_yes(m, px["price"], px["sigma_1m"], mins_eff, p["vol_mult"], 0.0)
    if fair is None:
        return None
    side = "yes" if fair >= 0.5 else "no"                                # the side price is already on
    price, size = q[f"{side}_ask"], q[f"{side}_ask_size"]
    if not price or not (p["fav_min"] <= price <= p["fav_max"]) or size < p["min_ask_size"]:
        return None
    p_side = fair if side == "yes" else 1 - fair
    fill = round(min(0.99, price + p["slippage"]), 4)
    fee = taker_fee(fill, 1, fee_mult)
    if p_side < fill + fee:                                              # model must not disagree
        return None
    up = (side == usd)
    return {"side": side, "price": fill, "fee": fee, "fair": round(fair, 4), "edge": round(p_side - fill - fee, 4),
            "direction": "UP" if up else "DOWN", "mom_z": round(mom["z"], 3), "minutes_left": round(mins, 2)}


def _cheap(p, m, q, px, mom, mins, mins_eff, usd, fee_mult):
    """Cheap early: buy a long-shot side (15-35c) that momentum is moving toward; managed exit (tp / time)."""
    side = usd if mom["dir"] == "UP" else ("no" if usd == "yes" else "yes")
    if abs(mom["z"]) < 0.5:
        return None
    price, size = q[f"{side}_ask"], q[f"{side}_ask_size"]
    if not price or not (p["cheap_min"] <= price <= p["cheap_max"]) or size < p["min_ask_size"]:
        return None
    fill = round(min(0.99, price + p["slippage"]), 4)
    fair = fair_yes(m, px["price"], px["sigma_1m"], mins_eff, p["vol_mult"], p["mom_weight"] * mom["drift_1m"])
    return {"side": side, "price": fill, "fee": taker_fee(fill, 1, fee_mult),
            "fair": None if fair is None else round(fair, 4), "edge": 0.0,
            "direction": mom["dir"], "mom_z": round(mom["z"], 3), "minutes_left": round(mins, 2)}


def stats(rows):
    """rows: all shadow trades. Returns one summary per variant, best P&L first."""
    out = []
    for v in VARIANTS:
        mine = [r for r in rows if r["variant"] == v["id"]]
        done = [r for r in mine if r["status"] in ("won", "lost")]
        wins = sum(1 for r in done if r["status"] == "won")
        pnl = sum(r["pnl"] for r in done)
        out.append({"id": v["id"], "name": v["name"], "what": v["what"], "trades": len(done),
                    "open": sum(1 for r in mine if r["status"] == "open"),
                    "win_rate": round(100 * wins / len(done), 1) if done else None,
                    "pnl": round(pnl, 2), "per_trade": round(pnl / len(done), 3) if done else None})
    return sorted(out, key=lambda s: (s["trades"] == 0, -s["pnl"]))
