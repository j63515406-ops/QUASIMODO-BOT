import os


def _s(n, d=""):
    return os.getenv(n, d).strip()


def _f(n, d):
    return float(_s(n, str(d)) or d)


def _i(n, d):
    return int(float(_s(n, str(d)) or d))


def _b(n, d):
    return _s(n, str(d)).lower() in ("1", "true", "yes", "si", "sí", "on")


def _l(n, d=""):
    return [x.strip().upper() for x in _s(n, d).split(",") if x.strip()]


BINGX_API_KEY = _s("BINGX_API_KEY")
BINGX_API_SECRET = _s("BINGX_API_SECRET")
BINGX_BASE_URL = _s("BINGX_BASE_URL", "https://open-api.bingx.com").rstrip("/")
HEDGE_MODE = _b("HEDGE_MODE", True)

TG_TOKEN = _s("TELEGRAM_BOT_TOKEN")
TG_CHAT = _s("TELEGRAM_CHAT_ID")

DRY_RUN = _b("DRY_RUN", True)

TIMEFRAME = _s("TIMEFRAME", "15m")
CANDLES = _i("CANDLES", 500)
MIN_QUOTE_VOLUME = _f("MIN_QUOTE_VOLUME", 300_000)
EXCLUDE_PREFIX = _l("EXCLUDE_PREFIX", "NCCO,NCSK,NCFX,NCSI")
WHITELIST = _l("WHITELIST")
BLACKLIST = _l("BLACKLIST")
WORKERS = _i("WORKERS", 8)
RATE_PER_SEC = _f("RATE_PER_SEC", 8)
CLOSE_DELAY_SECS = _i("CLOSE_DELAY_SECS", 6)
SIDE_FILTER = _s("SIDE_FILTER", "BOTH").upper()
NOTIFY_SETUPS = _b("NOTIFY_SETUPS", False)
NOTIFY_SUMMARY = _b("NOTIFY_SUMMARY", False)

RISK_PCT = _f("RISK_PCT", 1.0)
LEVERAGE = _i("LEVERAGE", 5)
MAX_OPEN = _i("MAX_OPEN", 3)
TP1_PCT = _f("TP1_PCT", 50)
MOVE_BE = _b("MOVE_BE", True)
MONITOR_SECS = _i("MONITOR_SECS", 20)
MAX_SLIPPAGE_R = _f("MAX_SLIPPAGE_R", 0.35)

USE_HTF = _b("USE_HTF", False)
HTF_TIMEFRAME = _s("HTF_TIMEFRAME", "4h")
HTF_LEN = _i("HTF_LEN", 50)

STATE_FILE = _s("STATE_FILE", "state.json")

STRATEGY = dict(
    piv_len=_i("PIV_LEN", 5),
    min_sweep=_f("MIN_SWEEP", 0.15),
    min_leg=_f("MIN_LEG", 1.5),
    zone_atr=_f("ZONE_ATR", 0.35),
    max_wait=_i("MAX_WAIT", 80),
    max_run=_i("MAX_RUN", 150),
    sig_mode=_s("SIG_MODE", "confirm").lower(),   # confirm | touch
    conf_win=_i("CONF_WIN", 5),
    sl_buf=_f("SL_BUF", 0.1),
    min_score=_i("MIN_SCORE", 3),
    st_len=_i("ST_LEN", 10),
    st_fac=_f("ST_FAC", 3.0),
    rsi_len=_i("RSI_LEN", 14),
    rsi_hi=_i("RSI_HI", 70),
    rsi_lo=_i("RSI_LO", 30),
    cvd_len=_i("CVD_LEN", 3),
    vol_mult=_f("VOL_MULT", 1.2),
)

TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
              "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400}
