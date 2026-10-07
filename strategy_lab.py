"""Strategy Lab page + API (Flask blueprint, registered in app.py).

Compares four strategies (A core ladder, B RSI bounce, C volume breakout,
D scalp) on the 1-minute candles downloaded through the Analysis tab, with a
"tune" (train) period and an untouched "check" (test) period. The optimizer
runs in a background thread; the web app has one Gunicorn worker, so the
in-memory job table is shared by all requests.
"""

import threading
import time
import uuid
from datetime import date, timedelta

from flask import Blueprint, jsonify, render_template, request

import candles as candle_store
import db
import lab_backtest as lb

bp = Blueprint("strategy_lab", __name__)

WARMUP_DAYS = 10          # calendar days loaded before the train start, only to warm up RSI / volume averages
MAX_TRADES_RETURNED = 500
_jobs = {}
_jobs_lock = threading.Lock()


def _err(msg, code=400):
    return jsonify({"status": "error", "message": msg}), code


def _f(v, default=None):
    if v in (None, "", "off"):
        return default
    return float(v)


def _i(v, default=0):
    if v in (None, ""):
        return default
    return int(float(v))


def _settings(body, sym):
    g = body.get("global") or {}
    charges_mode = g.get("charges_mode", "auto")
    return {
        "core_capital": _f(g.get("core_capital"), 500000),
        "active_capital": _f(g.get("active_capital"), 500000),
        "daily_loss_limit": _f(g.get("daily_loss_limit"), 0),
        "brokerage": _f(g.get("brokerage"), 0),
        "charges_pct": _f(g.get("charges_pct"), 0) if charges_mode == "custom" else None,
        "last_entry_time": g.get("last_entry_time") or "15:00",
        "square_off_time": g.get("square_off_time") or "15:10",
        "tick": float(sym["tick_size"] or 0.05),
    }


def _strategy_params(s, raw, g):
    risk = _f(raw.get("risk_per_trade"), 2500)
    common = {"shares": _i(raw.get("shares"), 1), "sizing": raw.get("sizing", "shares"),
              "risk_per_trade": risk, "max_positions": _i(raw.get("max_positions"), 1)}
    if s == "A":
        return {**common, "spacing_pct": _f(raw.get("spacing_pct"), 1), "target_pct": _f(raw.get("target_pct"), 2),
                "rsi_max": _f(raw.get("rsi_max")), "sl_pct": _f(raw.get("sl_pct"), 0),
                "core_qty": _i(raw.get("core_qty"), 0), "sizing": "shares"}
    if s == "B":
        return {**common, "rsi_buy": _f(raw.get("rsi_buy"), 30), "rsi_exit": _f(raw.get("rsi_exit")),
                "target_pct": _f(raw.get("target_pct"), 0.8), "sl_pct": _f(raw.get("sl_pct"), 0.7),
                "vwap_filter": bool(raw.get("vwap_filter")), "max_positions": 1}
    if s == "C":
        return {**common, "vol_mult": _f(raw.get("vol_mult"), 2), "r_multiple": _f(raw.get("r_multiple"), 2),
                "sl_mode": raw.get("sl_mode", "orb"), "sl_pct": _f(raw.get("sl_pct"), 1),
                "trail_pct": _f(raw.get("trail_pct"), 0), "max_trades_day": _i(raw.get("max_trades_day"), 1),
                "max_positions": 1}
    return {**common, "target_pct": _f(raw.get("target_pct"), 0.3), "sl_pct": _f(raw.get("sl_pct"), 0.3)}


def _load(body):
    sym = db.row("SELECT * FROM analysis_symbols WHERE instrumentId=?", (str(body.get("instrumentId", "")),))
    if not sym:
        raise ValueError("Choose a script that has data (download it on the Analysis page first)")
    tf = body.get("timeframe", "5m")
    if tf not in lb.TF_MIN:
        raise ValueError("Timeframe must be 1m, 5m or 15m")
    tr_from, tr_to = body.get("train_from"), body.get("train_to")
    te_from, te_to = body.get("test_from"), body.get("test_to")
    if not (tr_from and tr_to):
        raise ValueError("Choose the tune period dates")
    last = te_to or tr_to
    start = (date.fromisoformat(tr_from[:10]) - timedelta(days=WARMUP_DAYS)).isoformat()
    data = candle_store.load(sym["instrumentId"], start, last[:10])
    if not data:
        raise ValueError("No candles stored for these dates")
    g = body.get("global") or {}
    prep = lb.Prepared(data, tf, rsi_period=_i(g.get("rsi_period"), 14),
                       vol_lookback=_i(g.get("vol_lookback"), 20), orb_minutes=_i(g.get("orb_minutes"), 15))
    train = lb.index_range(prep, tr_from[:10], tr_to[:10])
    test = lb.index_range(prep, te_from[:10], te_to[:10]) if te_from and te_to else None
    if not train:
        raise ValueError("No candles in the tune period")
    return sym, prep, train, test


def _trim(res):
    res["trades"] = res["trades"][:MAX_TRADES_RETURNED]
    return res


@bp.route("/iifl/strategy-lab")
def page():
    return render_template("strategy_lab.html")


@bp.route("/iifl/api/lab/symbols", methods=["GET"])
def symbols():
    out = []
    for r in db.rows("SELECT instrumentId, symbol, tick_size FROM analysis_symbols ORDER BY symbol"):
        cov = candle_store.coverage(r["instrumentId"])
        if cov["n"]:
            out.append({**r, "first": cov["first"], "last": cov["last"], "days": cov["days"]})
    return jsonify({"status": "Ok", "result": out})


@bp.route("/iifl/api/lab/check", methods=["POST"])
def check():
    body = request.get_json(silent=True) or {}
    sym = db.row("SELECT * FROM analysis_symbols WHERE instrumentId=?", (str(body.get("instrumentId", "")),))
    if not sym:
        return _err("Unknown script")
    d_from, d_to = body.get("from"), body.get("to")
    data = candle_store.load(sym["instrumentId"], d_from or "2000-01-01", d_to or "2100-01-01")
    return jsonify({"status": "Ok", "result": lb.liquidity(data)})


@bp.route("/iifl/api/lab/run", methods=["POST"])
def run():
    body = request.get_json(silent=True) or {}
    try:
        sym, prep, train, test = _load(body)
        g = _settings(body, sym)
        chosen = [s for s in lb.STRATEGIES if (body.get("strategies") or {}).get(s, {}).get("enabled")]
        if not chosen:
            return _err("Tick at least one strategy")
        out = {}
        for s in chosen:
            p = _strategy_params(s, body["strategies"][s], g)
            out[s] = {"params": p,
                      "train": _trim(lb.simulate(prep, s, p, g, *train)),
                      "test": _trim(lb.simulate(prep, s, p, g, *test)) if test else None}
    except (ValueError, KeyError, TypeError) as e:
        return _err(str(e))
    return jsonify({"status": "Ok", "result": {"symbol": sym["symbol"], "timeframe": prep.tf, "strategies": out,
                                                "liquidity": lb.liquidity(prep.c[train[0]:(test or train)[1] + 1])}})


@bp.route("/iifl/api/lab/optimize", methods=["POST"])
def optimize():
    body = request.get_json(silent=True) or {}
    s = body.get("strategy")
    if s not in lb.STRATEGIES:
        return _err("Unknown strategy")
    try:
        sym, prep, train, test = _load(body)
        g = _settings(body, sym)
        base = _strategy_params(s, (body.get("strategies") or {}).get(s, {}), g)
    except (ValueError, KeyError, TypeError) as e:
        return _err(str(e))
    job_id = uuid.uuid4().hex[:10]
    with _jobs_lock:
        # keep the table small
        for k in [k for k, v in _jobs.items() if time.time() - v["started"] > 3600]:
            _jobs.pop(k, None)
        _jobs[job_id] = {"status": "running", "done": 0, "total": len(lb.combos(s)), "started": time.time(),
                         "strategy": s, "result": None, "error": None}

    def progress(done, total):
        _jobs[job_id].update(done=done, total=total)

    def work():
        try:
            res = lb.optimize(prep, s, base, g, train, test, progress=progress)
            _jobs[job_id].update(status="done", result=res)
        except Exception as e:  # noqa: BLE001
            _jobs[job_id].update(status="error", error=str(e)[:300])

    threading.Thread(target=work, daemon=True, name=f"lab-opt-{job_id}").start()
    return jsonify({"status": "Ok", "result": {"job": job_id}})


@bp.route("/iifl/api/lab/jobs/<job_id>", methods=["GET"])
def job(job_id):
    j = _jobs.get(job_id)
    if not j:
        return _err("Job not found (the web app may have restarted)", 404)
    return jsonify({"status": "Ok", "result": {k: v for k, v in j.items() if k != "started"}})
