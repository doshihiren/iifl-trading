"""RSI Analysis tab: page + API (Flask blueprint, registered in app.py).

Uses the 1-minute candles already downloaded through the Analysis tab.
The optimizer runs in a background thread (the web app has one Gunicorn
worker, so the in-memory job table is shared by all requests).
"""

import os
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, render_template, request

import candles as candle_store
import db
import market_calendar as cal
import rsi_backtest as rb

IST = ZoneInfo("Asia/Kolkata")
bp = Blueprint("rsi_analysis", __name__)

WARMUP_DAYS = 10            # calendar days loaded before the start date only to warm up RSI
_jobs = {}
_jobs_lock = threading.Lock()


def _err(msg, code=400):
    return jsonify({"status": "error", "message": msg}), code


def _opt_float(v):
    if v in (None, "", "off", "none"):
        return None
    return float(v)


def _params(body, sym):
    p = {
        "target_pct": float(body["target_pct"]),
        "shares": int(body["shares"]),
        "max_open": int(body["max_open"]),
        "capital_limit": float(body["capital_limit"]) if body.get("capital_limit") not in (None, "", 0, "0") else None,
        "batch_size": int(body.get("batch_size") or 0),
        "rebuy_dip_pct": float(body.get("rebuy_dip_pct") or 0),
        "charges_pct": float(body.get("charges_pct") or 0),
        "timeframe": body.get("timeframe", "15m"),
        "tick": float(sym["tick_size"] or 0.05),
        "rsi_period": int(body.get("rsi_period") or rb.DEFAULT_PERIOD),
        "max_entry_rsi": _opt_float(body.get("max_entry_rsi")),
        "min_entry_rsi": _opt_float(body.get("min_entry_rsi")),
        "hold_rsi": _opt_float(body.get("hold_rsi")),
        "exit_rsi": _opt_float(body.get("exit_rsi")),
        "max_stretch": float(body.get("max_stretch") or 0),
    }
    if p["target_pct"] <= 0 or p["shares"] <= 0 or p["max_open"] <= 0 or p["timeframe"] not in rb.TIMEFRAMES:
        raise ValueError("Target %, shares and max open entries must be above zero")
    if not 2 <= p["rsi_period"] <= 50:
        raise ValueError("RSI period must be between 2 and 50")
    if p["hold_rsi"] is not None and p["exit_rsi"] is not None and p["exit_rsi"] <= p["hold_rsi"]:
        raise ValueError("Exit RSI must be higher than Hold RSI")
    return p


def _holdings(body):
    text = (body.get("holdings") or "").strip()
    if not text:
        return None, []
    rows, errors = rb.parse_holdings(text)
    if not rows:
        return None, errors
    return {"rows": rows, "book_rsi": float(body.get("book_rsi") or 70), "parts": int(body.get("book_parts") or 1),
            "step": float(body.get("book_step") or 5), "min_profit_pct": float(body.get("min_profit_pct") or 0)}, errors


def _load(sym, body):
    """Candles for the chosen dates plus up to WARMUP_DAYS earlier for RSI warm-up."""
    cov = candle_store.coverage(sym["instrumentId"])
    if not cov["n"]:
        raise ValueError("No candles stored yet for this script – fetch data first")
    d_from = (body.get("from") or cov["first"][:10])[:10]
    d_to = (body.get("to") or cov["last"][:10])[:10]
    warm = (date.fromisoformat(d_from) - timedelta(days=WARMUP_DAYS)).isoformat()
    warm = max(warm, cov["first"][:10])
    data = candle_store.load(sym["instrumentId"], warm, d_to)
    if not data or data[-1]["ts"][:10] < d_from:
        raise ValueError(f"No candles between {d_from} and {d_to}")
    return data, d_from, d_to


def _symbol(body):
    sym = db.row("SELECT * FROM analysis_symbols WHERE instrumentId=?", (str(body.get("instrumentId", "")),))
    if not sym:
        raise ValueError("Choose a script that has data")
    return sym


@bp.route("/iifl/analysis-rsi")
def page():
    return render_template("analysis_rsi.html", dashboard_username=os.getenv("DASHBOARD_USERNAME", "admin"))


@bp.route("/iifl/api/analysis-rsi/run", methods=["POST"])
def api_run():
    body = request.get_json(silent=True) or {}
    try:
        sym = _symbol(body)
        params = _params(body, sym)
        holdings, h_errors = _holdings(body)
        data, d_from, d_to = _load(sym, body)
    except (KeyError, TypeError) as exc:
        return _err("Fill in target %, shares and max open entries")
    except ValueError as exc:
        return _err(str(exc))
    out = rb.run(data, params, trade_from=d_from, holdings=holdings)
    if not out["trade_from"]:
        return _err("Not enough candles to warm up RSI (need about 2 hours of data before trading starts)")
    out["meta"] = {"symbol": sym["symbol"], "from": out["trade_from"], "to": data[-1]["ts"], "candles": len(data),
                   "params": params, "holdings_errors": h_errors, "warmup_from": out["warmup_from"]}
    return jsonify({"status": "Ok", "result": out})


def _job_worker(job_id, data, d_from, params, holdings):
    job = _jobs[job_id]

    def progress(done, total, msg):
        job.update(done=done, total=total, message=msg)

    try:
        prep = rb.Prepared(data, d_from, period=params["rsi_period"])
        if prep.start >= prep.n:
            raise ValueError("Not enough candles to warm up RSI")
        result = rb.optimize(prep, params, progress=progress)
        if holdings:
            job["message"] = "Finding the best RSI level to sell your shares"
            result["holdings"] = rb.optimize_holdings(prep, holdings, params)[:6]
        result["meta"] = {"from": prep.ts[prep.start], "to": prep.ts[-1], "days": len(prep.days)}
        job.update(state="DONE", result=result, message="Done", done=job.get("total", 1))
    except Exception as exc:          # report any failure to the page instead of a silent thread death
        job.update(state="ERROR", message=str(exc)[:300])


@bp.route("/iifl/api/analysis-rsi/optimize", methods=["POST"])
def api_optimize():
    body = request.get_json(silent=True) or {}
    try:
        sym = _symbol(body)
        params = _params(body, sym)
        holdings, _ = _holdings(body)
        data, d_from, d_to = _load(sym, body)
    except (KeyError, TypeError):
        return _err("Fill in target %, shares and max open entries")
    except ValueError as exc:
        return _err(str(exc))
    with _jobs_lock:
        running = [j for j in _jobs.values() if j["state"] == "RUNNING"]
        if len(running) >= 2:
            return _err("Two searches are already running – wait for one to finish")
        for k in sorted(_jobs, key=lambda k: _jobs[k]["created"])[:-10]:
            _jobs.pop(k, None)
        job_id = uuid.uuid4().hex[:12]
        _jobs[job_id] = {"state": "RUNNING", "done": 0, "total": 1, "message": "Starting", "created": time.time(),
                         "symbol": sym["symbol"], "result": None}
    threading.Thread(target=_job_worker, args=(job_id, data, d_from, params, holdings), daemon=True).start()
    return jsonify({"status": "Ok", "result": {"job_id": job_id}})


@bp.route("/iifl/api/analysis-rsi/job/<job_id>", methods=["GET"])
def api_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return _err("Search not found (the web service may have restarted) – run it again", 404)
    return jsonify({"status": "Ok", "result": {k: v for k, v in job.items() if k != "created"}})


@bp.route("/iifl/api/analysis-rsi/now", methods=["POST"])
def api_now():
    """Current RSI and what the rules say, using stored candles + today's live 1m candles."""
    body = request.get_json(silent=True) or {}
    try:
        sym = _symbol(body)
        params = _params(body, sym)
        holdings, h_errors = _holdings(body)
    except (KeyError, TypeError):
        return _err("Fill in the settings first")
    except ValueError as exc:
        return _err(str(exc))
    now = datetime.now(IST)
    stored = db.rows("SELECT ts, open, high, low, close, volume FROM candles WHERE instrumentId=? "
                     "ORDER BY ts DESC LIMIT 1500", (sym["instrumentId"],))[::-1]
    source = "stored data (last download)"
    live_note = None
    if cal.is_trading_day(now) and now.time() >= cal.parse_hhmm("09:16"):
        try:
            today = candle_store.iifl_fetch(sym["instrumentId"], sym["exchange"] or "NSEEQ", now.date(), now.date())
            today = [k for k in today if k["ts"][:10] == now.date().isoformat()]
            if today:
                last_stored = stored[-1]["ts"] if stored else ""
                stored = [k for k in stored if k["ts"] < today[0]["ts"]] + today
                source = "live IIFL 1-minute candles (today)" if today[-1]["ts"] > last_stored else source
        except Exception as exc:
            live_note = ("IIFL session expired – log in to IIFL on the dashboard for live RSI"
                         if exc.__class__.__name__ == "SessionExpired" else f"Live candles unavailable: {str(exc)[:120]}")
    sig = rb.current_signal(stored, params, holdings, period=params["rsi_period"])
    if not sig:
        return _err("Not enough candles to calculate RSI")
    sig.update(source=source, live_note=live_note, symbol=sym["symbol"], holdings_errors=h_errors,
               checked_at=now.strftime("%d %b %H:%M:%S"))
    return jsonify({"status": "Ok", "result": sig})
