# Deploying the automated LIVE engine

Everything below is copy-paste for the VPS (`ssh root@your-vps`).

## What changed

| Before | Now |
|---|---|
| LIVE bots only set `ACTION_REQUIRED`; nothing reached IIFL | LIVE bots place BUY/SELL market orders automatically |
| Trade opened at the quote price | Trade opens only after IIFL reports the fill, at the **actual average fill price** |
| JSON file shared by two processes | SQLite (`/root/iifl/data/iifl.db`, WAL mode, transactions) |
| Restart could repeat an order | Every order has a unique key (`bot_id|slot|BUY`, `LOT<id>|SELL|n`); unclear orders are matched in the IIFL order book, never re-sent |
| Static "API CONNECTED" badge | Live badges: IIFL session, worker, market, LIVE switch |
| Stop = everything stops | **Stop New Entries** keeps selling open positions at target. **Exit & Stop** sells them at market |
| Weekday 09:15–15:30 only | NSE 2026 holidays + stale-quote check |
| Flask dev server | Gunicorn under systemd (same port, Nginx untouched) |

IIFL endpoints used: `POST/GET /v1/orders`, `GET /v1/trades`, `GET /v1/positions`, `GET /v1/holdings`, `GET /v1/profile`.

## 1. Deploy (do this with LIVE still off)

```bash
cd /root/iifl
source venv/bin/activate

# Backups: data folder + current service files
cp -a /root/iifl/data /root/iifl-data-backup-$(date +%Y%m%d-%H%M)
cp /etc/systemd/system/iifl-web.service /root/iifl-web.service.bak 2>/dev/null
cp /etc/systemd/system/iifl-bot.service /root/iifl-bot.service.bak 2>/dev/null

# Stop the worker during the upgrade
systemctl stop iifl-bot

git pull --ff-only origin main
pip install -r requirements.txt

# Checks
python -m py_compile *.py
python -m unittest tests.test_engine

# One-time migration bots.json -> SQLite (backs up bots.json first, safe to repeat)
python tools.py migrate
python tools.py refresh-ticks
python tools.py status

# New service files (Gunicorn for the web app)
cp deploy/iifl-web.service deploy/iifl-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart iifl-web
systemctl restart iifl-bot

# Verify
systemctl --no-pager status iifl-web iifl-bot | grep -E "Active|Main PID"
curl -s -o /dev/null -w "web HTTP %{http_code}\n" http://127.0.0.1:5015/iifl/auth/login
journalctl -u iifl-bot -n 30 --no-pager
```

If `git pull` complains about local changes: `git stash` then pull again.

Open the dashboard. Your PAPER bots should look and behave exactly as before.

## 2. Read-only broker check (no orders)

Log in to IIFL from the dashboard, then:

```bash
cd /root/iifl && source venv/bin/activate
python tools.py broker-check
```

This only reads your order book, trade book, positions and holdings, to confirm the field names the engine relies on. Paste the output to Claude (it contains no passwords or tokens).

## 3. First LIVE test: 1 share

Edit `/root/iifl/.env` (`nano /root/iifl/.env`) and make sure these lines exist:

```
LIVE_TRADING=true
IIFL_TRADING_IP_AUTHORIZED=true
```

```bash
systemctl restart iifl-web iifl-bot
```

On the dashboard add a bot: **SBC, 15m, qty 1, target 0.5%, max positions 1, max capital 200, DELIVERY, LIVE** → Start.

Watch for, in order:
1. LIVE Orders panel: `SUBMITTED` with an IIFL order id
2. Then `FILLED` with the fill price, and a new OPEN row in Trading Reports with Entry fill = IIFL's average price
3. IIFL app shows 1 SBC
4. Restart test: `systemctl restart iifl-bot`. No second BUY may appear for the same slot.
5. Either wait for the target, or press **Exit & Stop**: a SELL appears, fills, and the row closes with P&L.

Follow the worker live: `journalctl -u iifl-bot -f`

Only after this works, add your real-size bots.

## Order size

Each entry uses the bot's qty ± its "Qty random %" (default 10%). Example: qty 125 → every order is a random whole number from 113 to 137. Set the random % to 0 for an exact qty. The bot's max capital and `GLOBAL_MAX_CAPITAL` are the size limits; `LIVE_MAX_QTY_PER_ORDER` is no longer used.

## Batch buying (optional, per bot)

- **Max buys per batch** (0 = off): the bot buys on each time slot until it has made this many buys, then pauses new buys.
- After any SELL of that bot fills, it watches the live price. When the price is **Re-buy drop %** below that sell price, a new batch starts and it buys again, up to the same count. The first buy of the new batch happens immediately.
- If another sell fills while waiting, the re-buy level moves to that latest sell price.
- Max positions, capital limits, targets and exits work exactly as before.
- Pressing **Start** on a stopped bot begins a fresh batch. **Edit** changes settings without removing the bot (new values apply to new entries).

## Reports

Dashboard → **Reports** (or `/iifl/reports`): date range presets, LIVE/PAPER filter, script, bot, and grouping by day, week, month, script, bot, mode, timeframe, product or exit reason. Daily and cumulative P&L charts, LIVE vs PAPER comparison, open positions with unrealized P&L, and CSV download.

## Daily routine

IIFL ends the API session every night. **Each trading morning before 09:15, open the dashboard and click "Login to IIFL".** The badge turns green (`IIFL: CONNECTED`) and the bots run on their own all day.

## Useful commands

```bash
python tools.py status                    # bots, open lots, pending orders
journalctl -u iifl-bot -f                 # live worker log
journalctl -u iifl-bot --since today | grep -E "ORDER_|FILLED|MISMATCH|ERROR"
```

## Safety switches

- `LIVE_TRADING=false` in `.env` + `systemctl restart iifl-bot` stops all LIVE orders (entries and exits).
- If IIFL shows fewer shares than the bots hold for a symbol, new entries for it pause and the dashboard shows the mismatch.
- INTRADAY bots stop entering at 15:00 and sell their positions at 15:10.
- Holidays: built in for 2026. For 2027 add `/root/iifl/data/holidays.json` like `{"holidays": ["2027-01-26"]}`; without it the bot will not open new positions in 2027.

## Before real money

Rotate the IIFL **API secret** and the **dashboard password** (they were exposed during development), update `.env`, restart both services.
