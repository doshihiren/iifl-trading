"""SVIE data extractor – pulls SBC (or any NSE script) data from IIFL for backtesting.

Run on the server (it uses the IIFL session your dashboard already saved):

    cd /root/iifl && source venv/bin/activate

    # 1) Download 1-minute candles into the existing `candles` table + CSV
    python svie_data.py fetch --symbol SBC --from 2026-09-28 --to 2026-11-27

    # 2) Check what is stored (missing days / minutes, zero-volume bars)
    python svie_data.py check --symbol SBC --from 2026-09-28 --to 2026-11-27

    # 3) Export stored candles to CSV again (no IIFL call)
    python svie_data.py export --symbol SBC --from 2026-09-28 --to 2026-11-27

    # 4) From now on, record live market-depth snapshots every 5 s during
    #    market hours (IIFL history has NO order book, so this is the only way
    #    to backtest bid/ask imbalance later)
    python svie_data.py record-depth --symbol SBC --every 5

What IIFL history gives:  1-minute OHLC + volume only.
What it does NOT give:    trade-by-trade aggressor side, bid/ask depth.
The backtester therefore ESTIMATES buy/sell volume from each candle and leaves
market-depth rules out of the historical test (no fabricated order-book data).
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
import db

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("iifl.svie_data")
EXPORT_DIR = os.path.join(config.DATA_DIR, "svie")

DEPTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS quote_snapshots (
    instrumentId TEXT NOT NULL,
    ts           TEXT NOT NULL,      -- IST 'YYYY-MM-DD HH:MM:SS' (when we polled)
    ltp          REAL,
    volume       INTEGER,            -- cumulative day volume reported by IIFL
    total_buy_qty  INTEGER,          -- total pending bid quantity (displayed, NOT executed)
    total_sell_qty INTEGER,          -- total pending ask quantity
    best_bid     REAL, best_bid_qty INTEGER,
    best_ask     REAL, best_ask_qty INTEGER,
    bids_json    TEXT,               -- [[price, qty, orders], ...] top levels
    asks_json    TEXT,
    raw_json     TEXT,               -- full IIFL reply (so nothing is lost)
    PRIMARY KEY (instrumentId, ts)
) WITHOUT ROWID;
"""


# ------------------------------------------------------------------ helpers
def resolve(symbol, exchange="NSEEQ"):
    """symbol -> (instrumentId, tradingSymbol). Uses the IIFL contract file."""
    import instruments
    item = instruments.find_instrument(symbol, exchange)
    return str(item["instrumentId"]), item.get("tradingSymbol") or symbol


def load_candles(instrument_id, d_from, d_to):
    import candles
    return candles.load(instrument_id, d_from, d_to)


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow([r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"]])
    return path


def read_csv(path):
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out.append({"ts": r["ts"][:16], "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": int(float(r.get("volume") or 0))})
    out.sort(key=lambda r: r["ts"])
    return out


def quality_report(rows, d_from, d_to):
    """Days with no data, days with missing minutes, zero-volume bars."""
    import candles
    expected_days = [d.isoformat() for d in candles.trading_days(d_from, d_to)]
    by_day = {}
    for r in rows:
        by_day.setdefault(r["ts"][:10], []).append(r)
    full_minutes = 375  # 09:15 .. 15:29
    short = {d: len(v) for d, v in by_day.items() if len(v) < full_minutes - 5}
    return {
        "candles": len(rows),
        "trading_days_expected": len(expected_days),
        "days_with_data": len(by_day),
        "missing_days": [d for d in expected_days if d not in by_day],
        "short_days": short,                                   # day -> minutes received
        "zero_volume_bars": sum(1 for r in rows if not r["volume"]),
        "first": rows[0]["ts"] if rows else None,
        "last": rows[-1]["ts"] if rows else None,
    }


def _print_report(rep):
    print(f"  candles stored      : {rep['candles']}")
    print(f"  trading days        : {rep['days_with_data']} of {rep['trading_days_expected']} have data")
    print(f"  first / last candle : {rep['first']}  ->  {rep['last']}")
    print(f"  zero-volume bars    : {rep['zero_volume_bars']}")
    if rep["missing_days"]:
        print(f"  MISSING days        : {', '.join(rep['missing_days'])}")
    if rep["short_days"]:
        print("  short days (minutes): " + ", ".join(f"{d}={n}" for d, n in sorted(rep["short_days"].items())))


def _csv_path(symbol, d_from, d_to):
    return os.path.join(EXPORT_DIR, f"{symbol.upper()}_1m_{d_from}_{d_to}.csv")


# ------------------------------------------------------------------ commands
def cmd_fetch(a):
    import candles
    d_from, d_to = date.fromisoformat(a.date_from), date.fromisoformat(a.date_to)
    iid, tsym = (a.instrument_id, a.symbol) if a.instrument_id else resolve(a.symbol, a.exchange)
    print(f"{a.symbol} = instrumentId {iid} ({tsym}) on {a.exchange}")
    sym = {"instrumentId": iid, "symbol": a.symbol.upper(), "exchange": a.exchange}

    # sync_range() writes progress to analysis_symbols; make sure a row exists.
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO analysis_symbols(instrumentId, symbol, tradingSymbol, exchange, "
                  "sync_from, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                  (iid, a.symbol.upper(), tsym, a.exchange, d_from.isoformat(), db.now_utc(), db.now_utc()))

    print(f"Fetching 1-minute candles {d_from} .. {d_to} from IIFL (5-day chunks)…")
    result = candles.sync_range(sym, d_from, d_to)
    print(f"  stored {result['stored']} candles over {result['days']} trading days")
    if result["empty_days"]:
        print(f"  IIFL returned no data for: {', '.join(result['empty_days'])}")
    if result["failed_days"]:
        print(f"  FAILED days (re-run fetch to retry): {', '.join(result['failed_days'])}")

    rows = load_candles(iid, d_from, d_to)
    path = write_csv(a.out or _csv_path(a.symbol, d_from, d_to), rows)
    print(f"\nCSV written: {path}")
    _print_report(quality_report(rows, d_from, d_to))


def cmd_check(a):
    d_from, d_to = date.fromisoformat(a.date_from), date.fromisoformat(a.date_to)
    iid = a.instrument_id or resolve(a.symbol, a.exchange)[0]
    rows = load_candles(iid, d_from, d_to)
    print(f"{a.symbol} ({iid}) stored data {d_from} .. {d_to}")
    _print_report(quality_report(rows, d_from, d_to))


def cmd_export(a):
    d_from, d_to = date.fromisoformat(a.date_from), date.fromisoformat(a.date_to)
    iid = a.instrument_id or resolve(a.symbol, a.exchange)[0]
    rows = load_candles(iid, d_from, d_to)
    print(write_csv(a.out or _csv_path(a.symbol, d_from, d_to), rows), f"({len(rows)} candles)")


# ---- live depth recorder ---------------------------------------------------
def _n(v, cast=float):
    try:
        return cast(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _levels(item, *keys):
    for k in keys:
        lv = item.get(k)
        if isinstance(lv, list):
            out = []
            for x in lv:
                if isinstance(x, dict):
                    out.append([_n(x.get("price")), _n(x.get("quantity") or x.get("qty"), int),
                                _n(x.get("orders") or x.get("numberOfOrders"), int)])
                elif isinstance(x, (list, tuple)) and len(x) >= 2:
                    out.append([_n(x[0]), _n(x[1], int), _n(x[2], int) if len(x) > 2 else None])
            return out
    return []


def parse_quote(body):
    """IIFL marketquotes reply -> snapshot fields. Field names are read defensively;
    the raw reply is always stored too, so nothing is lost if a name differs."""
    item = {}
    if isinstance(body, dict):
        res = body.get("result")
        if isinstance(res, list) and res and isinstance(res[0], dict):
            item = res[0]
        elif isinstance(res, dict):
            item = res
    depth = item.get("marketDepth") or item.get("depth") or item
    bids = _levels(depth if isinstance(depth, dict) else item, "bids", "bid", "buy", "buyDepth")
    asks = _levels(depth if isinstance(depth, dict) else item, "asks", "ask", "sell", "sellDepth")

    def pick(*ks):
        for k in ks:
            if item.get(k) not in (None, ""):
                return item[k]
        return None
    return {
        "ltp": _n(pick("ltp", "lastTradedPrice")),
        "volume": _n(pick("volume", "totalTradedVolume", "tradedVolume"), int),
        "total_buy_qty": _n(pick("totalBuyQuantity", "totalBidQuantity", "totalBuyQty"), int),
        "total_sell_qty": _n(pick("totalSellQuantity", "totalAskQuantity", "totalSellQty"), int),
        "best_bid": bids[0][0] if bids else _n(pick("bestBidPrice", "bidPrice")),
        "best_bid_qty": bids[0][1] if bids else _n(pick("bestBidQuantity", "bidQuantity"), int),
        "best_ask": asks[0][0] if asks else _n(pick("bestAskPrice", "askPrice")),
        "best_ask_qty": asks[0][1] if asks else _n(pick("bestAskQuantity", "askQuantity"), int),
        "bids_json": json.dumps(bids), "asks_json": json.dumps(asks),
    }


def cmd_record_depth(a):
    import broker
    import market_calendar as cal
    import market_data
    every = max(2.0, float(a.every))
    iid = a.instrument_id or resolve(a.symbol, a.exchange)[0]
    db.conn().executescript(DEPTH_SCHEMA)
    print(f"Recording {a.symbol} ({iid}) quotes every {every:.0f}s during market hours. Ctrl+C to stop.")
    shown_raw = False
    while True:
        now = datetime.now(IST)
        if not cal.is_trading_day(now) or not (cal.parse_hhmm("09:15") <= now.time() <= cal.parse_hhmm("15:30")):
            time.sleep(30)
            continue
        try:
            status, body = market_data.market_quote(iid, exchange=a.exchange)
            if broker.is_session_error(status, body):
                print(f"{now:%H:%M:%S} IIFL session expired – log in on the dashboard; retrying in 60s")
                time.sleep(60)
                continue
            if not shown_raw:
                print("First IIFL reply (check that depth fields are present):")
                print(json.dumps(body, indent=1)[:1500])
                shown_raw = True
            s = parse_quote(body)
            with db.tx() as c:
                c.execute("INSERT OR REPLACE INTO quote_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (iid, now.strftime("%Y-%m-%d %H:%M:%S"), s["ltp"], s["volume"], s["total_buy_qty"],
                           s["total_sell_qty"], s["best_bid"], s["best_bid_qty"], s["best_ask"], s["best_ask_qty"],
                           s["bids_json"], s["asks_json"], json.dumps(body)[:20000]))
        except Exception as exc:  # keep recording through network blips
            log.warning("event=DEPTH_POLL_FAILED error=%s", exc)
            print(f"{now:%H:%M:%S} poll failed: {exc}")
        time.sleep(every)


def main(argv=None):
    p = argparse.ArgumentParser(description="SVIE data extractor (IIFL)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "check", "export"):
        s = sub.add_parser(name)
        s.add_argument("--symbol", default="SBC")
        s.add_argument("--exchange", default="NSEEQ")
        s.add_argument("--instrument-id", help="skip contract lookup (SBC = 6792)")
        s.add_argument("--from", dest="date_from", default="2026-09-28")
        s.add_argument("--to", dest="date_to", default="2026-11-27")
        s.add_argument("--out", help="CSV path (default /root/iifl/data/svie/...)")
    s = sub.add_parser("record-depth")
    s.add_argument("--symbol", default="SBC")
    s.add_argument("--exchange", default="NSEEQ")
    s.add_argument("--instrument-id")
    s.add_argument("--every", default="5", help="seconds between polls (min 2)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    {"fetch": cmd_fetch, "check": cmd_check, "export": cmd_export, "record-depth": cmd_record_depth}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
