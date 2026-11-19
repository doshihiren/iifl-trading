"""Unit tests for isolated Camarilla RSI backtest."""
import unittest
from datetime import datetime, timedelta
from camarilla_rsi import aggregate, pivots, rsi21, simulate


def minutes(days=4, price=100.0):
    result = []
    for day in range(days):
        start = datetime(2026, 11, 2 + day, 9, 15)
        for minute in range(375):
            ts = (start + timedelta(minutes=minute)).strftime("%Y-%m-%d %H:%M")
            result.append({"ts": ts, "open": price, "high": price+2,
                           "low": price-2, "close": price})
    return result


class CamarillaTests(unittest.TestCase):
    def test_aggregation(self):
        bars = aggregate(minutes(2))
        self.assertEqual(len(bars), 50)
        self.assertEqual(bars[0]["ts"], "2026-11-02 09:15")
        self.assertEqual(bars[-1]["ts"], "2026-11-03 15:15")

    def test_incomplete_candles_not_used(self):
        self.assertEqual(len(aggregate(minutes(1)[:-1])), 24)

    def test_pivots_only_use_previous_session(self):
        levels = pivots(aggregate(minutes(3)))
        self.assertNotIn("2026-11-02", levels)
        day = levels["2026-11-03"]
        self.assertAlmostEqual(day["R4"], 102.2)
        self.assertAlmostEqual(day["S4"], 97.8)
        self.assertAlmostEqual(day["R2"], 100 + 4.4/6)

    def test_flat_rsi_is_neutral(self):
        rsi = rsi21([{"close": 100} for _ in range(50)])
        self.assertTrue(all(x is None for x in rsi[:21]))
        self.assertEqual(rsi[21], 50)

    def test_no_false_wins_on_flat_data(self):
        result = simulate(minutes(4))
        self.assertEqual(result["summary"]["closed_trades"], 0)
        self.assertIsNone(result["summary"]["win_rate_pct"])

    def test_validation(self):
        with self.assertRaises(ValueError):
            simulate(minutes(), tolerance=-1)


if __name__ == "__main__":
    unittest.main()
