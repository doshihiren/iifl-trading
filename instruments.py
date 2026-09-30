import requests

from config import IIFL_BASE_URL


class InstrumentLookupError(RuntimeError):
    pass


def get_contracts(exchange):
    response = requests.get(
        f"{IIFL_BASE_URL}/v1/contractfiles/{exchange}.json",
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def find_instrument(symbol, exchange="NSEEQ"):
    symbol = symbol.strip().upper()
    contracts = get_contracts(exchange)

    for item in contracts:
        trading_symbol = str(item.get("tradingSymbol", "")).upper()
        underlying_symbol = str(item.get("underlyingInstrumentSymbol", "")).upper()
        underlying_name = str(item.get("underlyingInstrumentName", "")).upper()

        if symbol in {trading_symbol, underlying_symbol, underlying_name}:
            return item

        if trading_symbol == f"{symbol}-EQ":
            return item

    raise InstrumentLookupError(f"Instrument not found: {symbol} on {exchange}")
