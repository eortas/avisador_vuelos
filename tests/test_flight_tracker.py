import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flight_tracker import (
    Config,
    Quote,
    analyse_price,
    connect_database,
    format_amount,
    format_message,
    generate_departure_dates,
    get_price_history,
    mark_notification,
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
        alert_percent=10,
        alert_amount=5,
        sharp_rise_percent=20,
        sharp_rise_amount=10,
        confirmation_checks=2,
        baseline_hours=24,
        cooldown_hours=12,
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
    checked_at = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)

    def analyse(
        self,
        current: float,
        previous: float | None,
        historical: float | None,
        recent: list[float],
        last_notification_at: datetime | None = None,
    ):
        return analyse_price(
            current_price=current,
            previous_price=previous,
            historical_min=historical,
            recent_prices=recent,
            alert_percent=10,
            alert_amount=5,
            sharp_rise_percent=20,
            sharp_rise_amount=10,
            confirmation_checks=2,
            checked_at=self.checked_at,
            last_notification_at=last_notification_at,
            cooldown_hours=12,
        )

    def test_first_run_requires_notification(self) -> None:
        analysis = self.analyse(180, None, None, [])

        self.assertTrue(analysis.should_notify)

    def test_old_history_does_not_look_like_a_first_run(self) -> None:
        analysis = self.analyse(180, None, 150, [])

        self.assertFalse(analysis.should_notify)

    def test_detects_new_historical_minimum(self) -> None:
        analysis = self.analyse(150, 170, 160, [165, 170])

        self.assertTrue(analysis.is_new_historical_min)
        self.assertFalse(analysis.is_sharp_rise)

    def test_requires_two_consecutive_high_prices(self) -> None:
        first_high_check = self.analyse(28, 23, 23, [23])
        second_high_check = self.analyse(28, 28, 23, [23, 28])

        self.assertFalse(first_high_check.is_confirmed_rise)
        self.assertTrue(second_high_check.is_confirmed_rise)
        self.assertTrue(second_high_check.should_notify)

    def test_requires_percentage_and_amount_thresholds(self) -> None:
        not_enough_euros = self.analyse(25.5, 25.5, 23, [23, 25.5])
        not_enough_percent = self.analyse(105, 105, 100, [100, 105])

        self.assertFalse(not_enough_euros.is_confirmed_rise)
        self.assertFalse(not_enough_percent.is_confirmed_rise)

    def test_detects_sharp_rise_immediately(self) -> None:
        analysis = self.analyse(35, 23, 23, [23])

        self.assertTrue(analysis.is_sharp_rise)
        self.assertTrue(analysis.should_notify)

    def test_cooldown_blocks_normal_rise_but_not_sharp_rise(self) -> None:
        recent_notification = self.checked_at - timedelta(hours=2)
        normal = self.analyse(28, 28, 23, [23, 28], recent_notification)
        sharp = self.analyse(35, 23, 23, [23], recent_notification)

        self.assertTrue(normal.is_in_cooldown)
        self.assertFalse(normal.should_notify)
        self.assertTrue(sharp.should_notify)

    def test_formats_whole_and_decimal_prices(self) -> None:
        self.assertEqual(format_amount(144), "144")
        self.assertEqual(format_amount(144.5), "144.50")

    def test_forced_message_shows_current_status(self) -> None:
        config = sample_config()
        quote = Quote(date(2026, 9, 10), 144, "Wizz Air", 0)
        analysis = self.analyse(144, 144, 100, [144])

        message = format_message(config, quote, analysis, forced=True)

        self.assertIn("Estado actual", message)


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

            cheapest, run_id = save_run(connection, config, checked_at, quotes)
            previous, historical, recent, last_notification_at = get_price_history(
                connection,
                config,
                checked_at + timedelta(hours=1),
                [quote.departure_date for quote in quotes],
            )
            mark_notification(
                connection, run_id, checked_at, "tracking_started", cheapest.price
            )
            *_, last_notification_at = get_price_history(
                connection,
                config,
                checked_at + timedelta(hours=1),
                [quote.departure_date for quote in quotes],
            )
            quote_count = connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
            connection.close()

            self.assertEqual(cheapest.price, 175)
            self.assertEqual(previous, 175)
            self.assertEqual(historical, 175)
            self.assertEqual(recent, [175])
            self.assertEqual(last_notification_at, checked_at)
            self.assertEqual(quote_count, 2)

    def test_compares_only_dates_that_remain_in_the_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "prices.db"
            config = sample_config(database_path)
            connection = connect_database(database_path)
            checked_at = datetime(2026, 8, 18, 10, tzinfo=timezone.utc)
            quotes = [
                Quote(date(2026, 9, 10), 50, "Airline A", 0),
                Quote(date(2026, 9, 11), 100, "Airline B", 0),
            ]
            save_run(connection, config, checked_at, quotes)

            previous, _, recent, _ = get_price_history(
                connection,
                config,
                checked_at + timedelta(hours=1),
                [date(2026, 9, 11), date(2026, 9, 12)],
            )
            connection.close()

            self.assertEqual(previous, 100)
            self.assertEqual(recent, [100])


if __name__ == "__main__":
    unittest.main()
