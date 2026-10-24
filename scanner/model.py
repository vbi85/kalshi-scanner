"""Fair-value model and edge math.

Fair value: probability the underlying finishes on the YES side of the strike at close,
assuming a zero-drift lognormal walk with volatility measured from the last hour of
1-minute returns (inflated by VOL_MULT to respect fat tails).
"""
import math


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def prob_above(spot, strike, sigma_1m, minutes):
    if strike <= 0 or spot <= 0:
        return None
    if minutes <= 0 or not sigma_1m:
        return 1.0 if spot >= strike else 0.0
    sd = sigma_1m * math.sqrt(minutes)
    d2 = (math.log(spot / strike) - 0.5 * sd * sd) / sd
    return norm_cdf(d2)


def fair_yes(market, spot, sigma_1m, minutes, vol_mult=1.25):
    """Probability the market resolves YES. None if the strike structure isn't supported."""
    st = market.get("strike_type")
    lo, hi = market.get("floor_strike"), market.get("cap_strike")
    sig = (sigma_1m or 0) * vol_mult
    try:
        if st in ("greater", "greater_or_equal") and lo is not None:
            p = prob_above(spot, float(lo), sig, minutes)
        elif st in ("less", "less_or_equal") and hi is not None:
            pa = prob_above(spot, float(hi), sig, minutes)
            p = None if pa is None else 1 - pa
        elif st == "between" and lo is not None and hi is not None:
            a, b = prob_above(spot, float(lo), sig, minutes), prob_above(spot, float(hi), sig, minutes)
            p = None if a is None or b is None else max(0.0, a - b)
        else:
            return None
    except (ValueError, ZeroDivisionError):
        return None
    if p is None:
        return None
    return min(0.98, max(0.02, p))


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
