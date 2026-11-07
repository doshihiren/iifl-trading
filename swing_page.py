"""Swing Scanner page + API (Flask blueprint, registered in app.py).

Scans and backtests run in a background thread (all ~2000 NSE stocks can take
several seconds); the page polls /iifl/api/swing/jobs/<id>.
"""

import threading
import time
import uuid

from flask import Blueprint, jsonify, render_template, request

import swing

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
