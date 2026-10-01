import os
import hashlib
import json
import requests

from flask import Flask, request, redirect, jsonify, render_template, session, url_for
from dotenv import load_dotenv

from networking import force_ipv6

load_dotenv("/root/iifl/.env")
force_ipv6()

from instruments import InstrumentLookupError, find_instrument
from market_data import MarketDataError, historical_candles, market_quote
from orders import LiveTradingDisabled, build_sbc_test_order, place_sbc_test_order
from bot_store import add_bot, get_bot, list_bots, remove_bot, list_trades, update_bot
import config
import db
import market_calendar
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

db.init_db()

app = Flask(__name__)


def _load_secret_key():
    key = os.getenv("FLASK_SECRET_KEY")
    if key:
        return key
    # Persist a generated key so logins survive restarts and Gunicorn workers agree.
    path = os.path.join(config.DATA_DIR, "flask_secret")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        key = os.urandom(32).hex()
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(key)
        os.chmod(path, 0o600)
        return key


app.secret_key = _load_secret_key()

DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")


def dashboard_authenticated():
    return session.get("dashboard_authenticated") is True


@app.before_request
def protect_iifl_dashboard():
    path = request.path

    if not path.startswith("/iifl"):
        return None

    public_paths = {
        "/iifl/auth/login",
        "/iifl/callback",
    }

    if path in public_paths:
        return None

    if dashboard_authenticated():
        return None

    if path == "/iifl/" or path == "/iifl":
        return redirect(url_for("dashboard_login"))

    return jsonify({
        "status": "error",
        "message": "Dashboard login required"
    }), 401


@app.route("/iifl/auth/login", methods=["GET", "POST"])
def dashboard_login():
    if dashboard_authenticated():
        return redirect("/iifl/")

    error = None

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if not DASHBOARD_PASSWORD:
            error = "Dashboard password is not configured on the server."
        elif username == DASHBOARD_USERNAME and password == DASHBOARD_PASSWORD:
            session.clear()
            session["dashboard_authenticated"] = True
            session.permanent = True
            return redirect("/iifl/")
        else:
            error = "Invalid username or password."

    return render_template("login.html", error=error)


@app.route("/iifl/auth/logout", methods=["POST"])
def dashboard_logout():
    session.clear()
    return redirect(url_for("dashboard_login"))


@app.route("/iifl/")
def dashboard():
    return render_template("dashboard.html", dashboard_username=DASHBOARD_USERNAME)


API_KEY = os.getenv("IIFL_API_KEY")
API_SECRET = os.getenv("IIFL_API_SECRET")
CLIENT_ID = os.getenv("IIFL_CLIENT_ID")
APP_KEY = os.getenv("IIFL_APP_KEY")

REDIRECT_URL = os.getenv(
    "IIFL_REDIRECT_URL",
    "https://aweliontech.com/iifl/callback"
)

IIFL_BASE_URL = os.getenv(
    "IIFL_BASE_URL",
    "https://api.iiflcapital.com"
)

SESSION_FILE = "/root/iifl/iifl_session.json"


def save_session(data):
    with open(SESSION_FILE, "w") as f:
        json.dump(data, f, indent=2)

    os.chmod(SESSION_FILE, 0o600)


def load_session():
    if not os.path.exists(SESSION_FILE):
        return None

    try:
        with open(SESSION_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


@app.route("/iifl/login")
def login():

    if not APP_KEY:
        return jsonify({
            "status": "error",
            "message": "IIFL_APP_KEY is not configured"
        }), 500

    login_url = (
        "https://markets.iiflcapital.com/"
        "?v=1"
        f"&appkey={APP_KEY}"
        f"&redirecturl={REDIRECT_URL}"
    )

    return redirect(login_url)


@app.route("/iifl/callback")
def callback():

    auth_code = request.args.get("authCode") or request.args.get("authcode")
    callback_client_id = request.args.get("clientId") or request.args.get("clientid")

    if not auth_code:
        return jsonify({
            "status": "error",
            "message": "authCode was not received",
            "parameters": list(request.args.keys())
        }), 400

    if not callback_client_id:
        return jsonify({
            "status": "error",
            "message": "clientId was not received"
        }), 400

    if not API_SECRET:
        return jsonify({
            "status": "error",
            "message": "IIFL_API_SECRET is not configured"
        }), 500

    # IIFL documentation:
    # SHA256(clientId + authCode + apiSecret)

    raw = (
        callback_client_id
        + auth_code
        + API_SECRET
    )

    checksum = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()

    # Exchange checksum for userSession
    url = f"{IIFL_BASE_URL}/v1/getusersession"

    payload = {
        "checkSum": checksum
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=20
        )

        try:
            result = response.json()
        except Exception:
            result = {
                "status": "error",
                "http_status": response.status_code,
                "raw_response": response.text
            }

        if response.status_code != 200:
            return jsonify({
                "status": "error",
                "message": "IIFL session request failed",
                "http_status": response.status_code,
                "response": result
            }), 502

        user_session = result.get("userSession")

        if result.get("status") != "Ok" or not user_session:
            return jsonify({
                "status": "error",
                "message": "IIFL did not return a userSession",
                "response": result
            }), 401

        save_session({
            "client_id": callback_client_id,
            "user_session": user_session
        })
        db.set_runtime("broker_session", {
            "state": "CONNECTED",
            "detail": "new login",
            "checked_at": db.now_utc(),
        })
        db.add_event("BROKER_LOGIN", "New IIFL session saved from dashboard login")

        return """
        <html>
        <head>
            <title>IIFL Login Successful</title>
        </head>
        <body>
            <h2>IIFL authentication successful</h2>
            <p>Trading API session created successfully.</p>
            <p>You can close this window.</p>
        </body>
        </html>
        """

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": "Exception while creating IIFL session",
            "error": str(e)
        }), 500


@app.route("/iifl/status")
def status():

    session = load_session()

    return jsonify({
        "broker": "IIFL",
        "callback_configured": bool(REDIRECT_URL),
        "app_key_configured": bool(APP_KEY),
        "api_key_configured": bool(API_KEY),
        "session_file_exists": bool(session),
        "session_available": bool(
            session and session.get("user_session")
        )
    })


@app.route("/iifl/profile")
def profile():

    session = load_session()

    if not session or not session.get("user_session"):
        return jsonify({
            "status": "error",
            "message": "IIFL session not available. Login first."
        }), 401

    token = session["user_session"]

    try:

        response = requests.get(
            f"{IIFL_BASE_URL}/v1/profile",
            headers={
                "Authorization": f"Bearer {token}"
            },
            timeout=20
        )

        return (
            response.text,
            response.status_code,
            {
                "Content-Type": "application/json"
            }
        )

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route("/iifl/limits")
def limits():

    session = load_session()

    if not session or not session.get("user_session"):
        return jsonify({
            "status": "error",
            "message": "IIFL session not available. Login first."
        }), 401

    token = session["user_session"]

    try:

        response = requests.get(
            f"{IIFL_BASE_URL}/v1/limits",
            headers={
                "Authorization": f"Bearer {token}"
            },
            timeout=20
        )

        return (
            response.text,
            response.status_code,
            {
                "Content-Type": "application/json"
            }
        )

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route("/iifl/instruments/<symbol>")
def instrument_lookup(symbol):
    try:
        instrument = find_instrument(symbol, exchange=request.args.get("exchange", "NSEEQ"))
        return jsonify({
            "status": "Ok",
            "message": "Success",
            "result": instrument
        })
    except InstrumentLookupError as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 404
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 502


@app.route("/iifl/market/quote")
def market_quote_route():
    symbol = request.args.get("symbol", "SBC")
    exchange = request.args.get("exchange", "NSEEQ")

    try:
        instrument = find_instrument(symbol, exchange=exchange)
        http_status, result = market_quote(
            instrument_id=instrument["instrumentId"],
            exchange=exchange,
        )
        return jsonify({
            "status": "forwarded",
            "symbol": symbol.upper(),
            "instrument": {
                "instrumentId": instrument.get("instrumentId"),
                "tradingSymbol": instrument.get("tradingSymbol"),
                "exchange": instrument.get("exchange"),
                "tickSize": _tick_size(instrument),
            },
            "iifl_http_status": http_status,
            "iifl_response": result,
        }), http_status
    except InstrumentLookupError as e:
        return jsonify({"status": "error", "message": str(e)}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/iifl/market/candles")
def market_candles_route():
    symbol = request.args.get("symbol", "SBC")
    exchange = request.args.get("exchange", "NSEEQ")
    timeframe = request.args.get("timeframe", "1m")
    from_date = request.args.get("from")
    to_date = request.args.get("to")

    if not from_date or not to_date:
        return jsonify({
            "status": "error",
            "message": "Both from and to are required in DD-Mon-YYYY format."
        }), 400

    try:
        instrument = find_instrument(symbol, exchange=exchange)
        http_status, result = historical_candles(
            instrument_id=instrument["instrumentId"],
            exchange=exchange,
            timeframe=timeframe,
            from_date=from_date,
            to_date=to_date,
        )
        return jsonify({
            "status": "forwarded",
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "instrument": {
                "instrumentId": instrument.get("instrumentId"),
                "tradingSymbol": instrument.get("tradingSymbol"),
                "exchange": instrument.get("exchange"),
            },
            "iifl_http_status": http_status,
            "iifl_response": result,
        }), http_status
    except InstrumentLookupError as e:
        return jsonify({"status": "error", "message": str(e)}), 404
    except MarketDataError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/iifl/orders/test/sbc", methods=["GET"])
def sbc_test_order_preview():
    return jsonify({
        "status": "Ok",
        "message": "Preview only. No order has been sent.",
        "result": build_sbc_test_order()
    })


@app.route("/iifl/orders/test/sbc", methods=["POST"])
def sbc_test_order_execute():
    body = request.get_json(silent=True) or {}

    if body.get("confirm") != "PLACE_SBC_1_SHARE":
        return jsonify({
            "status": "error",
            "message": "Explicit confirmation required.",
            "required_confirm": "PLACE_SBC_1_SHARE"
        }), 400

    try:
        http_status, result = place_sbc_test_order()
        return jsonify({
            "status": "forwarded",
            "iifl_http_status": http_status,
            "iifl_response": result
        }), http_status
    except LiveTradingDisabled as e:
        return jsonify({
            "status": "blocked",
            "message": str(e)
        }), 403
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


def _tick_size(instrument):
    for key in ("tickSize", "tick_size", "ticksize"):
        value = instrument.get(key)
        try:
            if value not in (None, "") and float(value) > 0:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.05


ACTIVE_ORDER_STATES = ("SUBMITTING", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN")


def _bot_summary(bot):
    lots = db.rows(
        "SELECT quantity, entry_price FROM trades WHERE bot_id=? AND status IN ('OPEN','EXIT_SUBMITTED')",
        (bot["bot_id"],))
    pending = db.scalar(
        "SELECT COUNT(*) FROM orders WHERE bot_id=? AND state IN (?,?,?,?)",
        (bot["bot_id"], *ACTIVE_ORDER_STATES))
    bot = dict(bot)
    bot["open_positions"] = len(lots)
    bot["open_qty"] = sum(int(l["quantity"]) for l in lots)
    bot["deployed_capital"] = round(sum(float(l["entry_price"]) * int(l["quantity"]) for l in lots), 2)
    bot["pending_orders"] = int(pending or 0)
    return bot


@app.route("/iifl/api/bots", methods=["GET"])
def api_bots_list():
    return jsonify({
        "status": "Ok",
        "result": [_bot_summary(b) for b in list_bots()]
    })


@app.route("/iifl/api/bots", methods=["POST"])
def api_bots_save():
    body = request.get_json(silent=True) or {}

    symbol = str(body.get("symbol", "")).strip().upper()
    if not symbol:
        return jsonify({"status": "error", "message": "symbol is required"}), 400

    try:
        instrument = find_instrument(symbol, exchange="NSEEQ")
    except InstrumentLookupError as e:
        return jsonify({"status": "error", "message": str(e)}), 404

    try:
        quantity = int(body.get("qty", 1))
        target = float(body.get("target", 1.0))
        maxpos = int(body.get("maxpos", 10))
        capital = float(body.get("capital", 100000))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid numeric bot settings"}), 400

    timeframe = str(body.get("timeframe", "15m")).lower()
    if timeframe not in {"1m", "5m", "15m"}:
        return jsonify({"status": "error", "message": "timeframe must be 1m, 5m or 15m"}), 400

    mode = str(body.get("mode", "PAPER")).upper()
    if mode not in {"PAPER", "LIVE"}:
        return jsonify({"status": "error", "message": "mode must be PAPER or LIVE"}), 400

    product = str(body.get("product", "DELIVERY")).upper()
    if product not in {"DELIVERY", "INTRADAY"}:
        return jsonify({"status": "error", "message": "Invalid product"}), 400

    if quantity <= 0 or target <= 0 or maxpos <= 0 or capital <= 0:
        return jsonify({"status": "error", "message": "Bot settings must be greater than zero"}), 400

    if mode == "LIVE" and quantity > config.LIVE_MAX_QTY_PER_ORDER:
        return jsonify({
            "status": "error",
            "message": f"LIVE qty is limited to {config.LIVE_MAX_QTY_PER_ORDER} per order "
                       f"(LIVE_MAX_QTY_PER_ORDER in .env)"
        }), 400

    saved = add_bot({
        "symbol": symbol,
        "tradingSymbol": instrument.get("tradingSymbol"),
        "instrumentId": str(instrument.get("instrumentId")),
        "exchange": instrument.get("exchange", "NSEEQ"),
        "tick_size": _tick_size(instrument),
        "mode": mode,
        "qty": quantity,
        "target": target,
        "timeframe": timeframe,
        "product": product,
        "maxpos": maxpos,
        "capital": capital,
        "status": "READY"
    })
    db.add_event("BOT_ADDED", f"{mode} bot {symbol} {timeframe} qty {quantity} target {target}%",
                 bot_id=saved["bot_id"])

    return jsonify({
        "status": "Ok",
        "message": "Bot saved",
        "result": saved
    })


@app.route("/iifl/api/bots/<bot_id>", methods=["DELETE"])
def api_bots_delete(bot_id):
    bot = get_bot(bot_id)
    if not bot:
        return jsonify({"status": "error", "message": "Bot not found"}), 404
    summary = _bot_summary(bot)
    if bot["mode"] == "LIVE" and (summary["open_positions"] or summary["pending_orders"]):
        return jsonify({
            "status": "error",
            "message": "This LIVE bot still has open positions or pending orders. "
                       "Use 'Exit & Stop' first, then remove it."
        }), 409
    removed = remove_bot(bot_id)
    db.add_event("BOT_REMOVED", f"Bot {bot['symbol']} removed", bot_id=bot_id)
    return jsonify({
        "status": "Ok",
        "removed": removed
    })


@app.route("/iifl/api/bots/<bot_id>/start", methods=["POST"])
def api_bot_start(bot_id):
    bot = update_bot(bot_id, status="RUNNING", last_error=None)
    if not bot:
        return jsonify({"status": "error", "message": "Bot not found"}), 404
    db.add_event("BOT_START", f"{bot['mode']} bot {bot['symbol']} started", bot_id=bot_id)
    return jsonify({"status": "Ok", "result": bot})


@app.route("/iifl/api/bots/<bot_id>/stop", methods=["POST"])
def api_bot_stop(bot_id):
    """STOP_NEW_ENTRIES: no new BUYs; open positions keep being sold at target."""
    bot = update_bot(bot_id, status="STOPPED")
    if not bot:
        return jsonify({"status": "error", "message": "Bot not found"}), 404
    db.add_event("BOT_STOP", f"{bot['symbol']} stopped new entries; exits still managed", bot_id=bot_id)
    return jsonify({"status": "Ok", "result": bot})


@app.route("/iifl/api/bots/<bot_id>/exit", methods=["POST"])
def api_bot_exit(bot_id):
    """EXIT_AND_STOP: sell every open position of this bot at market, then stop."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "EXIT":
        return jsonify({"status": "error", "message": "Confirmation required", "required_confirm": "EXIT"}), 400
    bot = update_bot(bot_id, status="EXITING")
    if not bot:
        return jsonify({"status": "error", "message": "Bot not found"}), 404
    db.add_event("BOT_EXIT", f"{bot['symbol']} exit-and-stop requested", level="WARNING", bot_id=bot_id)
    return jsonify({"status": "Ok", "result": bot})


def _age_seconds(iso_value):
    if not iso_value:
        return None
    try:
        dt = datetime.fromisoformat(iso_value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except ValueError:
        return None


@app.route("/iifl/api/system", methods=["GET"])
def api_system():
    now = datetime.now(IST)
    session_file = load_session()
    session_state = db.get_runtime("broker_session") or {}
    if not session_file or not session_file.get("user_session"):
        badge = "RECONNECT REQUIRED"
    elif session_state.get("state") == "EXPIRED":
        badge = "SESSION EXPIRED"
    elif session_state.get("state") == "CONNECTED":
        badge = "CONNECTED"
    else:
        badge = "UNVERIFIED"

    heartbeat = db.get_runtime("worker_heartbeat") or {}
    hb_age = _age_seconds(heartbeat.get("at"))
    worker = "RUNNING" if hb_age is not None and hb_age <= config.WORKER_STALE_SECONDS else "NOT RUNNING"

    events = db.rows("SELECT ts, level, bot_id, kind, message FROM events ORDER BY id DESC LIMIT 40")
    return jsonify({
        "status": "Ok",
        "result": {
            "broker_session": badge,
            "session_detail": session_state.get("detail"),
            "session_checked_at": session_state.get("checked_at"),
            "worker": worker,
            "worker_heartbeat": heartbeat.get("at"),
            "market": market_calendar.market_status(now),
            "live_trading": config.LIVE_TRADING,
            "ip_authorized": config.IIFL_TRADING_IP_AUTHORIZED,
            "live_max_qty": config.LIVE_MAX_QTY_PER_ORDER,
            "global_capital": config.GLOBAL_MAX_CAPITAL,
            "reconciliation": db.get_runtime("reconciliation"),
            "events": events,
        }
    })


@app.route("/iifl/api/orders", methods=["GET"])
def api_orders():
    orders = db.rows(
        "SELECT id, bot_id, symbol, side, mode, qty, state, broker_order_id, filled_qty, avg_price, "
        "reject_reason, broker_status, created_at, completed_at FROM orders "
        "WHERE mode='LIVE' ORDER BY id DESC LIMIT 100")
    return jsonify({"status": "Ok", "result": orders})


@app.route("/iifl/api/reports/summary", methods=["GET"])
def api_reports_summary():
    mode = request.args.get("mode")
    where = "WHERE mode=?" if mode in ("LIVE", "PAPER") else ""
    params = (mode,) if where else ()
    stats = db.row(
        f"""SELECT COUNT(*) AS trade_count,
                   SUM(CASE WHEN status IN ('OPEN','EXIT_SUBMITTED') THEN 1 ELSE 0 END) AS open_trades,
                   COALESCE(SUM(CASE WHEN status='CLOSED' THEN pnl END),0) AS realized_pnl,
                   SUM(CASE WHEN status='CLOSED' AND pnl>0 THEN 1 ELSE 0 END) AS winning_trades,
                   SUM(CASE WHEN status='CLOSED' AND pnl<0 THEN 1 ELSE 0 END) AS losing_trades
            FROM trades {where}""", params)
    trades = db.rows(f"SELECT * FROM trades {where} ORDER BY id DESC LIMIT 200", params)
    trades.reverse()

    return jsonify({
        "status": "Ok",
        "result": {
            "trade_count": stats["trade_count"] or 0,
            "open_trades": stats["open_trades"] or 0,
            "realized_pnl": round(stats["realized_pnl"] or 0, 2),
            "winning_trades": stats["winning_trades"] or 0,
            "losing_trades": stats["losing_trades"] or 0,
            "trades": trades
        }
    })


if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5015,
        debug=False
    )
