"""Side-by-side strategy test.

Every variant paper-trades the SAME markets in the SAME windows, so results are directly
comparable. Variants never place real orders and never affect the approval gates —
the bot's main strategy (and live trading) is untouched.
To change what is tested, edit VARIANTS below and run kalshi-update.
"""
from .model import edges, fair_yes, taker_fee

# Change VARIANT_SET whenever the list changes: the dashboard then restarts the counts.
VARIANT_SET = "v2-2026-10-25"
VARIANTS = [
    {"id": "A", "name": "Current",       "what": "Your live settings (now momentum 0.8+, 40¢–75¢)", "over": {}},
    {"id": "B", "name": "Old rules",     "what": "Before tonight: momentum 0.5+, 5¢–90¢",
     "over": {"mom_min_z": 0.5, "min_price": 0.05, "max_price": 0.90}},
    {"id": "C", "name": "Edge 8¢",       "what": "Current + minimum edge 8¢", "over": {"min_edge": 0.08}},
    {"id": "D", "name": "Calmer odds",   "what": "Current + volatility ×1.5, momentum weight 0.15",
     "over": {"vol_mult": 1.5, "mom_weight": 0.15}},
    {"id": "E", "name": "No big spikes", "what": "Current + skip momentum above 2.0", "over": {"mom_max_z": 2.0}},
    # added Oct 27 (counts start then): a 20c+ "edge" usually means our price is off, not a real bargain
    {"id": "F", "name": "Skip huge edges", "what": "Current + skip edges above 20¢ (too good to be true)",
     "over": {"max_edge": 0.20}},
]

KEYS = ("min_edge", "max_price", "min_price", "vol_mult", "mom_weight", "mom_min_z", "mom_max_z",
        "min_ask_size", "slippage", "max_edge")


def params(cfg, variant):
    p = {k: getattr(cfg, k, None) for k in KEYS}
    p.update(variant["over"])
    return p


def decide(p, m, q, px, mom, mins, mins_eff, usd, fee_mult):
    """Return a trade dict if this variant would trade right now, else None.
    Caller has already checked: inside the 7:30–2:00 window, live price, momentum available."""
    if abs(mom["z"]) < p["mom_min_z"] or mom["r1"] * mom["drift_1m"] < 0:
        return None
    if p.get("mom_max_z") and abs(mom["z"]) > p["mom_max_z"]:
        return None
    drift = p["mom_weight"] * mom["drift_1m"]
    fair = fair_yes(m, px["price"], px["sigma_1m"], mins_eff, p["vol_mult"], drift)
    if fair is None:
        return None
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
