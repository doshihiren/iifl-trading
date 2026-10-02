"""IIFL Capital trading endpoints used by the LIVE execution engine.

Endpoints (base https://api.iiflcapital.com):
    POST   /v1/orders            place order (body is a JSON list)
    GET    /v1/orders            order book  (orderStatus, filledQuantity, averageTradedPrice)
    DELETE /v1/orders/{id}       cancel order
    GET    /v1/trades            trade book  (tradedPrice, filledQuantity, exchangeTradeId)
    GET    /v1/positions         net positions (netQuantity per product)
    GET    /v1/holdings          demat holdings (totalQuantity, dpQuantity, t1Quantity)
    GET    /v1/profile           cheap call used to check the session

Order writes are NEVER retried automatically. If IIFL does not clearly accept
or clearly reject an order, the result is UNKNOWN and the engine resolves it
from the order book instead of sending it again.
"""

import logging
import re

import requests

from config import IIFL_BASE_URL
from iifl_client import IIFLClientError, load_user_session

log = logging.getLogger("iifl.broker")

SESSION_ERROR_HINTS = (
    "session", "token", "unauthor", "not authorized", "login", "jwt", "expired",
)
AMBIGUOUS_HINTS = ("try after some time", "try again", "something went wrong", "timeout", "rate")
OK_STATUSES = {"ok", "success"}
_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SessionExpired(RuntimeError):
    pass


class BrokerError(RuntimeError):
    pass


# ---- HTTP plumbing -----------------------------------------------------

def _headers():
    try:
        token = load_user_session()
    except IIFLClientError as exc:
        raise SessionExpired(str(exc)) from exc
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _decode(response):
    try:
        return response.json()
    except Exception:
        return {"status": "error", "message": (response.text or "")[:500]}


def message_of(body):
    if isinstance(body, dict):
        for key in ("message", "error", "description", "emsg"):
            if body.get(key):
                return str(body[key])
        result = body.get("result")
        if isinstance(result, list) and result and isinstance(result[0], dict):
            for key in ("message", "error", "description"):
                if result[0].get(key):
                    return str(result[0][key])
        if isinstance(result, dict):
            for key in ("message", "error", "description"):
                if result.get(key):
                    return str(result[key])
    return ""


def is_session_error(http_status, body):
    if http_status in (401, 403):
        return True
    msg = message_of(body).lower()
    if not msg:
        return False
    if "session" in msg and any(w in msg for w in ("invalid", "expire", "not found", "logged")):
        return True
    return any(w in msg for w in ("unauthor", "invalid token", "token expired", "jwt"))


def _get(path, timeout=15):
    response = requests.get(f"{IIFL_BASE_URL}{path}", headers=_headers(), timeout=timeout)
    body = _decode(response)
    if is_session_error(response.status_code, body):
        raise SessionExpired(message_of(body) or f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise BrokerError(f"GET {path} HTTP {response.status_code}: {message_of(body)}")
    return body


def extract_rows(body):
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if not isinstance(body, dict):
        return []
    result = body.get("result")
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, dict):
        for key in ("orders", "trades", "positions", "holdings", "data", "positionList"):
            if isinstance(result.get(key), list):
                return [r for r in result[key] if isinstance(r, dict)]
        return []
    for key in ("data", "orders", "trades", "positions", "holdings"):
        if isinstance(body.get(key), list):
            return [r for r in body[key] if isinstance(r, dict)]
    return []


def _num(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# ---- session -----------------------------------------------------------

def check_session():
    """Return True if the saved IIFL session works, raise SessionExpired if not."""
    _get("/v1/profile")
    return True


# ---- order placement ---------------------------------------------------

def build_order(*, instrument_id, exchange, side, quantity, product, tag=None):
    order = {
        "instrumentId": str(instrument_id),
        "exchange": exchange,
        "transactionType": side,
        "quantity": str(int(quantity)),
        "orderComplexity": "REGULAR",
        "product": product,
        "orderType": "MARKET",
        "validity": "DAY",
    }
    if tag:
        order["orderTag"] = tag
    return [order]


def place_order(payload, timeout=15):
    """Send one order. Returns dict(outcome, broker_order_id, message, http_status, body).

    outcome is one of:
      ACCEPTED  broker returned an order id
      REJECTED  broker clearly refused; nothing was placed
      SESSION   session invalid; nothing was placed
      UNKNOWN   cannot tell if IIFL received it; resolve from the order book
    """
    try:
        headers = _headers()
    except SessionExpired as exc:
        return {"outcome": "SESSION", "message": str(exc), "http_status": None, "body": None,
                "broker_order_id": None}

    url = f"{IIFL_BASE_URL}/v1/orders"
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.ConnectTimeout as exc:
        # The TCP connection never opened, so the order cannot have been sent.
        return {"outcome": "REJECTED", "message": f"not sent: connect timeout ({exc.__class__.__name__})",
                "http_status": None, "body": None, "broker_order_id": None}
    except requests.exceptions.RequestException as exc:
        return {"outcome": "UNKNOWN", "message": f"no response: {exc.__class__.__name__}",
                "http_status": None, "body": None, "broker_order_id": None}

    body = _decode(response)
    msg = message_of(body)
    result = {"http_status": response.status_code, "body": body, "message": msg, "broker_order_id": None}

    first = {}
    if isinstance(body, dict):
        res = body.get("result")
        if isinstance(res, list) and res and isinstance(res[0], dict):
            first = res[0]
        elif isinstance(res, dict):
            first = res
    order_id = first.get("brokerOrderId") or (body.get("brokerOrderId") if isinstance(body, dict) else None)
    top_ok = isinstance(body, dict) and str(body.get("status", "")).lower() in OK_STATUSES
    first_status = str(first.get("status", "")).lower()

    if response.status_code == 200 and order_id and (top_ok or first_status in OK_STATUSES):
        result.update(outcome="ACCEPTED", broker_order_id=str(order_id))
        return result

    if is_session_error(response.status_code, body):
        result["outcome"] = "SESSION"
        return result

    lowered = msg.lower()
    if response.status_code == 429 or response.status_code >= 500 or any(h in lowered for h in AMBIGUOUS_HINTS):
        result["outcome"] = "UNKNOWN"
        return result

    if order_id:
        # An order id came back without a clear success flag: it exists at the broker.
        result.update(outcome="ACCEPTED", broker_order_id=str(order_id))
        return result

    result["outcome"] = "REJECTED"
    return result


def cancel_order(broker_order_id):
    if not _ORDER_ID_RE.match(str(broker_order_id)):
        raise ValueError("invalid broker order id")
    response = requests.delete(f"{IIFL_BASE_URL}/v1/orders/{broker_order_id}", headers=_headers(), timeout=15)
    body = _decode(response)
    if is_session_error(response.status_code, body):
        raise SessionExpired(message_of(body))
    return response.status_code, body


# ---- books -------------------------------------------------------------

def order_book():
    return [normalize_order_row(r) for r in extract_rows(_get("/v1/orders"))]


def trade_book():
    return [normalize_trade_row(r) for r in extract_rows(_get("/v1/trades"))]


def positions():
    return extract_rows(_get("/v1/positions"))


def holdings():
    return extract_rows(_get("/v1/holdings"))


def map_order_status(raw):
    s = str(raw or "").strip().upper().replace("-", "_")
    if s in {"COMPLETE", "COMPLETED", "FILLED", "EXECUTED", "TRADED", "FULLY_EXECUTED", "SUCCESS"}:
        return "COMPLETE"
    if s in {"REJECTED", "FAILED", "FAIL", "REJECT"}:
        return "REJECTED"
    if s in {"CANCELLED", "CANCELED", "CANCEL"}:
        return "CANCELLED"
    if s in {"PARTIALLY_FILLED", "PARTIAL", "PARTIALLY_EXECUTED"}:
        return "PARTIAL"
    return "OPEN"


def normalize_order_row(r):
    return {
        "broker_order_id": str(r.get("brokerOrderId") or r.get("orderId") or ""),
        "exchange_order_id": str(r.get("exchangeOrderId") or ""),
        "status": map_order_status(r.get("orderStatus") or r.get("status")),
        "raw_status": str(r.get("orderStatus") or r.get("status") or ""),
        "instrument_id": str(r.get("instrumentId") or ""),
        "side": str(r.get("transactionType") or "").upper(),
        "product": str(r.get("product") or "").upper(),
        "quantity": int(_num(r.get("quantity") or r.get("orderQuantity"))),
        "filled_qty": int(_num(r.get("filledQuantity"))),
        "avg_price": _num(r.get("averageTradedPrice")) or None,
        "reject_reason": str(r.get("rejectionReason") or ""),
        "tag": str(r.get("orderTag") or r.get("comments") or r.get("remarks") or ""),
        "time": str(r.get("exchangeTimestamp") or r.get("brokerUpdateTime") or r.get("exchangeUpdateTime") or ""),
    }


def normalize_trade_row(r):
    return {
        "instrument_id": str(r.get("instrumentId") or ""),
        "side": str(r.get("transactionType") or "").upper(),
        "product": str(r.get("product") or "").upper(),
        "broker_order_id": str(r.get("brokerOrderId") or r.get("orderId") or ""),
        "trade_ref": str(r.get("exchangeTradeId") or r.get("tradeId") or ""),
        "qty": int(_num(r.get("filledQuantity") or r.get("quantity") or r.get("filledQty"))),
        "price": _num(r.get("tradedPrice") or r.get("averageTradedPrice") or r.get("price")),
        "time": " ".join(str(x) for x in (r.get("fillDate"), r.get("fillTime")) if x)
                or str(r.get("exchangeTimestamp") or ""),
    }


def broker_quantity(instrument_id, trading_symbol, product, positions_rows, holdings_rows):
    """Shares the broker says we can sell for this instrument and product."""
    iid = str(instrument_id)
    syms = {s.upper() for s in (trading_symbol or "",) if s}
    if trading_symbol and trading_symbol.upper().endswith("-EQ"):
        syms.add(trading_symbol.upper()[:-3])

    def matches(r):
        ids = {str(r.get(k) or "") for k in ("instrumentId", "nseInstrumentId", "token")}
        names = {str(r.get(k) or "").upper() for k in ("tradingSymbol", "nseTradingSymbol", "symbol")}
        return iid in ids or bool(syms & names)

    qty = 0
    for r in positions_rows:
        if matches(r) and str(r.get("product") or "").upper() == product:
            qty += int(_num(r.get("netQuantity", r.get("quantity"))))
    if product == "DELIVERY":
        for r in holdings_rows:
            if matches(r):
                qty += int(_num(r.get("totalQuantity", r.get("quantity"))))
    return qty
