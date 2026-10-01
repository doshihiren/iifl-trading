import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bot_store import add_trade, close_trade, list_bots, open_trades, update_bot
from config import BOT_POLL_SECONDS, GLOBAL_MAX_CAPITAL
from market_data import extract_ltp, market_quote
from networking import force_ipv6

force_ipv6()

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    return datetime.now(IST)


def market_open(now=None):
    now = now or now_ist()
    if now.weekday() >= 5:
        return False
    minute_of_day = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minute_of_day < 15 * 60 + 30


def timeframe_minutes(value):
    return {"1m": 1, "5m": 5, "15m": 15}.get(str(value).lower(), 15)


def current_slot(bot, now=None):
    now = now or now_ist()
    start = 9 * 60 + 15
    total = now.hour * 60 + now.minute
    if total < start:
        return None

    step = timeframe_minutes(bot.get("timeframe", "15m"))
    slot = start + ((total - start) // step) * step
    hour, minute = divmod(slot, 60)
    return now.strftime("%Y-%m-%d") + f"T{hour:02d}:{minute:02d}"


def get_ltp(bot):
    status, body = market_quote(
        bot["instrumentId"],
        exchange=bot.get("exchange", "NSEEQ"),
    )
    if status != 200:
        raise RuntimeError(f"Quote HTTP {status}")
    return extract_ltp(body)


def total_open_capital():
    return sum(
        float(t.get("entry_price", 0)) * int(t.get("quantity", 0))
        for t in open_trades()
    )


def manage_targets(bot, ltp):
    bot_id = bot["bot_id"]

    if bot.get("pending_order"):
        return

    for trade in open_trades(bot_id=bot_id):
        if ltp < float(trade.get("target_price", 0)):
            continue

        if bot.get("mode") == "PAPER":
            close_trade(trade["id"], exit_price=ltp)
        else:
            update_bot(
                bot_id,
                status="ACTION_REQUIRED",
                pending_order={
                    "side": "SELL",
                    "trade_id": trade["id"],
                    "quantity": int(trade["quantity"]),
                    "reference_price": ltp,
                    "created_at": now_ist().isoformat(),
                },
            )
        return


def maybe_create_entry(bot, ltp):
    bot_id = bot["bot_id"]

    if bot.get("pending_order"):
        return

    slot = current_slot(bot)
    if not slot or bot.get("last_entry_slot") == slot:
        return

    qty = int(bot.get("qty", 1))
    estimated = ltp * qty
    opens = open_trades(bot_id=bot_id)

    if len(opens) >= int(bot.get("maxpos", 10)):
        update_bot(
            bot_id,
            last_entry_slot=slot,
            last_error="Max open positions reached",
        )
        return

    bot_capital = sum(
        float(t.get("entry_price", 0)) * int(t.get("quantity", 0))
        for t in opens
    )

    if bot_capital + estimated > float(bot.get("capital", GLOBAL_MAX_CAPITAL)):
        update_bot(
            bot_id,
            last_entry_slot=slot,
            last_error="Bot capital limit reached",
        )
        return

    if total_open_capital() + estimated > GLOBAL_MAX_CAPITAL:
        update_bot(
            bot_id,
            last_entry_slot=slot,
            last_error="Global capital limit reached",
        )
        return

    target = round(
        ltp * (1 + float(bot.get("target", 1.0)) / 100.0),
        2,
    )

    if bot.get("mode") == "PAPER":
        add_trade({
            "bot_id": bot_id,
            "symbol": bot["symbol"],
            "side": "BUY",
            "quantity": qty,
            "entry_price": ltp,
            "target_price": target,
            "mode": "PAPER",
            "product": bot.get("product", "DELIVERY"),
            "timeframe": bot.get("timeframe", "15m"),
            "pnl": 0.0,
        })

        update_bot(
            bot_id,
            last_entry_slot=slot,
            last_tick=now_ist().isoformat(),
            last_error=None,
            status="RUNNING",
        )
    else:
        update_bot(
            bot_id,
            last_entry_slot=slot,
            status="ACTION_REQUIRED",
            last_tick=now_ist().isoformat(),
            last_error=None,
            pending_order={
                "side": "BUY",
                "quantity": qty,
                "reference_price": ltp,
                "target_price": target,
                "created_at": now_ist().isoformat(),
            },
        )


def run_once():
    if not market_open():
        return

    for bot in list_bots():
        if bot.get("status") != "RUNNING":
            continue

        bot_id = bot.get("bot_id")
        if not bot_id:
            continue

        try:
            ltp = get_ltp(bot)
            manage_targets(bot, ltp)

            fresh = next(
                (b for b in list_bots() if b.get("bot_id") == bot_id),
                bot,
            )

            if fresh.get("status") == "RUNNING":
                maybe_create_entry(fresh, ltp)

            update_bot(
                bot_id,
                last_tick=now_ist().isoformat(),
            )
        except Exception as exc:
            update_bot(
                bot_id,
                last_tick=now_ist().isoformat(),
                last_error=str(exc),
            )


if __name__ == "__main__":
    while True:
        run_once()
        time.sleep(BOT_POLL_SECONDS)
