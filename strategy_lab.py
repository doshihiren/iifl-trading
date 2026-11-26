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
    if s == "E":
        mode = "momentum" if raw.get("mode") == "momentum" else "dip"
        return {**common, "mode": mode, "product": "DELIVERY" if raw.get("product") == "DELIVERY" else "INTRADAY",
                "rsi_level": _f(raw.get("rsi_level"), 60 if mode == "momentum" else 30),
                "rsi_exit": _f(raw.get("rsi_exit")), "vol_mult": _f(raw.get("vol_mult"), 2),
                "target_pct": _f(raw.get("target_pct"), 0), "sl_pct": _f(raw.get("sl_pct"), 0),
                "trail_pct": _f(raw.get("trail_pct"), 0), "max_trades_day": _i(raw.get("max_trades_day"), 3),
                "cooldown_bars": _i(raw.get("cooldown_bars"), 0)}
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
        _jobs[job_id] = {"status": "running", "done": 0, "total": len(lb.combos(s, base)), "started": time.time(),
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


# ===== Hedged Options Research (separate routes; NO broker order calls) =====
import csv as _option_csv
import io as _option_io
import math as _option_math
from datetime import datetime as _option_datetime, date as _option_date
from flask import render_template_string as _option_html

_OPTION_COLUMNS = {"timestamp","symbol","expiry","strike","type","bid","ask",
                   "lot_size","spot_close","ema20","ema50","rsi21","adx14","vwap"}

def _option_parse(data):
    if len(data)>15_000_000:
        raise ValueError("File exceeds 15 MB")
    reader=_option_csv.DictReader(_option_io.StringIO(data.decode("utf-8-sig")))
    if not reader.fieldnames or not _OPTION_COLUMNS.issubset(reader.fieldnames):
        raise ValueError("Missing CSV columns: "+", ".join(sorted(_OPTION_COLUMNS)))
    snapshots={}
    for n,row in enumerate(reader,2):
        if n>100002: raise ValueError("Maximum 100000 option quote rows")
        ts=_option_datetime.strptime(row["timestamp"],"%Y-%m-%d %H:%M")
        exp=_option_date.fromisoformat(row["expiry"])
        symbol=row["symbol"].strip().upper().replace(" ","")
        typ=row["type"].upper()
        if symbol not in ("NIFTY","BANKNIFTY") or typ not in ("PE","CE") or exp<ts.date():
            raise ValueError("Bad symbol, type or expiry on row "+str(n))
        time=ts.strftime("%H:%M")
        if not "09:15"<=time<="15:15" or (ts.minute-15)%15:
            raise ValueError("Expected 15-minute IST timestamps on row "+str(n))
        fields={k:float(row[k]) for k in
                ("strike","bid","ask","spot_close","ema20","ema50","rsi21","adx14","vwap")}
        if not all(_option_math.isfinite(v) for v in fields.values()):
            raise ValueError("Non-finite numeric field on row "+str(n))
        bid,ask=fields["bid"],fields["ask"]
        lot=int(row["lot_size"])
        if bid<0 or ask<=0 or ask<bid or lot<=0:
            raise ValueError("Invalid quote/lot size on row "+str(n))
        key=(symbol,ts.strftime("%Y-%m-%d %H:%M"))
        bucket=snapshots.setdefault(key,{"spot":None,"contracts":{}})
        spot={k:fields[k] for k in ("spot_close","ema20","ema50","rsi21","adx14","vwap")}
        if bucket["spot"] is not None and bucket["spot"]!=spot:
            raise ValueError("Different underlying indicators at same timestamp "+str(key))
        bucket["spot"]=spot
        contract=(row["expiry"],typ,fields["strike"])
        if contract in bucket["contracts"]:
            raise ValueError("Duplicate quote "+str(contract))
        bucket["contracts"][contract]={"bid":bid,"ask":ask,"lot":lot}
    if not snapshots: raise ValueError("CSV is empty")
    return snapshots

def _option_trend(s):
    if s["adx14"]<20:return None
    if s["ema20"]>s["ema50"] and s["spot_close"]>s["vwap"] and 55<=s["rsi21"]<=70:
        return "BULLISH"
    if s["ema20"]<s["ema50"] and s["spot_close"]<s["vwap"] and 30<=s["rsi21"]<=45:
        return "BEARISH"
    return None

def _option_strikes(chain,spot,date_str,bias,capital,risk_pct,fee):
    if bias not in ("BULLISH","BEARISH"):return []
    kind="PE" if bias=="BULLISH" else "CE"
    expiries=sorted({e for e,t,k in chain if t==kind
                     and 2<=(_option_date.fromisoformat(e)-_option_date.fromisoformat(date_str)).days<=35})
    if not expiries:return []
    expiry=expiries[0]
    rows=sorted((k,q) for (e,t,k),q in chain.items() if e==expiry and t==kind)
    candidates=[]
    for strike,short in rows:
        if (kind=="PE" and strike>=spot) or (kind=="CE" and strike<=spot):continue
        for hedge,long in rows:
            if (kind=="PE" and hedge>=strike) or (kind=="CE" and hedge<=strike):continue
            if short["lot"]!=long["lot"]:continue
            width=abs(strike-hedge)
            credit=short["bid"]-long["ask"]
            if width>1500 or not 0<credit<width:continue
            lot=short["lot"]
            max_loss=(width-credit)*lot
            if max_loss>capital*risk_pct/100:continue
            reward=credit*lot*.60-4*fee
            planned_risk=credit*lot*.25+4*fee
            if reward<=0 or planned_risk<=0 or reward/planned_risk<2:continue
            candidates.append({"expiry":expiry,"type":kind,"short_strike":strike,
                "hedge_strike":hedge,"lot":lot,"credit":round(credit,3),
                "max_theoretical_loss":round(max_loss,2),
                "planned_net_rr":round(reward/planned_risk,2)})
    return sorted(candidates,key=lambda p:(-p["planned_net_rr"],p["max_theoretical_loss"]))[:20]

def _option_exit_debit(chain,position):
    typ=position["type"];exp=position["expiry"]
    short=chain.get((exp,typ,position["short_strike"]))
    long=chain.get((exp,typ,position["hedge_strike"]))
    return max(0,short["ask"]-long["bid"]) if short and long else None

def _option_run(snapshots,symbol,capital,risk_pct,fee):
    if symbol not in ("NIFTY","BANKNIFTY") or not 10000<=capital<=1e8 or not .1<=risk_pct<=2 or not 0<=fee<=1000:
        raise ValueError("Invalid parameters")
    keys=sorted(k for k in snapshots if k[0]==symbol)
    if len(keys)<50: raise ValueError("Need at least 50 time snapshots for this index")
    trades=[];unpriced=[];position=None;entered=set();signal_count=0
    for i,key in enumerate(keys):
        ts=key[1];day,time=ts.split(" ")
        current=snapshots[key]
        if position and ts!=position["entry_ts"]:
            debit=_option_exit_debit(current["contracts"],position)
            if debit is None:
                if time>="15:15" or day!=position["entry_ts"][:10]:
                    unpriced.append(position);position=None
                continue
            credit=position["credit"]
            reason="TARGET" if debit<=credit*.40 else "STOP" if debit>=credit*1.25 else "EOD" if time>="15:15" else None
            if reason:
                pnl=round((credit-debit)*position["lot"]-4*fee,2)
                trades.append({**position,"exit_ts":ts,"exit_debit":round(debit,3),
                               "reason":reason,"net_pnl":pnl})
                position=None
        if position is None and day not in entered and i>0 and "10:00"<=time<="11:30":
            previous_key=keys[i-1]
            if previous_key[1][:10]==day:
                bias=_option_trend(snapshots[previous_key]["spot"])
                if bias:
                    signal_count+=1
                    strikes=_option_strikes(current["contracts"],current["spot"]["spot_close"],
                                            day,bias,capital,risk_pct,fee)
                    if strikes:
                        position={**strikes[0],"bias":bias,"entry_ts":ts}
                        entered.add(day)
    if position:unpriced.append(position)
    winners=sum(t["net_pnl"]>0 for t in trades)
    gains=sum(max(0,t["net_pnl"]) for t in trades)
    losses=-sum(min(0,t["net_pnl"]) for t in trades)
    last=keys[-1];last_spot=snapshots[last]["spot"]
    guidance=_option_strikes(snapshots[last]["contracts"],last_spot["spot_close"],
                             last[1][:10],_option_trend(last_spot),capital,risk_pct,fee)
    return {"summary":{"index":symbol,"snapshot_count":len(keys),"entry_signals":signal_count,
            "closed_trades":len(trades),"winners":winners,
            "win_rate_pct":round(100*winners/len(trades),2) if trades else None,
            "total_net_pnl":round(sum(t["net_pnl"] for t in trades),2),
            "profit_factor":round(gains/losses,3) if losses else None,
            "unpriced_exits":len(unpriced)},"trades":trades[:1000],
            "unpriced":unpriced[:100],"historical_strike_guidance":guidance,
            "as_of":last[1],"caveat":"Research-only snapshot fills; missing quotes and intra-bar fills not modeled."}

@bp.get("/iifl/hedged-options")
def hedged_options_page():
    return _option_html("""<!doctype html><html><head><meta name="viewport" content="width=device-width">
<title>IIFL Hedged Options</title><style>body{font:15px system-ui;background:#111e31;color:#e8efff;max-width:1050px;margin:30px auto;padding:15px}
input,select,button{padding:10px;margin:7px;background:#263d58;color:white;border:1px solid #68809d;border-radius:7px}
button{background:#1766bb;cursor:pointer}pre{white-space:pre-wrap;overflow:auto;background:#1a2c44;padding:18px;border-radius:10px}</style></head>
<body><a href="/iifl/" style="color:#9dbfff">Dashboard</a><h1>Hedged Options Research Bot</h1>
<p>NIFTY / BANKNIFTY • Credit spread • Planned 1:2 RR • PAPER & BACKTEST ONLY</p>
<p>CSV format: timestamp, symbol, expiry, strike, type, bid, ask, lot_size, spot_close, ema20, ema50, rsi21, adx14, vwap</p>
<p>Use genuine historical contract bid/ask snapshots. Never assume these are live executable prices.</p>
<form id="f"><input type="file" name="file" accept=".csv" required>
<select name="symbol"><option>NIFTY</option><option>BANKNIFTY</option></select>
<label>Capital ₹<input name="capital" type="number" value="500000" min="10000"></label>
<label>Risk %<input name="risk_pct" type="number" value="1" step=".1" min=".1" max="2"></label>
<label>Per-order fee ₹<input name="fee" type="number" value="20" min="0"></label><button>Run Backtest</button></form>
<pre id="out">Upload option snapshots to begin.</pre>
<script>document.getElementById("f").onsubmit=async e=>{e.preventDefault();
let out=document.getElementById("out");out.textContent="Calculating...";
try{let r=await fetch("/iifl/api/hedged-options/backtest",{method:"POST",body:new FormData(e.target)});
out.textContent=JSON.stringify(await r.json(),null,2)}catch(ex){out.textContent=ex.message}}</script>
</body></html>""")

@bp.post("/iifl/api/hedged-options/backtest")
def hedged_options_api():
    file=request.files.get("file")
    if not file or not file.filename.lower().endswith(".csv"):
        return jsonify(status="error",message="Upload a historical options CSV"),400
    try:
        snapshots=_option_parse(file.read(15_000_001))
        output=_option_run(snapshots,str(request.form.get("symbol","NIFTY")).upper(),
                           float(request.form.get("capital","500000")),
                           float(request.form.get("risk_pct","1")),
                           float(request.form.get("fee","20")))
        return jsonify(status="Ok",result=output)
    except (ValueError,UnicodeDecodeError) as exc:
        return jsonify(status="error",message=str(exc)),400
