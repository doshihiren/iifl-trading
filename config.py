import os

from dotenv import load_dotenv

load_dotenv(os.getenv("IIFL_ENV_FILE", "/root/iifl/.env"))


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    return int(os.getenv(name, str(default)))


def env_float(name, default):
    return float(os.getenv(name, str(default)))


IIFL_BASE_URL = os.getenv("IIFL_BASE_URL", "https://api.iiflcapital.com").rstrip("/")

LIVE_TRADING = env_bool("LIVE_TRADING", False)
IIFL_TRADING_IP_AUTHORIZED = env_bool("IIFL_TRADING_IP_AUTHORIZED", False)

QUANTITY_PER_ENTRY = env_int("QUANTITY_PER_ENTRY", 1)
MAX_OPEN_POSITIONS = env_int("MAX_OPEN_POSITIONS", 10)
DEFAULT_PROFIT_TARGET_PERCENT = env_float("DEFAULT_PROFIT_TARGET_PERCENT", 1.0)
DEFAULT_QUANTITY = env_int("DEFAULT_QUANTITY", QUANTITY_PER_ENTRY)
DEFAULT_TIMEFRAME = os.getenv("DEFAULT_TIMEFRAME", "15m").strip().lower()
GLOBAL_MAX_CAPITAL = env_float("GLOBAL_MAX_CAPITAL", 100000.0)

BOT_POLL_SECONDS = env_int("BOT_POLL_SECONDS", 5)

# Legacy JSON store (read once for migration, then kept as a backup only).
BOT_DATA_FILE = os.getenv("BOT_DATA_FILE", "/root/iifl/data/bots.json")

# SQLite runtime database shared by the web app and the worker.
BOT_DB_FILE = os.getenv("BOT_DB_FILE", "/root/iifl/data/iifl.db")
DATA_DIR = os.path.dirname(BOT_DB_FILE)

# ---- LIVE safety limits -------------------------------------------------
# Largest quantity a single LIVE order may carry. Starts at 1 so the first
# live tests can only ever trade one share. Raise it in .env when ready.
LIVE_MAX_QTY_PER_ORDER = env_int("LIVE_MAX_QTY_PER_ORDER", 1)

# Send a short deterministic tag with every LIVE order so it can be matched
# in the IIFL order book after a crash. Set false if IIFL rejects the field.
SEND_ORDER_TAG = env_bool("SEND_ORDER_TAG", True)

# ---- Market timing (IST) ------------------------------------------------
MARKET_OPEN = os.getenv("MARKET_OPEN", "09:15")
MARKET_CLOSE = os.getenv("MARKET_CLOSE", "15:30")
INTRADAY_LAST_ENTRY = os.getenv("INTRADAY_LAST_ENTRY", "15:00")
INTRADAY_SQUAREOFF = os.getenv("INTRADAY_SQUAREOFF", "15:10")
HOLIDAYS_FILE = os.getenv("HOLIDAYS_FILE", os.path.join(DATA_DIR, "holidays.json"))

# A quote whose exchange timestamp is older than this is treated as stale.
QUOTE_MAX_AGE_SECONDS = env_int("QUOTE_MAX_AGE_SECONDS", 300)

# ---- Worker timing ------------------------------------------------------
SESSION_CHECK_SECONDS = env_int("SESSION_CHECK_SECONDS", 60)
RECON_SECONDS = env_int("RECON_SECONDS", 30)
UNKNOWN_ORDER_TIMEOUT_SECONDS = env_int("UNKNOWN_ORDER_TIMEOUT_SECONDS", 180)
EXIT_RETRY_SECONDS = env_int("EXIT_RETRY_SECONDS", 60)
EXIT_MAX_ATTEMPTS = env_int("EXIT_MAX_ATTEMPTS", 5)
WORKER_STALE_SECONDS = env_int("WORKER_STALE_SECONDS", 60)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
