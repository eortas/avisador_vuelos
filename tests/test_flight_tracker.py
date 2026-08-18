import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from flight_tracker import (
    Config,
    Quote,
    analyse_price,
    connect_database,
    format_amount,
    generate_departure_dates,
    get_previous_prices,
    save_run,
)


def sample_config(database_path: Path = Path("data/test.db")) -> Config:
    return Config(
        origin="KRK",
        destination="BIO",
        departure_start=date(2026, 9, 10),
        departure_end=date(2026, 9, 12),
        max_stops=1,
        currency="EUR",
        alert_percent=8,
        trend_hours=72,
        trend_points=3,
        request_delay_seconds=0,
        max_dates=31,
        database_path=database_path,
        telegram_bot_token=None,
        telegram_chat_id=None,
    )


class DepartureDateTests(unittest.TestCase):
    def test_generates_every_date_in_the_window(self) -> None:
        departure_dates = generate_departure_dates(sample_config())

        self.assertEqual(
            departure_dates,
            [
                date(2026, 9, 10),
                date(2026, 9, 11),
                date(2026, 9, 12),
            ],
        )

    def test_rejects_too_many_dates(self) -> None:
        config = replace(sample_config(), max_dates=2)

        with self.assertRaisesRegex(ValueError, "MAX_DATES"):
            generate_departure_dates(config)


class PriceAnalysisTests(unittest.TestCase):
    def test_first_run_requires_notification(self) -> None:
        analysis = analyse_price(180, None, None, [], 8, 3)

        self.assertTrue(analysis.should_notify)

    def test_detects_new_historical_minimum(self) -> None:
        analysis = analyse_price(150, 170, 160, [165, 170], 8, 3)

        self.assertTrue(analysis.is_new_historical_min)
        self.assertFalse(analysis.is_significant_rise)

    def test_detects_significant_rise(self) -> None:
        analysis = analyse_price(200, 180, 150, [175, 180], 8, 3)

        self.assertTrue(analysis.is_significant_rise)
        self.assertAlmostEqual(analysis.change_percent, 11.11, places=2)

    def test_detects_sustained_rise(self) -> None:
        analysis = analyse_price(180, 170, 140, [160, 170], 8, 3)

        self.assertTrue(analysis.is_sustained_rise)

    def test_formats_whole_and_decimal_prices(self) -> None:
        self.assertEqual(format_amount(144), "144")
        self.assertEqual(format_amount(144.5), "144.50")


class DatabaseTests(unittest.TestCase):
    def test_saves_run_and_all_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "prices.db"
            config = sample_config(database_path)
            connection = connect_database(database_path)
            checked_at = datetime(2026, 8, 18, 10, tzinfo=timezone.utc)
            quotes = [
                Quote(date(2026, 9, 10), 190, "Airline A", 1),
                Quote(date(2026, 9, 11), 175, "Airline B", 0),
            ]

            cheapest = save_run(connection, config, checked_at, quotes)
            previous, historical, recent = get_previous_prices(
                connection, config, checked_at
            )
            quote_count = connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
            connection.close()

            self.assertEqual(cheapest.price, 175)
            self.assertEqual(previous, 175)
            self.assertEqual(historical, 175)
            self.assertEqual(recent, [175])
            self.assertEqual(quote_count, 2)


if __name__ == "__main__":
    unittest.main()
