"""All settings come from environment variables (the .env file on the server)."""
import os
import re
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env(name, default=None):
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _list(name, default):
    return [x.strip() for x in _env(name, default).split(",") if x.strip()]


def _bool(name, default):
    return str(_env(name, default)).lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # ---------- Kalshi ----------
    data_base: str = _env("KALSHI_DATA_BASE", "https://api.elections.kalshi.com/trade-api/v2")
    trade_base: str = _env("KALSHI_TRADE_BASE", "https://external-api.kalshi.com/trade-api/v2")
    demo: bool = _bool("KALSHI_DEMO", "false")      # true -> use demo exchange for orders (fake money)
    api_key_id: str = _env("KALSHI_API_KEY_ID", "")
    private_key_path: str = _env("KALSHI_PRIVATE_KEY_PATH", "")

    # ---------- What to scan ----------
    series: list = field(default_factory=lambda: _list("SERIES", "KXBTC15M,KXGOLD15M,KXWTI15M,KXNATGAS15M"))
    auto_discover: bool = _bool("AUTO_DISCOVER", "false")
    discover_categories: list = field(default_factory=lambda: _list("DISCOVER_CATEGORIES", "Crypto,Commodities"))
    discover_every_min: int = int(_env("DISCOVER_EVERY_MIN", "30"))

    # ---------- Signal rules ----------
    poll_seconds: int = int(_env("POLL_SECONDS", "10"))
    max_minutes_to_close: float = float(_env("MAX_MINUTES_TO_CLOSE", "16"))  # show the current 15-min window
    entry_start_min: float = float(_env("ENTRY_START_MIN", "7.5"))  # no trades until 7:30 or less remains
    entry_end_min: float = float(_env("ENTRY_END_MIN", "2.0"))      # no new trades under 2:00
    mom_lookback_min: int = int(_env("MOM_LOOKBACK_MIN", "5"))      # momentum measured over last 5 min
    mom_min_z: float = float(_env("MOM_MIN_Z", "0.5"))              # minimum momentum strength (std devs)
    mom_weight: float = float(_env("MOM_WEIGHT", "0.25"))           # how much momentum carries forward
    min_edge: float = float(_env("MIN_EDGE", "0.05"))           # $ per contract after fees + slippage
    min_ask_size: float = float(_env("MIN_ASK_SIZE", "5"))
    min_price: float = float(_env("MIN_PRICE", "0.05"))         # skip lottery tickets
    max_price: float = float(_env("MAX_PRICE", "0.90"))         # skip pennies-in-front-of-steamroller
    vol_lookback_min: int = int(_env("VOL_LOOKBACK_MIN", "60"))
    vol_mult: float = float(_env("VOL_MULT", "1.25"))            # fatten tails = more conservative
    slippage: float = float(_env("SLIPPAGE", "0.01"))
    max_price_age_sec: float = float(_env("MAX_PRICE_AGE_SEC", "60"))

    # ---------- Trading mode ----------
    # paper = simulate only. live = place real orders, but ONLY for assets that pass every approval gate.
    # This is only the starting value. The Paper/Live switch on the dashboard overrides it.
    trading_mode: str = _env("TRADING_MODE", "paper").lower()

    # ---------- Approval gates (must ALL pass, per asset, before live trading unlocks) ----------
    gate_min_trades: int = int(_env("GATE_MIN_TRADES", "150"))
    gate_min_days: float = float(_env("GATE_MIN_DAYS", "7"))
    gate_min_profit_factor: float = float(_env("GATE_MIN_PROFIT_FACTOR", "1.25"))
    gate_min_calibration_pts: float = float(_env("GATE_MIN_CALIBRATION_PTS", "3"))
    gate_max_drawdown: float = float(_env("GATE_MAX_DRAWDOWN", "15"))   # $ per 1-contract paper unit
    gate_recent_n: int = int(_env("GATE_RECENT_N", "50"))

    # ---------- Risk caps (live only) ----------
    max_contracts_per_trade: int = int(_env("MAX_CONTRACTS_PER_TRADE", "5"))
    max_dollars_per_trade: float = float(_env("MAX_DOLLARS_PER_TRADE", "10"))
    max_daily_loss: float = float(_env("MAX_DAILY_LOSS", "25"))
    max_open_positions: int = int(_env("MAX_OPEN_POSITIONS", "3"))
    max_trades_per_day: int = int(_env("MAX_TRADES_PER_DAY", "20"))
    live_loss_streak_kill: int = int(_env("LIVE_LOSS_STREAK_KILL", "6"))

    # ---------- Alerts ----------
    alert_methods: list = field(default_factory=lambda: _list("ALERT_METHOD", "console"))
    alert_cooldown_min: float = float(_env("ALERT_COOLDOWN_MIN", "10"))
    max_alerts_per_hour: int = int(_env("MAX_ALERTS_PER_HOUR", "12"))
    smtp_host: str = _env("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(_env("SMTP_PORT", "587"))
    smtp_user: str = _env("SMTP_USER", "")
    smtp_password: str = _env("SMTP_PASSWORD", "")
    sms_gateway_to: str = _env("SMS_GATEWAY_TO", "")
    twilio_sid: str = _env("TWILIO_ACCOUNT_SID", "")
    twilio_token: str = _env("TWILIO_AUTH_TOKEN", "")
    twilio_from: str = _env("TWILIO_FROM", "")
    twilio_to: str = _env("TWILIO_TO", "")

    # ---------- Dashboard ----------
    dash_user: str = _env("DASH_USER", "admin")
    dash_password: str = _env("DASH_PASSWORD", "")
    dash_port: int = int(_env("DASH_PORT", "8080"))

    # ---------- Storage ----------
    db_path: str = _env("DB_PATH", "data/trades.db")
    key_dir: str = _env("KEY_DIR", "keys")          # where the dashboard saves your Kalshi private key

    @property
    def order_base(self):
        return "https://external-api.demo.kalshi.co/trade-api/v2" if self.demo else self.trade_base


# Underlying price feed per asset. Patterns match "<series ticker> <series title>" in lower case.
ASSETS = {
    "BTC":    {"patterns": [r"\bbitcoin\b", r"\bkxbtc"],                           "feed": "coinbase", "symbol": "BTC-USD"},
    "GOLD":   {"patterns": [r"\bgold\b", r"\bkxgold"],                             "feed": "yahoo",    "symbol": "GC=F"},
    "WTI":    {"patterns": [r"\bwti\b", r"\bkxwti", r"\bcrude\b"],                 "feed": "yahoo",    "symbol": "CL=F"},
    "NATGAS": {"patterns": [r"\bnatural gas\b", r"\bkxnatgas", r"\bhenry hub\b"], "feed": "yahoo",    "symbol": "NG=F"},
}
ASSET_ORDER = ["BTC", "GOLD", "WTI", "NATGAS"]
ASSET_LABEL = {"BTC": "BTC", "GOLD": "GOLD", "WTI": "OIL (WTI)", "NATGAS": "NAT GAS"}

EXCLUDE_PATTERNS = [r"\bethereum\b", r"\bkxeth", r"\bsolana\b", r"\bxrp\b", r"\bdoge", r"\bbrent\b",
                    r"\bgasoline\b", r"\bgas prices?\b", r"\bdominance\b"]


def classify_asset(text: str):
    t = (text or "").lower()
    if any(re.search(p, t) for p in EXCLUDE_PATTERNS):
        return None
    for asset, spec in ASSETS.items():
        if any(re.search(p, t) for p in spec["patterns"]):
            return asset
    return None
