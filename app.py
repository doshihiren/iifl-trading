import os
import hashlib
import json
import requests

from flask import Flask, request, redirect, jsonify, render_template
from dotenv import load_dotenv

from networking import force_ipv4

load_dotenv("/root/iifl/.env")
force_ipv4()

from instruments import InstrumentLookupError, find_instrument
from market_data import MarketDataError, historical_candles, market_quote
from orders import LiveTradingDisabled, build_sbc_test_order, place_sbc_test_order

app = Flask(__name__)

@app.route("/iifl/")
def dashboard():
    return render_template("dashboard.html")


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


if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5015,
        debug=False
    )
