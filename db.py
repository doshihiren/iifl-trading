"""SQLite persistence shared by the Flask app and the trading worker.

Both processes open their own connections. WAL mode lets the dashboard read
while the worker writes, and every write that must be atomic runs inside
``tx()`` which takes SQLite's write lock up front (BEGIN IMMEDIATE), so two
processes can never interleave half-finished changes.
"""

import json
import logging
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import config

log = logging.getLogger("iifl.db")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    bot_id          TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    tradingSymbol   TEXT,
    instrumentId    TEXT NOT NULL,
    exchange        TEXT NOT NULL DEFAULT 'NSEEQ',
    tick_size       REAL NOT NULL DEFAULT 0.05,
    mode            TEXT NOT NULL DEFAULT 'PAPER',
    qty             INTEGER NOT NULL DEFAULT 1,
    qty_random_pct  REAL NOT NULL DEFAULT 10,
    target          REAL NOT NULL DEFAULT 1.0,
    timeframe       TEXT NOT NULL DEFAULT '15m',
    product         TEXT NOT NULL DEFAULT 'DELIVERY',
    maxpos          INTEGER NOT NULL DEFAULT 10,
    capital         REAL NOT NULL DEFAULT 100000,
    status          TEXT NOT NULL DEFAULT 'READY',
    last_entry_slot TEXT,
    last_tick       TEXT,
    last_ltp        REAL,
    last_order      TEXT,
    last_error      TEXT,
    deleted         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_signals (
    signal_key  TEXT PRIMARY KEY,
    bot_id      TEXT NOT NULL,
    slot        TEXT NOT NULL,
    side        TEXT NOT NULL,
    ltp         REAL,
    decision    TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_key       TEXT NOT NULL UNIQUE,
    bot_id          TEXT NOT NULL,
    signal_key      TEXT,
    lot_id          INTEGER,
    side            TEXT NOT NULL,
    mode            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    instrumentId    TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    product         TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    reference_price REAL,
    state           TEXT NOT NULL,
    broker_order_id TEXT,
    order_tag       TEXT,
    filled_qty      INTEGER NOT NULL DEFAULT 0,
    avg_price       REAL,
    reject_reason   TEXT,
    broker_status   TEXT,
    response_json   TEXT,
    created_at      TEXT NOT NULL,
    submitted_at    TEXT,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_state ON orders(state);
CREATE INDEX IF NOT EXISTS ix_orders_bot ON orders(bot_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_broker
    ON orders(broker_order_id) WHERE broker_order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL,
    broker_order_id TEXT,
    trade_ref       TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    price           REAL NOT NULL,
    fill_time       TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(order_id, trade_ref)
);

CREATE TABLE IF NOT EXISTS trades (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id               TEXT,
    symbol               TEXT NOT NULL,
    side                 TEXT NOT NULL DEFAULT 'BUY',
    quantity             INTEGER NOT NULL,
    entry_price          REAL NOT NULL,
    target_price         REAL NOT NULL,
    mode                 TEXT NOT NULL,
    product              TEXT,
    timeframe            TEXT,
    status               TEXT NOT NULL DEFAULT 'OPEN',
    entry_order_id       INTEGER,
    exit_order_id        INTEGER,
    broker_buy_order_id  TEXT,
    broker_sell_order_id TEXT,
    exit_price           REAL,
    pnl                  REAL,
    fees                 REAL,
    net_pnl              REAL,
    exit_reason          TEXT,
    exit_attempts        INTEGER NOT NULL DEFAULT 0,
    next_exit_at         TEXT,
    created_at           TEXT NOT NULL,
    closed_at            TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS ix_trades_bot ON trades(bot_id);

CREATE TABLE IF NOT EXISTS runtime_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL,
    bot_id   TEXT,
    kind     TEXT NOT NULL,
    message  TEXT NOT NULL,
    data     TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialized_paths = set()


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def _connect(path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def conn():
    """Per-thread connection to the configured database."""
    path = config.BOT_DB_FILE
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != path:
        c = _connect(path)
        _local.conn = c
        _local.path = path
        _local.depth = 0
    if path not in _initialized_paths:
        init_db()
    return c


def reset_connection():
    """Drop this thread's connection (used by tests)."""
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
    _local.conn = None
    _local.path = None
    _local.depth = 0


@contextmanager
def tx():
    """Atomic write transaction. Nested calls join the outer transaction."""
    c = conn()
    depth = getattr(_local, "depth", 0)
    if depth:
        _local.depth = depth + 1
        try:
            yield c
        finally:
            _local.depth -= 1
        return

    c.execute("BEGIN IMMEDIATE")
    _local.depth = 1
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        _local.depth = 0


def rows(sql, params=()):
    return [dict(r) for r in conn().execute(sql, params).fetchall()]


def row(sql, params=()):
    r = conn().execute(sql, params).fetchone()
    return dict(r) if r else None


def scalar(sql, params=()):
    r = conn().execute(sql, params).fetchone()
    return r[0] if r else None


# ---- runtime key/value -------------------------------------------------

def set_runtime(key, value):
    c = conn()
    c.execute(
        "INSERT INTO runtime_state(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value), now_utc()),
    )


def get_runtime(key, default=None):
    r = row("SELECT value, updated_at FROM runtime_state WHERE key=?", (key,))
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except Exception:
        return default


def get_runtime_with_time(key):
    r = row("SELECT value, updated_at FROM runtime_state WHERE key=?", (key,))
    if not r:
        return None, None
    try:
        return json.loads(r["value"]), r["updated_at"]
    except Exception:
        return None, r["updated_at"]


# ---- events ------------------------------------------------------------

def add_event(kind, message, *, level="INFO", bot_id=None, data=None):
    try:
        conn().execute(
            "INSERT INTO events(ts, level, bot_id, kind, message, data) VALUES(?,?,?,?,?,?)",
            (now_utc(), level, bot_id, kind, message[:1000],
             json.dumps(data, default=str) if data is not None else None),
        )
    except Exception:
        log.exception("could not store event")


# ---- schema + migration ------------------------------------------------

def init_db():
    path = config.BOT_DB_FILE
    with _init_lock:
        if path in _initialized_paths:
            return
        c = getattr(_local, "conn", None)
        if c is None or getattr(_local, "path", None) != path:
            c = _connect(path)
            _local.conn = c
            _local.path = path
            _local.depth = 0
        c.executescript(SCHEMA)
        _add_missing_columns(c)
        c.execute(
            "INSERT OR IGNORE INTO runtime_state(key, value, updated_at) VALUES('schema_version', ?, ?)",
            (json.dumps(SCHEMA_VERSION), now_utc()),
        )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _initialized_paths.add(path)
    migrate_from_json()


# Columns added after the first release: (table, column, definition)
LATER_COLUMNS = [
    ("bots", "qty_random_pct", "REAL NOT NULL DEFAULT 10"),
    # Batch buying: buy up to batch_size entries, pause, and after a sell wait
    # for the price to fall rebuy_dip_pct below that sell before the next batch.
    ("bots", "batch_size", "INTEGER NOT NULL DEFAULT 0"),
    ("bots", "rebuy_dip_pct", "REAL NOT NULL DEFAULT 0"),
    ("bots", "batch_no", "INTEGER NOT NULL DEFAULT 1"),
    ("bots", "batch_state", "TEXT NOT NULL DEFAULT 'BUYING'"),
    ("bots", "rebuy_ref_price", "REAL"),
    ("bots", "rebuy_trigger_price", "REAL"),
    ("orders", "batch_no", "INTEGER"),
]


def _add_missing_columns(c):
    for table, column, definition in LATER_COLUMNS:
        existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _legacy_status(status):
    status = str(status or "READY").upper()
    if status == "ACTION_REQUIRED":
        # The old LIVE flow only ever parked an order for manual action;
        # nothing was sent to the broker, so the bot simply restarts stopped.
        return "STOPPED"
    if status in {"RUNNING", "STOPPED", "READY"}:
        return status
    return "STOPPED"


def migrate_from_json(json_path=None, *, force=False):
    """One-time import of /root/iifl/data/bots.json. Safe to call repeatedly."""
    json_path = json_path or config.BOT_DATA_FILE
    if not force and get_runtime("json_migrated"):
        return {"status": "already_migrated"}
    if not os.path.exists(json_path):
        set_runtime("json_migrated", {"at": now_utc(), "source": None})
        return {"status": "no_json_file"}

    with open(json_path, "r") as f:
        data = json.load(f)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{json_path}.pre-sqlite-{stamp}.bak"
    shutil.copy2(json_path, backup)
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass

    bots_in = data.get("bots", []) or []
    trades_in = data.get("trades", []) or []
    ts = now_utc()
    added_bots = 0
    added_trades = 0

    with tx() as c:
        for b in bots_in:
            bot_id = b.get("bot_id")
            if not bot_id or not b.get("instrumentId"):
                continue
            cur = c.execute(
                """INSERT OR IGNORE INTO bots(bot_id, symbol, tradingSymbol, instrumentId, exchange,
                       tick_size, mode, qty, target, timeframe, product, maxpos, capital, status,
                       last_entry_slot, last_tick, last_error, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    bot_id,
                    str(b.get("symbol", "")).upper(),
                    b.get("tradingSymbol"),
                    str(b.get("instrumentId")),
                    b.get("exchange", "NSEEQ"),
                    float(b.get("tick_size") or 0.05),
                    str(b.get("mode", "PAPER")).upper(),
                    int(b.get("qty", 1)),
                    float(b.get("target", 1.0)),
                    str(b.get("timeframe", "15m")).lower(),
                    str(b.get("product", "DELIVERY")).upper(),
                    int(b.get("maxpos", 10)),
                    float(b.get("capital", 100000)),
                    _legacy_status(b.get("status")),
                    b.get("last_entry_slot"),
                    b.get("last_tick"),
                    b.get("last_error"),
                    b.get("created_at") or ts,
                    ts,
                ),
            )
            added_bots += cur.rowcount

        for t in trades_in:
            qty = int(t.get("quantity", t.get("qty", 0)) or 0)
            if qty <= 0 or t.get("entry_price") is None:
                continue
            status = str(t.get("status", "OPEN")).upper()
            status = "OPEN" if status == "OPEN" else "CLOSED"
            cur = c.execute(
                """INSERT OR IGNORE INTO trades(id, bot_id, symbol, side, quantity, entry_price,
                       target_price, mode, product, timeframe, status, broker_sell_order_id,
                       exit_price, pnl, exit_reason, created_at, closed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(t["id"]) if t.get("id") is not None else None,
                    t.get("bot_id"),
                    str(t.get("symbol", "")).upper(),
                    str(t.get("side", "BUY")).upper(),
                    qty,
                    float(t["entry_price"]),
                    float(t.get("target_price") or 0),
                    str(t.get("mode", "PAPER")).upper(),
                    t.get("product"),
                    t.get("timeframe"),
                    status,
                    t.get("broker_sell_order_id"),
                    t.get("exit_price"),
                    t.get("pnl"),
                    "TARGET" if status == "CLOSED" else None,
                    t.get("created_at") or ts,
                    t.get("closed_at"),
                ),
            )
            added_trades += cur.rowcount

        result = {
            "at": ts,
            "source": json_path,
            "backup": backup,
            "bots": added_bots,
            "trades": added_trades,
        }
        set_runtime("json_migrated", result)
        add_event("MIGRATION", f"Imported {added_bots} bots and {added_trades} trades from JSON",
                  data=result)

    log.info("json migration done bots=%s trades=%s backup=%s", added_bots, added_trades, backup)
    return {"status": "migrated", **result}
