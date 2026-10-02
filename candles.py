"""1-minute candle storage and sync for the Analysis tab.

The trading worker runs `SyncThread` in the background (never in the trading
loop). It
  * fetches the history a user requested (1 day / 1 week / 1 month ...), and
  * at 17:00 IST on every trading day adds that day's candles for scripts
    marked "live".

Candles come from POST /v1/marketdata/historicaldata, requested in small date
chunks so any per-request limit is respected. The response layout is parsed
defensively: rows may be [time, o, h, l, c, v, ...] lists, dicts, or strings.
"""

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import db
import market_calendar as cal

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("iifl.candles")

SESSION_START = "09:15"
SESSION_END = "15:29"          # last 1-minute candle of the day
LIVE_SYNC_AT = "17:00"
CHUNK_DAYS = 5                 # calendar days per request
REQUEST_PAUSE = 0.6            # seconds between IIFL calls


# ---------------------------------------------------------------- parsing
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_ist(value):
    """Candle time (epoch s/ms, ISO string, or 'DD-Mon-YYYY HH:MM:SS') -> aware IST datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        return datetime.fromtimestamp(n, timezone.utc).astimezone(IST)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)
    except ValueError:
        pass
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _row_to_candle(row):
    if isinstance(row, str):
        text = row.strip()
        if text.startswith("[") or text.startswith("{"):
            try:
                return _row_to_candle(json.loads(text))
            except ValueError:
                return None
        sep = "|" if "|" in text else ","
        row = text.split(sep)
    if isinstance(row, (list, tuple)):
        if len(row) < 5:
            return None
        t, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
        v = row[5] if len(row) > 5 else 0
    elif isinstance(row, dict):
        def pick(*keys):
            for k in keys:
                if row.get(k) not in (None, ""):
                    return row[k]
            return None
        t = pick("timestamp", "time", "dateTime", "datetime", "date", "candleTime", "epoch")
        o, h, l, c = pick("open", "o"), pick("high", "h"), pick("low", "l"), pick("close", "c")
        v = pick("volume", "v") or 0
    else:
        return None
    dt = _to_ist(t)
    o, h, l, c = _num(o), _num(h), _num(l), _num(c)
    if dt is None or None in (o, h, l, c) or min(o, h, l, c) <= 0:
        return None
    return {"ts": dt.strftime("%Y-%m-%d %H:%M"), "open": o, "high": h, "low": l, "close": c,
            "volume": int(_num(v) or 0)}


def _candle_rows(body):
    """Find the list of candle rows anywhere in an IIFL history response."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return []
    if isinstance(body, list):
        # Either a list of candles or a list of wrapper objects with "candles".
        out = []
        for item in body:
            if isinstance(item, dict) and any(k in item for k in ("candles", "Candles", "data")):
                out.extend(_candle_rows(item))
            else:
                out.append(item)
        return out
    if isinstance(body, dict):
        for key in ("candles", "Candles", "result", "data", "historicalData", "historicaldata"):
            if key in body and body[key] not in (None, ""):
                return _candle_rows(body[key])
    return []


def parse_candles(body):
    """IIFL response -> sorted, de-duplicated 1-minute candles inside market hours."""
    out = {}
    for row in _candle_rows(body):
        c = _row_to_candle(row)
        if c and SESSION_START <= c["ts"][11:] <= SESSION_END:
            out[c["ts"]] = c
    return [out[k] for k in sorted(out)]


# ---------------------------------------------------------------- fetching
class CandleFetchError(RuntimeError):
    pass


def iifl_fetch(instrument_id, exchange, d_from, d_to):
    """One historical request (inclusive dates). Returns parsed candles."""
    import broker
    import market_data
    status, body = market_data.historical_candles(
        instrument_id, exchange=exchange, timeframe="1m",
        from_date=d_from.strftime("%d-%b-%Y"), to_date=d_to.strftime("%d-%b-%Y"))
    if broker.is_session_error(status, body):
        raise broker.SessionExpired(broker.message_of(body) or f"HTTP {status}")
    if status != 200:
        raise CandleFetchError(f"HTTP {status}: {broker.message_of(body) or str(body)[:200]}")
    return parse_candles(body)


def trading_days(d_from, d_to):
    days, d = [], d_from
    while d <= d_to:
        if cal.is_trading_day(datetime(d.year, d.month, d.day, 12, tzinfo=IST)):
            days.append(d)
        d += timedelta(days=1)
    return days


def store(instrument_id, candles):
    if not candles:
        return 0
    with db.tx() as c:
        c.executemany(
            "INSERT INTO candles(instrumentId, ts, open, high, low, close, volume) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(instrumentId, ts) DO UPDATE SET open=excluded.open, high=excluded.high, "
            "low=excluded.low, close=excluded.close, volume=excluded.volume",
            [(str(instrument_id), k["ts"], k["open"], k["high"], k["low"], k["close"], k["volume"])
             for k in candles])
    return len(candles)


def _set(instrument_id, **fields):
    fields["updated_at"] = db.now_utc()
    cols = ", ".join(f"{k}=?" for k in fields)
    with db.tx() as c:
        c.execute(f"UPDATE analysis_symbols SET {cols} WHERE instrumentId=?", (*fields.values(), str(instrument_id)))


def sync_range(sym, d_from, d_to, fetch=iifl_fetch, pause=REQUEST_PAUSE):
    """Fetch [d_from, d_to] in chunks; on a chunk error retry it day by day."""
    iid, exchange = str(sym["instrumentId"]), sym["exchange"]
    days = trading_days(d_from, d_to)
    if not days:
        return {"stored": 0, "days": 0, "empty_days": [], "failed_days": []}
    stored, empty, failed = 0, [], []
    got_days = set()
    i = 0
    while i < len(days):
        chunk = [d for d in days[i:] if (d - days[i]).days < CHUNK_DAYS]
        i += len(chunk)
        _set(iid, sync_status=f"Fetching {chunk[0]:%d %b} – {chunk[-1]:%d %b}…")
        try:
            candles = fetch(iid, exchange, chunk[0], chunk[-1])
            stored += store(iid, candles)
            got_days |= {c["ts"][:10] for c in candles}
        except CandleFetchError as exc:
            log.warning("event=CANDLE_CHUNK_FAILED symbol=%s from=%s to=%s error=%s", sym["symbol"], chunk[0], chunk[-1], exc)
            for d in chunk:
                time.sleep(pause)
                try:
                    candles = fetch(iid, exchange, d, d)
                    stored += store(iid, candles)
                    got_days |= {c["ts"][:10] for c in candles}
                except CandleFetchError:
                    failed.append(d.isoformat())
        time.sleep(pause)
    for d in days:
        if d.isoformat() not in got_days and d.isoformat() not in failed:
            empty.append(d.isoformat())
    return {"stored": stored, "days": len(days), "empty_days": empty, "failed_days": failed}


def coverage(instrument_id):
    r = db.row("SELECT MIN(ts) AS first, MAX(ts) AS last, COUNT(*) AS n, COUNT(DISTINCT substr(ts,1,10)) AS days "
               "FROM candles WHERE instrumentId=?", (str(instrument_id),))
    return r or {"first": None, "last": None, "n": 0, "days": 0}


def _summary(result):
    msg = f"{result['stored']} candles over {result['days']} trading day(s)"
    if result["empty_days"]:
        msg += f"; IIFL returned no data for {len(result['empty_days'])} day(s)"
    if result["failed_days"]:
        msg += f"; {len(result['failed_days'])} day(s) failed"
    return msg


def last_complete_day(now):
    """Most recent trading day whose session has finished."""
    d = now.date()
    if not (cal.is_trading_day(now) and now.time() >= cal.parse_hhmm("15:31")):
        d -= timedelta(days=1)
    while not cal.is_trading_day(datetime(d.year, d.month, d.day, 12, tzinfo=IST)):
        d -= timedelta(days=1)
    return d


def run_requested(now, fetch=iifl_fetch, pause=REQUEST_PAUSE):
    """Process scripts the user asked to (re)fetch."""
    for sym in db.rows("SELECT * FROM analysis_symbols WHERE sync_requested=1"):
        d_from = date.fromisoformat(sym["sync_from"])
        d_to = last_complete_day(now)
        try:
            result = sync_range(sym, d_from, d_to, fetch=fetch, pause=pause)
            _set(sym["instrumentId"], sync_requested=0, last_synced_day=d_to.isoformat(),
                 sync_status=_summary(result), last_error=None if not result["failed_days"] else
                 "Some days failed: " + ", ".join(result["failed_days"][:5]))
            db.add_event("CANDLES_SYNCED", f"{sym['symbol']}: {_summary(result)}")
        except Exception as exc:
            _set(sym["instrumentId"], sync_status="Fetch failed", last_error=str(exc)[:300])
            log.warning("event=CANDLE_SYNC_FAILED symbol=%s error=%s", sym["symbol"], exc)
            if exc.__class__.__name__ == "SessionExpired":
                return


def run_live_daily(now, fetch=iifl_fetch, pause=REQUEST_PAUSE):
    """After 17:00 on a trading day, add the missing days for every live script."""
    if not cal.is_trading_day(now) or now.time() < cal.parse_hhmm(LIVE_SYNC_AT):
        return
    today = now.date()
    for sym in db.rows("SELECT * FROM analysis_symbols WHERE live=1 AND sync_requested=0"):
        last = date.fromisoformat(sym["last_synced_day"]) if sym["last_synced_day"] else today - timedelta(days=1)
        if last >= today:
            continue
        try:
            result = sync_range(sym, last + timedelta(days=1), today, fetch=fetch, pause=pause)
            _set(sym["instrumentId"], last_synced_day=today.isoformat(),
                 sync_status="Live: " + _summary(result) + f" (added {today:%d %b})", last_error=None)
            db.add_event("CANDLES_LIVE", f"{sym['symbol']}: added {today:%d %b} – {_summary(result)}")
        except Exception as exc:
            _set(sym["instrumentId"], last_error=str(exc)[:300])
            log.warning("event=CANDLE_LIVE_FAILED symbol=%s error=%s", sym["symbol"], exc)
            if exc.__class__.__name__ == "SessionExpired":
                return


class SyncThread(threading.Thread):
    """Background candle sync inside the worker process."""

    def __init__(self, interval=20):
        super().__init__(name="candle-sync", daemon=True)
        self.interval = interval
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                now = datetime.now(IST)
                run_requested(now)
                run_live_daily(now)
            except Exception:
                log.exception("event=CANDLE_THREAD_ERROR")
            self.stop_event.wait(self.interval)

    def stop(self):
        self.stop_event.set()


def load(instrument_id, d_from, d_to):
    return db.rows(
        "SELECT ts, open, high, low, close, volume FROM candles WHERE instrumentId=? AND ts>=? AND ts<=? ORDER BY ts",
        (str(instrument_id), f"{d_from} 00:00", f"{d_to} 23:59"))
