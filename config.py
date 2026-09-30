import os


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


IIFL_BASE_URL = os.getenv("IIFL_BASE_URL", "https://api.iiflcapital.com").rstrip("/")
LIVE_TRADING = env_bool("LIVE_TRADING", False)
IIFL_TRADING_IP_AUTHORIZED = env_bool("IIFL_TRADING_IP_AUTHORIZED", False)
