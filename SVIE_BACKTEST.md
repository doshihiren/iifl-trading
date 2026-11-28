# SVIE – SBC data extraction + 3-strategy backtest

New files (nothing existing is changed, LIVE trading is not touched):

| File | What it does |
|---|---|
| `svie_data.py` | Pulls SBC 1-minute candles from IIFL (`/v1/marketdata/historicaldata`) into the existing `candles` table + CSV; checks data quality; records live market depth |
| `svie_backtest.py` | Backtests A Buy & Hold, B RSI + VWAP, C Volume Intelligence on the same candles and ₹20 L |
| `tests/test_svie.py` | Mechanics tests (no look-ahead, fill caps, charges, cash reconciliation, loss limits) |
| `deploy/svie-depth.service` | Optional systemd service for the depth recorder |

## 1. Deploy

```bash
cd /root/iifl && source venv/bin/activate
git pull --ff-only origin main          # after you push these files
python -m py_compile svie_data.py svie_backtest.py
python -m unittest tests.test_svie
```

Log in to IIFL on the dashboard first (the scripts use the same saved session).

## 2. Extract the data

Fetch two extra weeks before 28 Sep so RSI and relative volume are warmed up:

```bash
python svie_data.py fetch --symbol SBC --instrument-id 6792 --from 2026-09-14 --to 2026-11-27
python svie_data.py check --symbol SBC --instrument-id 6792 --from 2026-09-14 --to 2026-11-27
```

`check` lists missing days and short days. If IIFL returned nothing for some days,
run `fetch` again; if it still has gaps, IIFL may limit how far back 1-minute data
goes – the backtest will then only cover the days that exist (the report shows the real range).

CSV lands in `/root/iifl/data/svie/SBC_1m_2026-09-14_2026-11-27.csv`.

## 3. Run the backtest

```bash
python svie_backtest.py --symbol-id 6792 --data-from 2026-09-14 --from 2026-09-28 --to 2026-11-27
# with a small parameter search (train first 70% of days, check on the last 30%)
python svie_backtest.py --symbol-id 6792 --data-from 2026-09-14 --from 2026-09-28 --to 2026-11-27 --sweep
```

Useful options: `--brokerage 20` (₹ per executed order – set your IIFL plan),
`--slippage 0.05` (% per fill), `--participation 10` (max % of each candle's volume),
`--params my.json` (override any value in `DEFAULTS` at the top of `svie_backtest.py`).

Output in `/root/iifl/data/svie/`: `..._report.html` (open in a browser),
`..._summary.json`, `..._A|B|C_trades.csv`, `..._A|B|C_equity.csv`.

## 4. Start collecting market depth (for future tests)

IIFL history has no order book, so depth rules can't be tested on past data.
Start recording now:

```bash
cp deploy/svie-depth.service /etc/systemd/system/ && systemctl daemon-reload
systemctl enable --now svie-depth
journalctl -u svie-depth -n 40 --no-pager   # first line shows IIFL's raw quote reply
```

The first log line prints IIFL's raw quote reply – check that bid/ask levels are in it.
Snapshots go to the `quote_snapshots` table (raw reply kept too).

## What the backtest does and does not prove

* Buy/sell volume, Volume Delta and Buy Ratio are **estimated** from each candle
  (where it closes in its range). They are not exchange aggressor data.
* Bid/ask imbalance, cancellations and spoofing-like rules are **not** tested.
* Signals use completed candles only and fill at the next candle's open + slippage;
  fills are capped by candle volume; brokerage, STT, exchange, SEBI, stamp and GST
  are charged (rates in `lab_backtest.py` – compare with one IIFL contract note).
* Two months of one stock is a short sample. Trust only settings that also hold on
  the sweep's unseen test period, then paper-trade before any live money.
