from config import IIFL_TRADING_IP_AUTHORIZED, LIVE_TRADING
from iifl_client import request_json


class LiveTradingDisabled(RuntimeError):
    pass


def build_market_order(*, instrument_id, exchange, side, quantity, product="INTRADAY"):
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")

    if int(quantity) <= 0:
        raise ValueError("quantity must be greater than zero")

    return [{
        "instrumentId": str(instrument_id),
        "exchange": exchange,
        "transactionType": side,
        "quantity": str(quantity),
        "orderComplexity": "REGULAR",
        "product": product,
        "orderType": "MARKET",
        "validity": "DAY",
    }]


def assert_live_trading_allowed():
    if not LIVE_TRADING:
        raise LiveTradingDisabled("LIVE_TRADING is false")

    if not IIFL_TRADING_IP_AUTHORIZED:
        raise LiveTradingDisabled("IIFL_TRADING_IP_AUTHORIZED is false")


def place_order(payload):
    assert_live_trading_allowed()
    return request_json("POST", "/v1/orders", payload=payload)


def build_sbc_test_order():
    return build_market_order(
        instrument_id="6792",
        exchange="NSEEQ",
        side="BUY",
        quantity=1,
        product="INTRADAY",
    )


def place_sbc_test_order():
    payload = build_sbc_test_order()
    return place_order(payload)
