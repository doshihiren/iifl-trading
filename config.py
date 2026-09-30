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
BOT_DATA_FILE = os.getenv("BOT_DATA_FILE", "/root/iifl/data/bots.json")
