"""Lightweight RSI + volume summary for the main order dashboard."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

import candles as candle_store
from instruments import InstrumentLookupError, find_instrument
import rsi_backtest as rb

IST = ZoneInfo("Asia/Kolkata")
bp = Blueprint("technical_summary", __name__)


def _signal(rsi, prev_rsi, volume_ratio):
    rising = rsi > prev_rsi
    falling = rsi < prev_rsi

    if rsi >= 70 and falling:
        return "SELL", "RSI is overbought and has started falling."
    if rsi >= 75 and volume_ratio is not None and volume_ratio >= 1.2:
        return "SELL", "RSI is strongly overbought with elevated volume; momentum may be extended."
    if rsi <= 35 and rising and volume_ratio is not None and volume_ratio >= 1.0:
        return "BUY", "RSI is low and turning upward with at/above-average volume."
    if rsi < 45 and rising and volume_ratio is not None and volume_ratio >= 1.2:
        return "BUY", "RSI is below neutral but rising, while above-average volume confirms improving momentum."
    if rsi <= 30 and not rising:
        return "HOLD", "RSI is oversold but has not turned upward yet."
    if volume_ratio is not None and volume_ratio < 0.6:
        return "HOLD", "Volume is too weak to confirm the current RSI move."
    return "HOLD", "RSI and volume do not currently agree strongly enough for a BUY or SELL signal."


def _rsi_text(rsi, trend):
    if rsi <= 30:
        zone = "oversold"
    elif rsi < 45:
        zone = "weak / recovering"
    elif rsi <= 60:
        zone = "neutral"
    elif rsi < 70:
        zone = "strong momentum"
    else:
        zone = "overbought"
    return f"RSI {rsi:.1f} is {zone}; momentum is {trend}."


def _volume_text(volume_ratio):
    if volume_ratio is None:
        return "Volume comparison is unavailable."
    if volume_ratio >= 1.5:
        return f"Volume is high at {volume_ratio:.2f}x the recent 20-minute average."
    if volume_ratio >= 1.0:
        return f"Volume is above average at {volume_ratio:.2f}x."
    if volume_ratio >= 0.6:
        return f"Volume is below average at {volume_ratio:.2f}x."
    return f"Volume is low at only {volume_ratio:.2f}x the recent average."


@bp.route("/iifl/api/technical-now", methods=["GET"])
def api_technical_now():
    symbol = str(request.args.get("symbol", "")).strip().upper()
    if not symbol:
        return jsonify({"status": "error", "message": "symbol is required"}), 400

    try:
        inst = find_instrument(symbol, exchange="NSEEQ")
    except InstrumentLookupError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404

    now = datetime.now(IST)
    # A few calendar days gives enough warm-up candles even around weekends.
    d_from = now.date() - timedelta(days=5)
    try:
        rows = candle_store.iifl_fetch(
            str(inst["instrumentId"]),
            inst.get("exchange", "NSEEQ"),
            d_from,
            now.date(),
        )
    except Exception as exc:
        msg = str(exc)[:180]
        if exc.__class__.__name__ == "SessionExpired":
            msg = "IIFL session expired - login again for live RSI and volume."
        return jsonify({"status": "error", "message": msg}), 502

    if len(rows) < 22:
        return jsonify({"status": "error", "message": "Not enough candle data to calculate RSI and volume"}), 400

    closes = [float(x["close"]) for x in rows]
    rsis = rb.rsi_series(closes, rb.DEFAULT_PERIOD)
    rsi = float(rsis[-1])
    prev = float(rsis[-2])
    trend = "rising" if rsi > prev else ("falling" if rsi < prev else "flat")

    volumes = [float(x.get("volume") or 0) for x in rows]
    recent = volumes[-21:]
    avg20 = sum(recent[:-1]) / 20 if len(recent) == 21 else 0
    current_volume = recent[-1] if recent else 0
    ratio = round(current_volume / avg20, 2) if avg20 > 0 and current_volume > 0 else None

    action, reason = _signal(rsi, prev, ratio)

    return jsonify({
        "status": "Ok",
        "result": {
            "symbol": symbol,
            "instrumentId": str(inst["instrumentId"]),
            "price": float(rows[-1]["close"]),
            "candle": rows[-1]["ts"],
            "rsi": round(rsi, 1),
            "rsi_prev": round(prev, 1),
            "rsi_trend": trend,
            "volume": int(current_volume),
            "volume_avg20": round(avg20, 0) if avg20 else None,
            "volume_ratio": ratio,
            "signal": action,
            "signal_reason": reason,
            "rsi_text": _rsi_text(rsi, trend),
            "volume_text": _volume_text(ratio),
            "checked_at": now.isoformat(),
        },
    })
