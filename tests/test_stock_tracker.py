import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from stock_tracker import (
    StockConfig,
    connect_database,
    format_message,
    get_previous_availability,
    page_availability,
    save_stock_check,
)


def sample_config(database_path: Path) -> StockConfig:
    return StockConfig(
        product_url="https://www.mediamarkt.es/es/product/_switch-2.html",
        product_name="Nintendo Switch 2",
        database_path=database_path,
        telegram_bot_token=None,
        telegram_chat_id=None,
    )


class PageAvailabilityTests(unittest.TestCase):
    def test_detects_stock_from_structured_data(self) -> None:
        page = '''
        <script type="application/ld+json">
        {"@type": "Product", "offers": {"availability": "https://schema.org/InStock"}}
        </script>
        '''

        self.assertTrue(page_availability(page))

    def test_detects_coming_soon_as_unavailable(self) -> None:
        page = '''
        <script type="application/ld+json">
        {"offers": {"availability": "https://schema.org/InStock"}}
        </script>
        <p>Disponible próximamente</p><button>Crear alerta de disponibilidad</button>
        '''

        self.assertFalse(page_availability(page))

    def test_rejects_an_ambiguous_page(self) -> None:
        self.assertIsNone(page_availability("<p>Ficha de producto</p>"))


class StockDatabaseTests(unittest.TestCase):
    def test_saves_and_reads_the_latest_stock_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = sample_config(Path(directory) / "stock.db")
            connection = connect_database(config.database_path)
            checked_at = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)

            self.assertIsNone(get_previous_availability(connection, config.product_url))
            save_stock_check(connection, config, checked_at, False)
            save_stock_check(connection, config, checked_at, True)

            self.assertTrue(get_previous_availability(connection, config.product_url))
            connection.close()

    def test_forced_message_shows_the_current_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            message = format_message(
                sample_config(Path(directory) / "stock.db"),
                False,
                forced=True,
            )

            self.assertIn("Estado actual", message)
            self.assertIn("No disponible", message)

    def test_unavailable_message_does_not_claim_there_is_stock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            message = format_message(
                sample_config(Path(directory) / "stock.db"),
                False,
            )

            self.assertIn("Sin stock", message)


if __name__ == "__main__":
    unittest.main()
