"""Trading worker (run by iifl-bot.service).

Runs the engine every BOT_POLL_SECONDS. Only one worker may run at a time:
a second copy exits immediately instead of risking duplicate orders.
"""

import fcntl
import logging
import os
import signal
import sys
import time

from networking import force_ipv6

force_ipv6()

import config  # noqa: E402
import db  # noqa: E402
from engine import Engine  # noqa: E402

_running = True


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def acquire_lock():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = os.path.join(config.DATA_DIR, "worker.lock")
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.getLogger("iifl.worker").error("event=LOCKED another worker is already running; exiting")
        sys.exit(1)
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _stop(*_):
    global _running
    _running = False


def main():
    setup_logging()
    log = logging.getLogger("iifl.worker")
    lock = acquire_lock()  # noqa: F841  (held for the life of the process)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    db.init_db()
    engine = Engine()
    engine.startup()
    log.info("event=WORKER_READY poll=%ss live_trading=%s ip_authorized=%s max_live_qty=%s",
             config.BOT_POLL_SECONDS, config.LIVE_TRADING, config.IIFL_TRADING_IP_AUTHORIZED,
             config.LIVE_MAX_QTY_PER_ORDER)

    while _running:
        started = time.monotonic()
        try:
            engine.run_once()
        except Exception:
            log.exception("event=LOOP_ERROR")
        elapsed = time.monotonic() - started
        time.sleep(max(0.5, config.BOT_POLL_SECONDS - elapsed))

    log.info("event=WORKER_STOPPED")


if __name__ == "__main__":
    main()
