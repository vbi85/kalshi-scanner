"""Approval gates. Live trading for an asset unlocks only when EVERY gate passes
on that asset's settled PAPER trades. Gates are re-checked every cycle, so an asset
that starts failing is automatically pushed back to paper."""
import time


def stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0, "pnl": 0.0, "wins": 0, "win_rate": 0.0, "avg_price": 0.0,
                "profit_factor": 0.0, "max_dd": 0.0, "days": 0.0, "recent_pnl": 0.0}
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for t in trades if t["status"] == "won")
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    eq = peak = dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    first = min(t["ts"] for t in trades)
    return {
        "n": n,
        "pnl": round(sum(pnls), 2),
        "wins": wins,
        "win_rate": wins / n,
        "avg_price": sum(t["price"] for t in trades) / n,
        "profit_factor": (gross_win / gross_loss) if gross_loss else (99.0 if gross_win else 0.0),
        "max_dd": round(dd, 2),
        "days": (time.time() - first) / 86400,
    }


def evaluate(trades, cfg, recent_n=None, accuracy=None):
    """Return (passed: bool, checks: list of dicts) for one asset's paper trades."""
    s = stats(trades)
    recent_n = recent_n or cfg.gate_recent_n
    recent_pnl = sum(t["pnl"] for t in trades[-recent_n:]) if trades else 0.0
    calib = (s["win_rate"] - s["avg_price"]) * 100   # do we win more often than the price implies?
    # Brier score (lower = better forecasts): does adding momentum beat the plain model?
    scored = [t for t in trades if t.get("fair") is not None and t.get("fair_base") is not None]

    def brier(key):
        out = []
        for t in scored:
            p_win = t[key] if t["side"] == "yes" else 1 - t[key]
            out.append((p_win - (1 if t["status"] == "won" else 0)) ** 2)
        return sum(out) / len(out) if out else None
    b_mom, b_base = brier("fair"), brier("fair_base")
    mom_ok = b_mom is not None and b_mom <= b_base
    checks = [
        {"name": "Enough trades", "value": f'{s["n"]}', "target": f'≥ {cfg.gate_min_trades}',
         "ok": s["n"] >= cfg.gate_min_trades},
        {"name": "Days tested", "value": f'{s["days"]:.1f}', "target": f'≥ {cfg.gate_min_days:g}',
         "ok": s["days"] >= cfg.gate_min_days},
        {"name": "Net profit after fees", "value": f'${s["pnl"]:.2f}', "target": "> $0",
         "ok": s["pnl"] > 0},
        {"name": "Profit factor", "value": f'{s["profit_factor"]:.2f}', "target": f'≥ {cfg.gate_min_profit_factor:g}',
         "ok": s["profit_factor"] >= cfg.gate_min_profit_factor},
        {"name": "Beats market odds", "value": f'{calib:+.1f} pts', "target": f'≥ +{cfg.gate_min_calibration_pts:g} pts',
         "ok": calib >= cfg.gate_min_calibration_pts},
        {"name": "Max drawdown", "value": f'${s["max_dd"]:.2f}', "target": f'≤ ${cfg.gate_max_drawdown:g}',
         "ok": s["n"] > 0 and s["max_dd"] <= cfg.gate_max_drawdown},
        {"name": "Momentum improves predictions",
         "value": "—" if b_mom is None else f"{b_mom:.3f}", "target": "—" if b_base is None else f"≤ {b_base:.3f}",
         "ok": mom_ok},
        {"name": f"Last {recent_n} trades profitable", "value": f'${recent_pnl:.2f}', "target": "> $0",
         "ok": recent_pnl > 0},
    ]
    if accuracy is not None:   # BTC: our price must match Kalshi's target price
        n, err = accuracy.get("n", 0), accuracy.get("mae_pct")
        checks.insert(0, {"name": "Price matches Kalshi",
                          "value": "—" if not n else f'±${accuracy["mae"]:.2f} ({n})',
                          "target": f'≤ {cfg.gate_max_price_err_pct:g}%, {cfg.gate_min_price_checks} checks',
                          "ok": n >= cfg.gate_min_price_checks and err is not None and err <= cfg.gate_max_price_err_pct})
    return all(c["ok"] for c in checks), checks, s
