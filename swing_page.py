"""Swing Scanner page + API (Flask blueprint, registered in app.py).

Scans and backtests run in a background thread (all ~2000 NSE stocks can take
several seconds); the page polls /iifl/api/swing/jobs/<id>.
"""

import threading
import time
import uuid

from flask import Blueprint, jsonify, render_template, request

import swing
import swing_live

bp = Blueprint("swing_page", __name__)
_jobs = {}
_lock = threading.Lock()
MAX_TRADES = 3000


def _err(msg, code=400):
    return jsonify({"status": "error", "message": msg}), code


def _start(kind, fn):
    job_id = uuid.uuid4().hex[:10]
    with _lock:
        for k in [k for k, v in _jobs.items() if time.time() - v["started"] > 3600]:
            _jobs.pop(k, None)
        _jobs[job_id] = {"kind": kind, "status": "running", "stage": "Loading candles…", "done": 0, "total": 0,
                         "started": time.time(), "result": None, "error": None}

    def progress(stage):
        def cb(done, total):
            _jobs[job_id].update(stage=stage, done=done, total=total)
        return cb

    def work():
        try:
            _jobs[job_id].update(status="done", result=fn(progress))
        except Exception as e:  # noqa: BLE001
            _jobs[job_id].update(status="error", error=str(e)[:300])

    threading.Thread(target=work, daemon=True, name=f"swing-{kind}-{job_id}").start()
    return jsonify({"status": "Ok", "result": {"job": job_id}})


@bp.route("/iifl/swing")
def page():
    return render_template("swing.html")


@bp.route("/iifl/api/swing/status")
def status():
    return jsonify({"status": "Ok", "result": {**swing.data_status(), "defaults": swing.DEFAULTS}})


@bp.route("/iifl/api/swing/download", methods=["POST"])
def download():
    body = request.get_json(silent=True) or {}
    return jsonify({"status": "Ok", "result": swing.request_download(body.get("years", 3))})


@bp.route("/iifl/api/swing/scan", methods=["POST"])
def scan():
    body = request.get_json(silent=True) or {}
    try:
        p = swing.params(body.get("params"))
    except (ValueError, TypeError) as e:
        return _err(str(e))
    if not swing.data_status()["with_data"]:
        return _err("No daily data yet – click 'Download NSE daily data' first")

    def fn(progress):
        stocks = swing.cached_stocks(p, progress=progress("Loading candles…"))
        return swing.scan(stocks, p)
    return _start("scan", fn)


@bp.route("/iifl/api/swing/backtest", methods=["POST"])
def backtest():
    body = request.get_json(silent=True) or {}
    try:
        p = swing.params(body.get("params"))
    except (ValueError, TypeError) as e:
        return _err(str(e))
    d_from, d_to = body.get("from") or None, body.get("to") or None
    symbols = [s for s in str(body.get("symbols") or "").replace("\n", ",").split(",") if s.strip()]
    if body.get("universe") == "list" and not symbols:
        return _err("Type at least one symbol, e.g. HDFCBANK, RELIANCE")

    def fn(progress):
        if body.get("universe") == "list":
            stocks = swing.load_stocks(p, symbols=symbols, progress=progress("Loading candles…"))
            if not stocks:
                raise ValueError("No data for these symbols (check spelling, or download data first)")
        else:
            stocks = swing.cached_stocks(p, progress=progress("Loading candles…"))
        res = swing.backtest(stocks, p, d_from, d_to, progress=progress("Simulating…"))
        res["trades"] = res["trades"][-MAX_TRADES:]
        res["params"] = p
        res["stocks"] = len(stocks)
        return res
    return _start("backtest", fn)


@bp.route("/iifl/api/swing/jobs/<job_id>")
def job(job_id):
    j = _jobs.get(job_id)
    if not j:
        return _err("Job not found (the web app may have restarted) – run again", 404)
    return jsonify({"status": "Ok", "result": {k: v for k, v in j.items() if k != "started"}})


@bp.route("/iifl/api/swing/bot", methods=["GET"])
def bot_overview():
    return jsonify({"status": "Ok", "result": swing_live.overview()})


@bp.route("/iifl/api/swing/bot", methods=["POST"])
def bot_update():
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    try:
        p = swing.params(body.get("params")) if body.get("params") else None
        capital = float(body["capital"]) if body.get("capital") not in (None, "") else None
        maxpos = int(float(body["max_positions"])) if body.get("max_positions") not in (None, "") else None
    except (ValueError, TypeError) as e:
        return _err(str(e))
    mode = body.get("mode")
    if mode is not None and mode not in ("LIVE", "PAPER"):
        return _err("Mode must be LIVE or PAPER")
    if capital is not None and capital < 1000:
        return _err("Capital looks too small")
    if maxpos is not None and not 1 <= maxpos <= 50:
        return _err("Max stocks must be 1-50")
    keep = ("sma_len", "rsi_len", "entry_rsi", "exit_rsi", "max_hold", "sl_mode", "sl_value", "min_price", "min_value_cr", "brokerage")
    params = {k: p[k] for k in keep} if p else None
    if action == "start":
        cfg = swing_live.save_cfg(status="RUNNING", mode=mode, capital=capital, max_positions=maxpos, params=params)
        swing.db.add_event("SWING_START", f"Swing auto-trading started ({cfg['mode']}, ₹{cfg['capital']:,.0f}, "
                           f"{cfg['max_positions']} stocks)")
    elif action == "stop":
        cfg = swing_live.save_cfg(status="STOPPED")
        swing.db.add_event("SWING_STOP", "Swing auto-trading: new buys stopped; open positions still exit automatically")
    elif action == "save":
        cfg = swing_live.save_cfg(capital=capital, max_positions=maxpos, params=params,
                                  mode=mode if swing_live.get_cfg()["status"] != "RUNNING" else None)
    else:
        return _err("Unknown action")
    # Settings changed: rebuild tomorrow's plan from the latest close (if data is ready).
    try:
        from datetime import datetime
        swing_live.make_plan(datetime.now(swing_live.IST), force=True)
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"status": "Ok", "result": swing_live.overview()})
