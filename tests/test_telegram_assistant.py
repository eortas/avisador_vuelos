import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from flight_tracker import Config, Quote, connect_database, save_run
from telegram_assistant import (
    AssistantConfig,
    answer_question,
    build_messages,
    load_conversation,
    load_flight_context,
    prepare_assistant_database,
)


def sample_config(database_path: Path) -> Config:
    return Config(
        origin="KRK",
        destination="BIO",
        departure_start=date(2026, 9, 17),
        departure_end=date(2026, 9, 18),
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
        telegram_bot_token="telegram-token",
        telegram_chat_id="123",
    )


def assistant_config() -> AssistantConfig:
    return AssistantConfig(
        mistral_api_key="mistral-key",
        mistral_model="mistral-small-latest",
        poll_timeout=30,
        history_messages=8,
    )


class TelegramAssistantTests(unittest.TestCase):
    def create_database(self, directory: str):
        database_path = Path(directory) / "prices.db"
        config = sample_config(database_path)
        connection = connect_database(database_path)
        prepare_assistant_database(connection)
        save_run(
            connection,
            config,
            datetime(2026, 8, 18, 10, tzinfo=timezone.utc),
            [
                Quote(date(2026, 9, 17), 45, "Airline A", 1),
                Quote(date(2026, 9, 18), 39, "Airline B", 0),
            ],
        )
        return connection, config

    def test_context_contains_only_configured_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection, config = self.create_database(directory)
            other_config = Config(
                **{
                    **config.__dict__,
                    "origin": "MAD",
                    "destination": "LHR",
                }
            )
            save_run(
                connection,
                other_config,
                datetime(2026, 8, 18, 11, tzinfo=timezone.utc),
                [Quote(date(2026, 9, 17), 20, "Other Airline", 0)],
            )

            context = load_flight_context(connection, config)
            connection.close()

            self.assertEqual(context["route"], "KRK -> BIO")
            self.assertEqual(context["current_minimum"]["price"], 39)
            self.assertNotIn("MAD", str(context))
            self.assertNotIn("Other Airline", str(context))

    def test_prompt_limits_answers_to_configured_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection, config = self.create_database(directory)
            context = load_flight_context(connection, config)
            connection.close()

            messages = build_messages(config, context, [], "Que vuelo es mas barato?")

            self.assertIn("Solo puedes responder", messages[0]["content"])
            self.assertIn("KRK -> BIO", messages[0]["content"])
            self.assertIn('"price":39.0', messages[1]["content"])

    def test_answer_uses_context_and_saves_short_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection, config = self.create_database(directory)
            received_messages = []

            def fake_completion(_, messages):
                received_messages.extend(messages)
                return "El minimo es 39 EUR el 18/09/2026."

            answer = answer_question(
                connection,
                config,
                assistant_config(),
                "123",
                "Cual es el minimo?",
                completion=fake_completion,
            )
            conversation = load_conversation(connection, "123", 8)
            connection.close()

            self.assertEqual(answer, "El minimo es 39 EUR el 18/09/2026.")
            self.assertIn('"price":39.0', received_messages[1]["content"])
            self.assertEqual(
                conversation,
                [
                    {"role": "user", "content": "Cual es el minimo?"},
                    {
                        "role": "assistant",
                        "content": "El minimo es 39 EUR el 18/09/2026.",
                    },
                ],
            )

    def test_status_does_not_call_mistral(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection, config = self.create_database(directory)

            def unexpected_completion(_, __):
                raise AssertionError("Mistral no debe usarse para /estado")

            answer = answer_question(
                connection,
                config,
                assistant_config(),
                "123",
                "/estado",
                completion=unexpected_completion,
            )
            connection.close()

            self.assertIn("Minimo actual: 39 EUR", answer)
            self.assertIn("KRK -> BIO", answer)


if __name__ == "__main__":
    unittest.main()
