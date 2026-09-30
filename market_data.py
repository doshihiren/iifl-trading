import json
from datetime import datetime

import requests

from config import IIFL_BASE_URL
from iifl_client import auth_headers


TIMEFRAME_MAP = {
    "1m": "1 minute",
    "5m": "5 minutes",
    "10m": "10 minutes",
    "15m": "15 minutes",
    "30m": "30 minutes",
    "60m": "60 minutes",
    "1d": "1 day",
}


class MarketDataError(RuntimeError):
    pass


def _decode_response(response):
    try:
        body = response.json()
    except Exception:
        text = response.text.strip()
        try:
            body = json.loads(text)
        except Exception:
            body = {
                "status": "error",
                "message": "IIFL returned an unparseable market-data response",
                "raw_response": text[:2000],
            }

    if isinstance(body, str):
        try:
            return json.loads(body)
        except Exception:
            return {
                "status": "error",
                "message": "IIFL returned an unparseable string response",
                "raw_response": body[:2000],
            }

    return body


def _post(path, payload, timeout=20):
    response = requests.post(
        f"{IIFL_BASE_URL}{path}",
        headers=auth_headers(),
        json=payload,
        timeout=timeout,
    )
    return response.status_code, _decode_response(response)


def market_quote(instrument_id, exchange="NSEEQ"):
    payload = [{
        "exchange": exchange,
        "instrumentId": str(instrument_id),
    }]
    return _post("/v1/marketdata/marketquotes", payload)


def historical_candles(
    instrument_id,
    *,
    exchange="NSEEQ",
    timeframe="1m",
    from_date,
    to_date,
):
    if timeframe not in TIMEFRAME_MAP:
        raise MarketDataError(
            f"Unsupported timeframe: {timeframe}. "
            f"Use one of: {', '.join(TIMEFRAME_MAP)}"
        )

    for value, label in ((from_date, "from_date"), (to_date, "to_date")):
        try:
            datetime.strptime(value, "%d-%b-%Y")
        except ValueError as exc:
            raise MarketDataError(
                f"{label} must use DD-Mon-YYYY format, for example 30-Sep-2026"
            ) from exc

    payload = {
        "exchange": exchange,
        "instrumentId": str(instrument_id),
        "interval": TIMEFRAME_MAP[timeframe],
        "fromDate": from_date,
        "toDate": to_date,
    }

    return _post("/v1/marketdata/historicaldata", payload)


def extract_ltp(body):
    """Return LTP from the confirmed IIFL market quote response."""
    if not isinstance(body, dict):
        raise MarketDataError("Unexpected market quote response")

    result = body.get("result")
    if not isinstance(result, list) or not result:
        raise MarketDataError("Market quote result is empty")

    item = result[0]
    if not isinstance(item, dict) or item.get("ltp") is None:
        raise MarketDataError("LTP is missing from market quote response")

    return float(item["ltp"])
