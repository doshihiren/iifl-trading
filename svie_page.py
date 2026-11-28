"""SVIE Backtest page + API (Flask blueprint, registered in app.py).

  /iifl/svie                       page
  GET  /iifl/api/svie/status       stored candle coverage + depth-recorder count + defaults
  POST /iifl/api/svie/fetch        download 1-minute candles from IIFL (background job)
  POST /iifl/api/svie/run          run A / B / C (+ optional sweep) (background job)
  GET  /iifl/api/svie/jobs/<id>    poll a job
  GET  /iifl/api/svie/files/<name> download a report / CSV written by a run

Read-only with respect to the broker: nothing here places or cancels orders.
"""

import copy
import os
import re
import threading
import time
import uuid
from datetime import date, timedelta

from flask import Blueprint, jsonify, render_template, request, send_from_directory

import candles as candle_store
import config
import db
import svie_backtest as sb
import svie_data

bp = Blueprint("svie_page", __name__)
OUT_DIR = os.path.join(config.DATA_DIR, "svie")
MAX_TRADES = 1500
EQUITY_POINTS = 700
_jobs = {}
_lock = threading.Lock()
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}$")
_FILE = re.compile(r"^svie_[A-Za-z0-9_.\-]+\.(html|csv|json)$")


def _err(msg, code=400):
    return jsonify({"status": "error", "message": msg}), code


def _date(v, name):
    if not v or not _DATE.match(str(v)):
        raise ValueError(f"{name}: use YYYY-MM-DD")
    return str(v)


def _iid(v):
    v = str(v or "6792").strip()
    if not v.isdigit():
        raise ValueError("instrumentId must be a number (SBC = 6792)")
    return v


def params_from(raw):
    """Only keys that exist in DEFAULTS, cast to the default's type."""
    p = copy.deepcopy(sb.DEFAULTS)
    for k, v in (raw or {}).items():
        if k not in p or v in (None, ""):
            continue
        d = p[k]
        if isinstance(d, list):
            continue
        if isinstance(d, str):
            if not _TIME.match(str(v)):
                raise ValueError(f"{k}: use HH:MM")
            p[k] = str(v)
        elif isinstance(d, int):
            p[k] = int(float(v))
        else:
            p[k] = float(v)
        if not isinstance(d, str) and p[k] < 0:
            raise ValueError(f"{k} cannot be negative")
    if p["intraday_cap"] + p["positional_cap"] > p["capital"]:
        raise ValueError("Intraday + positional limits are more than the capital")
    if p["participation_pct"] <= 0 or p["participation_pct"] > 100:
        raise ValueError("Participation must be between 0 and 100 %")
    return p


def _start(kind, fn):
    job_id = uuid.uuid4().hex[:10]
    with _lock:
        for k in [k for k, v in _jobs.items() if time.time() - v["started"] > 3600]:
            _jobs.pop(k, None)
        if any(v["status"] == "running" and v["kind"] == kind for v in _jobs.values()):
            return _err(f"A {kind} job is already running – wait for it to finish", 409)
        _jobs[job_id] = {"kind": kind, "status": "running", "stage": "Starting…", "started": time.time(),
                         "result": None, "error": None}

    def stage(msg):
        _jobs[job_id]["stage"] = msg

    def work():
        try:
            _jobs[job_id].update(status="done", result=fn(stage))
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:300]
            if e.__class__.__name__ == "SessionExpired":
                msg = "IIFL session expired – click 'Login to IIFL' on the dashboard, then try again."
            _jobs[job_id].update(status="error", error=msg)

    threading.Thread(target=work, daemon=True, name=f"svie-{kind}-{job_id}").start()
    return jsonify({"status": "Ok", "result": {"job": job_id}})


# ------------------------------------------------------------------ routes
@bp.route("/iifl/svie")
def page():
    return render_template("svie.html")


@bp.route("/iifl/api/svie/status")
def status():
    try:
        iid = _iid(request.args.get("iid"))
        d_from = _date(request.args.get("from") or "2026-09-14", "from")
        d_to = _date(request.args.get("to") or date.today().isoformat(), "to")
    except ValueError as e:
        return _err(str(e))
    rows = candle_store.load(iid, d_from, d_to)
    rep = svie_data.quality_report(rows, date.fromisoformat(d_from), date.fromisoformat(d_to))
    depth = {"rows": 0, "today": 0, "last": None}
    try:
        r = db.row("SELECT COUNT(*) AS n, MAX(ts) AS last, SUM(substr(ts,1,10)=?) AS today "
                   "FROM quote_snapshots WHERE instrumentId=?", (date.today().isoformat(), iid))
        depth = {"rows": r["n"] or 0, "today": r["today"] or 0, "last": r["last"]}
    except Exception:  # table not created yet = recorder never started
        pass
    defaults = {k: v for k, v in sb.DEFAULTS.items() if not isinstance(v, list)}
    return jsonify({"status": "Ok", "result": {"coverage": rep, "depth": depth, "defaults": defaults,
                                               "limits_note": sb.LIMITS_NOTE}})


@bp.route("/iifl/api/svie/fetch", methods=["POST"])
def fetch():
    body = request.get_json(silent=True) or {}
    try:
        iid = _iid(body.get("iid"))
        d_from = date.fromisoformat(_date(body.get("from"), "from"))
        d_to = date.fromisoformat(_date(body.get("to"), "to"))
    except ValueError as e:
        return _err(str(e))
    if d_to < d_from:
        return _err("'to' is before 'from'")
    if (d_to - d_from).days > 400:
        return _err("Fetch at most ~13 months at a time")
    symbol = str(body.get("symbol") or "SBC").upper()[:30]

    def fn(stage):
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO analysis_symbols(instrumentId, symbol, tradingSymbol, exchange, "
                      "sync_from, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                      (iid, symbol, symbol, "NSEEQ", d_from.isoformat(), db.now_utc(), db.now_utc()))
        stage(f"Downloading {d_from} → {d_to} from IIFL…")
        res = candle_store.sync_range({"instrumentId": iid, "symbol": symbol, "exchange": "NSEEQ"}, d_from, d_to)
        rows = candle_store.load(iid, d_from.isoformat(), d_to.isoformat())
        svie_data.write_csv(os.path.join(OUT_DIR, f"svie_{symbol}_1m_{d_from}_{d_to}.csv"), rows)
        return {"fetch": res, "coverage": svie_data.quality_report(rows, d_from, d_to),
                "csv": f"svie_{symbol}_1m_{d_from}_{d_to}.csv"}
    return _start("fetch", fn)


def _thin(eq, n=EQUITY_POINTS):
    step = max(1, len(eq) // n)
    out = eq[::step]
    if eq and out[-1] != eq[-1]:
        out.append(eq[-1])
    return out


@bp.route("/iifl/api/svie/run", methods=["POST"])
def run():
    body = request.get_json(silent=True) or {}
    try:
        iid = _iid(body.get("iid"))
        d_from = _date(body.get("from"), "from")
        d_to = _date(body.get("to"), "to")
        warm = int(body.get("warmup_days") or 14)
        p = params_from(body.get("params"))
    except (ValueError, TypeError) as e:
        return _err(str(e))
    if d_to < d_from:
        return _err("'to' is before 'from'")
    data_from = (date.fromisoformat(d_from) - timedelta(days=max(0, min(warm, 60)))).isoformat()
    want_sweep = bool(body.get("sweep"))
    symbol = str(body.get("symbol") or "SBC").upper()[:30]

    def fn(stage):
        stage("Loading candles…")
        rows = candle_store.load(iid, data_from, d_to)
        if not rows:
            raise RuntimeError("No candles stored for this range – click 'Fetch from IIFL' first")
        stage("Running Buy & Hold, RSI + VWAP and Volume Intelligence…")
        pr, a, b, res = sb.run_all(rows, p, d_from, d_to)
        sw = sb.sweep(rows, p, d_from, d_to, progress=stage) if want_sweep else None
        meta = {"symbol": symbol, "from": pr.day[a], "to": pr.day[b - 1], "candles": b - a,
                "warmup_candles": a, "capital": p["capital"], "slippage_pct": p["slippage_pct"],
                "participation_pct": p["participation_pct"], "brokerage": p["brokerage_per_order"], "params": p}
        stage("Writing report files…")
        base = os.path.basename(sb.write_outputs(res, OUT_DIR, meta, sw))
        out = {"meta": meta, "sweep": sw, "limits_note": sb.LIMITS_NOTE,
               "files": {"report": base + "_report.html", "summary": base + "_summary.json",
                         **{f"{k}_trades": f"{base}_{k}_trades.csv" for k in res},
                         **{f"{k}_equity": f"{base}_{k}_equity.csv" for k in res}},
               "price": [(pr.c[i]["ts"], pr.c[i]["close"]) for i in range(a, b)][::max(1, (b - a) // EQUITY_POINTS)],
               "strategies": {}}
        for k, v in res.items():
            out["strategies"][k] = {"name": v["name"], "metrics": v["metrics"], "events": v["events"],
                                    "equity": _thin(v["equity"]), "trades": v["trades"][-MAX_TRADES:],
                                    "trades_total": len(v["trades"]),
                                    "exits": sb.exit_breakdown(v["trades"])}
        return out
    return _start("run", fn)


@bp.route("/iifl/api/svie/jobs/<job_id>")
def job(job_id):
    j = _jobs.get(job_id)
    if not j:
        return _err("Job not found (the web app may have restarted) – run it again", 404)
    return jsonify({"status": "Ok", "result": {k: j[k] for k in ("kind", "status", "stage", "result", "error")}})


@bp.route("/iifl/api/svie/files/<name>")
def files(name):
    if not _FILE.match(name):
        return _err("Bad file name", 404)
    return send_from_directory(OUT_DIR, name, as_attachment=not name.endswith(".html"))
