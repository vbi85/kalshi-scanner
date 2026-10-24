"""Fair-value model and edge math.

Fair value: probability the underlying finishes on the YES side of the strike at close,
assuming a zero-drift lognormal walk with volatility measured from the last hour of
1-minute returns (inflated by VOL_MULT to respect fat tails).
"""
import math


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def prob_above(spot, strike, sigma_1m, minutes, drift_1m=0.0):
    """P(price at close >= strike). drift_1m = expected log move per minute (from momentum)."""
    if strike <= 0 or spot <= 0:
        return None
    if minutes <= 0 or not sigma_1m:
        return 1.0 if spot >= strike else 0.0
    sd = sigma_1m * math.sqrt(minutes)
    d2 = (math.log(spot / strike) + drift_1m * minutes - 0.5 * sd * sd) / sd
    return norm_cdf(d2)


def momentum(price, closes, sigma_1m, lookback=5):
    """Momentum over the last `lookback` minutes.
    Returns {"dir": "UP"/"DOWN", "z": move in standard deviations, "r1": last-minute move,
             "drift_1m": average log move per minute} or None if not enough data."""
    if not closes or len(closes) < lookback + 1 or not sigma_1m or not price:
        return None
    past = closes[-(lookback + 1)]
    last = closes[-2] if len(closes) >= 2 else closes[-1]
    r = math.log(price / past)
    r1 = math.log(price / last) if last else 0.0
    z = r / (sigma_1m * math.sqrt(lookback))
    return {"dir": "UP" if r >= 0 else "DOWN", "z": z, "r1": r1, "drift_1m": r / lookback}


def fair_yes(market, spot, sigma_1m, minutes, vol_mult=1.25, drift_1m=0.0):
    """Probability the market resolves YES. None if the strike structure isn't supported."""
    st = market.get("strike_type")
    lo, hi = market.get("floor_strike"), market.get("cap_strike")
    sig = (sigma_1m or 0) * vol_mult
    try:
        if st in ("greater", "greater_or_equal") and lo is not None:
            p = prob_above(spot, float(lo), sig, minutes, drift_1m)
        elif st in ("less", "less_or_equal") and hi is not None:
            pa = prob_above(spot, float(hi), sig, minutes, drift_1m)
            p = None if pa is None else 1 - pa
        elif st == "between" and lo is not None and hi is not None:
            a = prob_above(spot, float(lo), sig, minutes, drift_1m)
            b = prob_above(spot, float(hi), sig, minutes, drift_1m)
            p = None if a is None or b is None else max(0.0, a - b)
        else:
            return None
    except (ValueError, ZeroDivisionError):
        return None
    if p is None:
        return None
    return min(0.98, max(0.02, p))


def up_side(market):
    """Which contract side profits if price goes UP. None for range markets."""
    st = market.get("strike_type")
    if st in ("greater", "greater_or_equal"):
        return "yes"
    if st in ("less", "less_or_equal"):
        return "no"
    return None


def taker_fee(price, contracts=1, multiplier=1.0):
    """Kalshi taker fee: 0.07 x C x P x (1-P), rounded up to the cent per order."""
    raw = 0.07 * multiplier * contracts * price * (1 - price)
    return math.ceil(raw * 100 - 1e-9) / 100


def edges(fair, q, fee_mult=1.0, slippage=0.01):
    """Expected profit per 1 contract for buying YES and for buying NO at the ask."""
    out = {}
    if q["yes_ask"] and 0 < q["yes_ask"] < 1:
        out["yes"] = fair - q["yes_ask"] - taker_fee(q["yes_ask"], 1, fee_mult) - slippage
    if q["no_ask"] and 0 < q["no_ask"] < 1:
        out["no"] = (1 - fair) - q["no_ask"] - taker_fee(q["no_ask"], 1, fee_mult) - slippage
    return out
