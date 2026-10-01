"""NSE trading calendar.

Built-in list: NSE equity trading holidays for 2026 (NSE circular). Add or
override dates in /root/iifl/data/holidays.json, e.g.
    {"holidays": ["2027-01-26", "2027-03-22"]}
If the calendar has no entries for the current year the bot refuses to
open new positions (exits still run) until the year is added.
"""

import json
import logging
import os
from datetime import datetime, time

import config

log = logging.getLogger("iifl.calendar")

NSE_HOLIDAYS = {
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25",
}

_cache = {"mtime": None, "dates": set()}


def _file_holidays():
    path = config.HOLIDAYS_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return set()
    if _cache["mtime"] != mtime:
        try:
            with open(path) as f:
                data = json.load(f)
            dates = data.get("holidays", data) if isinstance(data, dict) else data
            _cache["dates"] = {str(d)[:10] for d in dates}
        except Exception:
            log.exception("could not read holidays file %s", path)
            _cache["dates"] = set()
        _cache["mtime"] = mtime
    return _cache["dates"]


def holidays():
    return NSE_HOLIDAYS | _file_holidays()


def calendar_known(day):
    year = str(day.year)
    return any(d.startswith(year) for d in holidays())


def parse_hhmm(value):
    h, m = str(value).split(":")
    return time(int(h), int(m))


def is_trading_day(now):
    return now.weekday() < 5 and now.strftime("%Y-%m-%d") not in holidays()


def market_open(now):
    if not is_trading_day(now):
        return False
    t = now.time()
    return parse_hhmm(config.MARKET_OPEN) <= t < parse_hhmm(config.MARKET_CLOSE)


def market_status(now):
    if now.weekday() >= 5:
        return "CLOSED_WEEKEND"
    if now.strftime("%Y-%m-%d") in holidays():
        return "CLOSED_HOLIDAY"
    if market_open(now):
        return "OPEN"
    return "CLOSED"


def at_or_after(now, hhmm):
    return now.time() >= parse_hhmm(hhmm)


def parse_quote_time(value):
    """Best effort parse of a quote timestamp into an aware datetime (IST)."""
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    if value in (None, "", 0):
        return None
    try:
        num = float(value)
        if num > 1e12:
            num /= 1000.0
        if num > 1e9:
            return datetime.fromtimestamp(num, ist)
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S",
                "%d-%b-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=ist)
        except ValueError:
            continue
    return None
