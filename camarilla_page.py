"""Camarilla + RSI page (/iifl/camarilla). Analysis only – no orders.

Uses the 1-minute candles downloaded on the Analysis page (add NIFTY, or NIFTYBEES
if the index isn't in IIFL's equity contract list – the signals are % based).
"""

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, render_template, request

import camarilla_strategy as cs
import candles as candle_store
import db
import market_calendar as cal

IST = ZoneInfo("Asia/Kolkata")
bp = Blueprint("camarilla", __name__)
WARMUP_DAYS = 10            # calendar days before the start date, for RSI warm-up and the first day's pivots
MAX_ROWS = 600


def _err(msg, code=400):
    return jsonify({"status": "error", "message": msg}), code


def _params(body):
    def f(name, lo, hi):
        v = body.get(name)
        if v in (None, ""):
            return cs.DEFAULTS[name]
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name.replace('_', ' ')} must be a number")
        if not lo <= v <= hi:
            raise ValueError(f"{name.replace('_', ' ')} must be between {lo} and {hi}")
        return v

    p = {
        "timeframe_min": int(f("timeframe_min", 1, 60)),
        "rsi_period": int(f("rsi_period", 2, 50)),
        "upper_band": f("upper_band", 50, 95),
        "lower_band": f("lower_band", 5, 50),
        "level_tol_pct": f("level_tol_pct", 0, 1),
        "touch_lookback": int(f("touch_lookback", 0, 10)),
        "max_sl_pct": f("max_sl_pct", 0.01, 5),
        "min_rr": f("min_rr", 0, 5),
        "charges_pts": f("charges_pts", 0, 100),
    }
    p["trend_filter"] = str(body.get("trend_filter") or cs.DEFAULTS["trend_filter"])
    p["target_mode"] = str(body.get("target_mode") or cs.DEFAULTS["target_mode"])
    if p["trend_filter"] not in ("off", "daily", "ema"):
        raise ValueError("Trend filter must be off, daily or ema")
    if p["target_mode"] not in ("next", "mid"):
        raise ValueError("Target must be next or mid")
    if p["timeframe_min"] not in (5, 15, 30):
        raise ValueError("Timeframe must be 5, 15 or 30 minutes")
    for k in ("no_entry_after", "square_off"):
        v = (body.get(k) or cs.DEFAULTS[k]).strip()
        cal.parse_hhmm(v)
        p[k] = v
    return p


def _is_trading_day(d):
    return cal.is_trading_day(datetime(d.year, d.month, d.day, 12, tzinfo=IST))


def _symbol(body):
    sym = db.row("SELECT * FROM analysis_symbols WHERE instrumentId=?", (str(body.get("instrumentId", "")),))
    if not sym:
        raise ValueError("Choose a script that has data (add it on the Analysis page first)")
    return sym


@bp.route("/iifl/camarilla")
def page():
    return render_template("camarilla.html", dashboard_username=os.getenv("DASHBOARD_USERNAME", "admin"))


@bp.route("/iifl/api/camarilla/run", methods=["POST"])
def api_run():
    body = request.get_json(silent=True) or {}
    try:
        sym = _symbol(body)
        p = _params(body)
        cov = candle_store.coverage(sym["instrumentId"])
        if not cov["n"]:
            raise ValueError("No candles stored yet for this script – fetch data on the Analysis page")
        d_from = (body.get("from") or cov["first"][:10])[:10]
        d_to = (body.get("to") or cov["last"][:10])[:10]
        warm = max((date.fromisoformat(d_from) - timedelta(days=WARMUP_DAYS)).isoformat(), cov["first"][:10])
        data = candle_store.load(sym["instrumentId"], warm, d_to)
    except (TypeError, ValueError) as exc:
        return _err(str(exc) or "Check the settings")
    if not data or data[-1]["ts"][:10] < d_from:
        return _err(f"No candles between {d_from} and {d_to}")
    out = cs.backtest(data, p, trade_from=d_from, trade_to=d_to, is_trading_day=_is_trading_day)
    sigs = out["signals"]
    out["signals"] = [{k: v for k, v in s.items() if k != "bar"} for s in sigs[-MAX_ROWS:]]
    out["trades"] = out["trades"][-MAX_ROWS:]
    first_day = data[0]["ts"][:10]
    out["meta"] = {"symbol": sym["symbol"], "from": d_from, "to": data[-1]["ts"], "candles": len(data),
                   "note": ("Your start date is the first day of data, so that day has no pivots "
                            "(pivots need the previous day).") if first_day >= d_from else None}
    return jsonify({"status": "Ok", "result": out})


@bp.route("/iifl/api/camarilla/optimize", methods=["POST"])
def api_optimize():
    """Tune on the first 2/3 of the dates, check on the last 1/3 (or the dates given)."""
    body = request.get_json(silent=True) or {}
    try:
        sym = _symbol(body)
        p = _params(body)
        cov = candle_store.coverage(sym["instrumentId"])
        if not cov["n"]:
            raise ValueError("No candles stored yet for this script")
        d_from = (body.get("from") or cov["first"][:10])[:10]
        d_to = (body.get("to") or cov["last"][:10])[:10]
        warm = max((date.fromisoformat(d_from) - timedelta(days=WARMUP_DAYS)).isoformat(), cov["first"][:10])
        data = candle_store.load(sym["instrumentId"], warm, d_to)
    except (TypeError, ValueError) as exc:
        return _err(str(exc) or "Check the settings")
    days = sorted({c["ts"][:10] for c in data if d_from <= c["ts"][:10] <= d_to})
    if len(days) < 15:
        return _err(f"Need at least 15 trading days for a fair test (have {len(days)}) – fetch more data")
    cut = len(days) * 2 // 3
    tune, check = (days[0], days[cut - 1]), (days[cut], days[-1])
    out = cs.optimize(data, p, tune, check, is_trading_day=_is_trading_day)
    out["meta"] = {"symbol": sym["symbol"], "days": len(days)}
    return jsonify({"status": "Ok", "result": out})


def _completed(candles, now, tf):
    """Drop the 15m candle that is still forming."""
    t = now.hour * 60 + now.minute
    slot_start = cs.SESSION_START_MIN + ((t - cs.SESSION_START_MIN) // tf) * tf
    cut = f"{now.date().isoformat()} {cs._hhmm(slot_start)}"
    return [c for c in candles if c["ts"] < cut]


@bp.route("/iifl/api/camarilla/now", methods=["POST"])
def api_now():
    body = request.get_json(silent=True) or {}
    try:
        sym = _symbol(body)
        p = _params(body)
    except (TypeError, ValueError) as exc:
        return _err(str(exc) or "Check the settings")
    now = datetime.now(IST)
    stored = db.rows("SELECT ts, open, high, low, close, volume FROM candles WHERE instrumentId=? "
                     "ORDER BY ts DESC LIMIT 6000", (sym["instrumentId"],))[::-1]
    source, live_note = "stored data (last download)", None
    if cal.is_trading_day(now) and now.time() >= cal.parse_hhmm("09:16"):
        try:
            today = candle_store.iifl_fetch(sym["instrumentId"], sym["exchange"] or "NSEEQ", now.date(), now.date())
            today = [k for k in today if k["ts"][:10] == now.date().isoformat()]
            if today:
                stored = [k for k in stored if k["ts"] < today[0]["ts"]] + today
                stored = _completed(stored, now, p["timeframe_min"])
                source = "live IIFL 1-minute candles (today)"
        except Exception as exc:
            live_note = ("IIFL session expired – log in to IIFL on the dashboard for live signals"
                         if exc.__class__.__name__ == "SessionExpired" else f"Live candles unavailable: {str(exc)[:120]}")
    state = cs.current_state(stored, p)
    if not state:
        return _err("Not enough candles to calculate RSI")
    state.update(source=source, live_note=live_note, symbol=sym["symbol"], checked_at=now.strftime("%d %b %H:%M:%S"))
    if state.get("signal"):
        state["signal"] = {k: v for k, v in state["signal"].items() if k != "bar"}
    return jsonify({"status": "Ok", "result": state})
