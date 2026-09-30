import os
import hashlib
import json
import requests

from flask import Flask, request, redirect, jsonify, render_template, session, url_for
from dotenv import load_dotenv

from networking import force_ipv4

load_dotenv("/root/iifl/.env")
force_ipv4()

from instruments import InstrumentLookupError, find_instrument
from market_data import MarketDataError, historical_candles, market_quote
from orders import LiveTradingDisabled, build_sbc_test_order, place_sbc_test_order
from bot_store import list_bots, upsert_bot, remove_bot, list_trades

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(32).hex()

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


@app.route("/iifl/api/bots", methods=["GET"])
def api_bots_list():
    return jsonify({
        "status": "Ok",
        "result": list_bots()
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

    saved = upsert_bot({
        "symbol": symbol,
        "tradingSymbol": instrument.get("tradingSymbol"),
        "instrumentId": str(instrument.get("instrumentId")),
        "exchange": instrument.get("exchange", "NSEEQ"),
        "mode": mode,
        "qty": quantity,
        "target": target,
        "timeframe": timeframe,
        "product": product,
        "maxpos": maxpos,
        "capital": capital,
        "status": "READY"
    })

    return jsonify({
        "status": "Ok",
        "message": "Bot saved",
        "result": saved
    })


@app.route("/iifl/api/bots/<symbol>", methods=["DELETE"])
def api_bots_delete(symbol):
    removed = remove_bot(symbol)
    return jsonify({
        "status": "Ok",
        "removed": removed
    })


@app.route("/iifl/api/reports/summary", methods=["GET"])
def api_reports_summary():
    trades = list_trades()
    realized = 0.0
    winning = 0
    losing = 0
    open_count = 0

    for trade in trades:
        status = str(trade.get("status", "")).upper()
        pnl = float(trade.get("pnl", 0) or 0)
        if status == "OPEN":
            open_count += 1
        else:
            realized += pnl
            if pnl > 0:
                winning += 1
            elif pnl < 0:
                losing += 1

    return jsonify({
        "status": "Ok",
        "result": {
            "trade_count": len(trades),
            "open_trades": open_count,
            "realized_pnl": round(realized, 2),
            "winning_trades": winning,
            "losing_trades": losing,
            "trades": trades[-100:]
        }
    })


if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5015,
        debug=False
    )
