from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Config:
    origin: str
    destination: str
    departure_start: date
    departure_end: date
    max_stops: int
    currency: str
    alert_percent: float
    alert_amount: float
    sharp_rise_percent: float
    sharp_rise_amount: float
    confirmation_checks: int
    baseline_hours: int
    cooldown_hours: int
    request_delay_seconds: float
    max_dates: int
    database_path: Path
    telegram_bot_token: str | None
    telegram_chat_id: str | None


@dataclass(frozen=True)
class Quote:
    departure_date: date
    price: float
    airlines: str
    stops: int


@dataclass(frozen=True)
class PriceAnalysis:
    previous_price: float | None
    baseline_price: float | None
    historical_min: float | None
    change_percent: float | None
    is_new_historical_min: bool
    is_confirmed_rise: bool
    is_sharp_rise: bool
    is_in_cooldown: bool

    @property
    def should_notify(self) -> bool:
        return (
            self.historical_min is None
            or self.is_new_historical_min
            or self.is_sharp_rise
            or (self.is_confirmed_rise and not self.is_in_cooldown)
        )

    @property
    def notification_reason(self) -> str:
        if self.historical_min is None:
            return "tracking_started"
        if self.is_new_historical_min:
            return "historical_min"
        if self.is_sharp_rise:
            return "sharp_rise"
        return "confirmed_rise"


def load_dotenv(path: Path = Path(".env")) -> None:
    """Carga variables sencillas desde .env sin sobrescribir el entorno."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config() -> Config:
    load_dotenv()

    today = date.today()
    min_days = int(os.getenv("DEPARTURE_MIN_DAYS", "30"))
    max_days = int(os.getenv("DEPARTURE_MAX_DAYS", "60"))
    if min_days < 0 or min_days > max_days:
        raise ValueError("El rango DEPARTURE_MIN_DAYS/DEPARTURE_MAX_DAYS no es valido")

    config = Config(
        origin=os.getenv("ORIGIN", "KRK").upper(),
        destination=os.getenv("DESTINATION", "BIO").upper(),
        departure_start=today + timedelta(days=min_days),
        departure_end=today + timedelta(days=max_days),
        max_stops=int(os.getenv("MAX_STOPS", "1")),
        currency=os.getenv("CURRENCY", "EUR").upper(),
        alert_percent=float(os.getenv("ALERT_PERCENT", "10")),
        alert_amount=float(os.getenv("ALERT_AMOUNT", "5")),
        sharp_rise_percent=float(os.getenv("SHARP_RISE_PERCENT", "20")),
        sharp_rise_amount=float(os.getenv("SHARP_RISE_AMOUNT", "10")),
        confirmation_checks=int(os.getenv("CONFIRMATION_CHECKS", "2")),
        baseline_hours=int(os.getenv("BASELINE_HOURS", "24")),
        cooldown_hours=int(os.getenv("COOLDOWN_HOURS", "12")),
        request_delay_seconds=float(os.getenv("REQUEST_DELAY_SECONDS", "2")),
        max_dates=int(os.getenv("MAX_DATES", "31")),
        database_path=Path(os.getenv("DATABASE_PATH", "data/one-way-prices.db")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    )
    validate_config(config)
    return config


def validate_config(config: Config) -> None:
    if len(config.origin) != 3 or len(config.destination) != 3:
        raise ValueError("ORIGIN y DESTINATION deben ser codigos IATA de 3 letras")
    if config.departure_start > config.departure_end:
        raise ValueError("La fecha inicial no puede ser posterior a la final")
    if not 0 <= config.max_stops <= 2:
        raise ValueError("MAX_STOPS debe estar entre 0 y 2")
    if min(
        config.alert_percent,
        config.alert_amount,
        config.sharp_rise_percent,
        config.sharp_rise_amount,
    ) < 0:
        raise ValueError("Los umbrales de alerta no pueden ser negativos")
    if config.confirmation_checks < 2:
        raise ValueError("CONFIRMATION_CHECKS debe ser al menos 2")
    if config.baseline_hours < 1 or config.cooldown_hours < 0:
        raise ValueError("BASELINE_HOURS debe ser positivo y COOLDOWN_HOURS no negativo")
    if config.max_dates < 1:
        raise ValueError("MAX_DATES debe ser al menos 1")
    if bool(config.telegram_bot_token) != bool(config.telegram_chat_id):
        raise ValueError("TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID deben definirse juntos")


def dates_between(start: date, end: date) -> list[date]:
    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def generate_departure_dates(config: Config) -> list[date]:
    departure_dates = dates_between(config.departure_start, config.departure_end)
    if len(departure_dates) > config.max_dates:
        raise ValueError(
            f"Se generaron {len(departure_dates)} fechas; "
            f"el limite MAX_DATES es {config.max_dates}"
        )
    return departure_dates


def search_departure(config: Config, departure: date) -> Quote:
    # Importamos aqui para que las pruebas de logica no dependan de la red.
    from fli.models import (
        Airport,
        FlightSearchFilters,
        FlightSegment,
        MaxStops,
        PassengerInfo,
        SortBy,
        TripType,
    )
    from fli.search import SearchFlights

    stops_by_number = {
        0: MaxStops.NON_STOP,
        1: MaxStops.ONE_STOP_OR_FEWER,
        2: MaxStops.TWO_OR_FEWER_STOPS,
    }
    try:
        origin = Airport[config.origin]
        destination = Airport[config.destination]
    except KeyError as error:
        raise RuntimeError(f"Aeropuerto no reconocido: {error.args[0]}") from error

    filters = FlightSearchFilters(
        trip_type=TripType.ONE_WAY,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[origin, 0]],
                arrival_airport=[[destination, 0]],
                travel_date=departure.isoformat(),
            ),
        ],
        stops=stops_by_number[config.max_stops],
        sort_by=SortBy.CHEAPEST,
    )
    results = SearchFlights().search(
        filters,
        currency=config.currency,
        language="es",
        country="ES",
    )
    valid_results = [
        result
        for result in results or []
        if not isinstance(result, tuple) and result.price is not None
    ]
    if not valid_results:
        raise RuntimeError("Google Flights no devolvio resultados")

    cheapest = min(valid_results, key=lambda result: result.price)
    airlines = sorted(
        {
            leg.airline.value
            for leg in cheapest.legs
        }
    )
    return Quote(
        departure_date=departure,
        price=float(cheapest.price),
        airlines=", ".join(airlines),
        stops=cheapest.stops,
    )


def collect_quotes(
    config: Config,
    search: Callable[[Config, date], Quote] = search_departure,
) -> list[Quote]:
    departure_dates = generate_departure_dates(config)
    quotes = []

    for index, departure in enumerate(departure_dates, start=1):
        print(
            f"[{index}/{len(departure_dates)}] {departure.isoformat()}",
            flush=True,
        )
        try:
            quote = search(config, departure)
            quotes.append(quote)
            print(
                f"  {format_amount(quote.price)} {config.currency} - {quote.airlines}",
                flush=True,
            )
        except Exception as error:
            print(f"  Sin resultado: {error}", file=sys.stderr, flush=True)

        if index < len(departure_dates) and config.request_delay_seconds > 0:
            time.sleep(config.request_delay_seconds)

    if not quotes:
        raise RuntimeError("Ninguna fecha devolvio precios")
    return quotes


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            trip_type TEXT NOT NULL,
            min_price REAL NOT NULL,
            departure_date TEXT NOT NULL,
            return_date TEXT NOT NULL DEFAULT '',
            airlines TEXT NOT NULL,
            stops INTEGER NOT NULL,
            currency TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            departure_date TEXT NOT NULL,
            return_date TEXT NOT NULL DEFAULT '',
            price REAL NOT NULL,
            airlines TEXT NOT NULL,
            stops INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
            sent_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            price REAL NOT NULL
        );

        """
    )
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
    }
    if "trip_type" not in columns:
        connection.execute(
            "ALTER TABLE runs ADD COLUMN trip_type TEXT NOT NULL DEFAULT 'round-trip'"
        )
        connection.commit()
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_runs_route_trip_time
        ON runs(origin, destination, trip_type, checked_at)
        """
    )
    return connection


def get_price_history(
    connection: sqlite3.Connection,
    config: Config,
    checked_at: datetime,
    departure_dates: list[date],
) -> tuple[float | None, float | None, list[float], datetime | None]:
    route = (config.origin, config.destination)
    historical_row = connection.execute(
        """
        SELECT MIN(min_price) AS min_price FROM runs
        WHERE origin = ? AND destination = ? AND trip_type = 'one-way'
        """,
        route,
    ).fetchone()
    cutoff = (checked_at - timedelta(hours=config.baseline_hours)).isoformat()
    placeholders = ", ".join("?" for _ in departure_dates)
    recent_rows = connection.execute(
        f"""
        SELECT r.checked_at, MIN(q.price) AS min_price
        FROM runs AS r
        JOIN quotes AS q ON q.run_id = r.id
        WHERE r.origin = ? AND r.destination = ? AND r.trip_type = 'one-way'
          AND r.checked_at >= ?
          AND q.departure_date IN ({placeholders})
        GROUP BY r.id, r.checked_at
        ORDER BY r.checked_at
        """,
        (*route, cutoff, *(day.isoformat() for day in departure_dates)),
    ).fetchall()
    notification_row = connection.execute(
        """
        SELECT n.sent_at
        FROM notifications AS n
        JOIN runs AS r ON r.id = n.run_id
        WHERE r.origin = ? AND r.destination = ? AND r.trip_type = 'one-way'
          AND n.reason != 'forced'
        ORDER BY n.sent_at DESC LIMIT 1
        """,
        route,
    ).fetchone()

    historical = historical_row["min_price"] if historical_row else None
    recent = [row["min_price"] for row in recent_rows]
    previous = recent[-1] if recent else None
    last_notification_at = (
        datetime.fromisoformat(notification_row["sent_at"])
        if notification_row
        else None
    )
    return previous, historical, recent, last_notification_at


def analyse_price(
    current_price: float,
    previous_price: float | None,
    historical_min: float | None,
    recent_prices: list[float],
    alert_percent: float,
    alert_amount: float,
    sharp_rise_percent: float,
    sharp_rise_amount: float,
    confirmation_checks: int,
    checked_at: datetime,
    last_notification_at: datetime | None,
    cooldown_hours: int,
) -> PriceAnalysis:
    change_percent = None
    if previous_price:
        change_percent = (current_price - previous_price) / previous_price * 100

    baseline_price = min(recent_prices) if recent_prices else None

    def exceeds_threshold(price: float, baseline: float) -> bool:
        increase_percent = (price - baseline) / baseline * 100
        return increase_percent >= alert_percent and price - baseline >= alert_amount

    confirmed_prices = [
        *recent_prices[-(confirmation_checks - 1) :],
        current_price,
    ]
    confirmed_rise = (
        baseline_price is not None
        and len(confirmed_prices) == confirmation_checks
        and all(
            exceeds_threshold(price, baseline_price) for price in confirmed_prices
        )
    )
    sharp_rise = (
        previous_price is not None
        and change_percent is not None
        and change_percent >= sharp_rise_percent
        and current_price - previous_price >= sharp_rise_amount
    )
    in_cooldown = (
        last_notification_at is not None
        and checked_at - last_notification_at < timedelta(hours=cooldown_hours)
    )
    return PriceAnalysis(
        previous_price=previous_price,
        baseline_price=baseline_price,
        historical_min=historical_min,
        change_percent=change_percent,
        is_new_historical_min=historical_min is not None and current_price < historical_min,
        is_confirmed_rise=confirmed_rise,
        is_sharp_rise=sharp_rise,
        is_in_cooldown=in_cooldown,
    )


def save_run(
    connection: sqlite3.Connection,
    config: Config,
    checked_at: datetime,
    quotes: list[Quote],
) -> tuple[Quote, int]:
    cheapest = min(quotes, key=lambda quote: quote.price)
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO runs (
                checked_at, origin, destination, trip_type, min_price,
                departure_date, return_date, airlines, stops, currency
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checked_at.isoformat(),
                config.origin,
                config.destination,
                "one-way",
                cheapest.price,
                cheapest.departure_date.isoformat(),
                "",
                cheapest.airlines,
                cheapest.stops,
                config.currency,
            ),
        )
        run_id = cursor.lastrowid
        connection.executemany(
            """
            INSERT INTO quotes (
                run_id, departure_date, return_date, price, airlines, stops
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    quote.departure_date.isoformat(),
                    "",
                    quote.price,
                    quote.airlines,
                    quote.stops,
                )
                for quote in quotes
            ],
        )
    return cheapest, run_id


def mark_notification(
    connection: sqlite3.Connection,
    run_id: int,
    sent_at: datetime,
    reason: str,
    price: float,
) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO notifications (run_id, sent_at, reason, price)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, sent_at.isoformat(), reason, price),
        )


def format_message(
    config: Config,
    quote: Quote,
    analysis: PriceAnalysis,
    quotes: list[Quote] | None = None,
    forced: bool = False,
) -> str:
    if forced:
        heading = "Estado actual"
    elif analysis.historical_min is None:
        heading = "Seguimiento iniciado"
    elif analysis.is_new_historical_min:
        heading = "Nuevo minimo historico"
    elif analysis.is_sharp_rise:
        heading = "Subida brusca del precio minimo"
    else:
        heading = "Subida confirmada del precio minimo"

    lines = [
        f"{config.origin} -> {config.destination}",
        heading,
        "",
        f"Minimo actual: {format_amount(quote.price)} {config.currency}",
    ]
    if analysis.previous_price is not None and analysis.change_percent is not None:
        lines.append(
            f"Consulta anterior: {format_amount(analysis.previous_price)} {config.currency} "
            f"({analysis.change_percent:+.1f} %)"
        )
    if analysis.baseline_price is not None:
        lines.append(
            f"Minimo ultimas {config.baseline_hours} h: "
            f"{format_amount(analysis.baseline_price)} {config.currency}"
        )
    if analysis.historical_min is not None:
        best_historical = min(analysis.historical_min, quote.price)
        lines.append(
            f"Minimo historico: {format_amount(best_historical)} {config.currency}"
        )

    best_quotes = sorted(
        quotes or [quote],
        key=lambda item: (item.price, item.departure_date),
    )[:5]
    lines.extend(["", f"{len(best_quotes)} mejores salidas:"])
    for index, best_quote in enumerate(best_quotes, start=1):
        if best_quote.stops == 0:
            stops = "directo"
        elif best_quote.stops == 1:
            stops = "1 escala"
        else:
            stops = f"{best_quote.stops} escalas"
        lines.append(
            f"{index}. {best_quote.departure_date.strftime('%d/%m/%Y')} - "
            f"{format_amount(best_quote.price)} {config.currency} - {stops} - "
            f"{best_quote.airlines or 'Compania no indicada'}"
        )
    return "\n".join(lines)


def format_amount(amount: float) -> str:
    amount = float(amount)
    if amount.is_integer():
        return str(int(amount))
    return f"{amount:.2f}"


def send_telegram(token: str, chat_id: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    request = Request(url, data=data, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"Telegram respondio con HTTP {response.status}")
    except HTTPError as error:
        raise RuntimeError(f"Telegram respondio con HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError("No se pudo enviar el aviso por Telegram") from error


def run(
    config: Config,
    no_notify: bool = False,
    force_notify: bool = False,
) -> Quote:
    checked_at = datetime.now(timezone.utc)
    quotes = collect_quotes(config)
    connection = connect_database(config.database_path)

    try:
        previous, historical, recent, last_notification_at = get_price_history(
            connection,
            config,
            checked_at,
            [quote.departure_date for quote in quotes],
        )
        cheapest = min(quotes, key=lambda quote: quote.price)
        analysis = analyse_price(
            current_price=cheapest.price,
            previous_price=previous,
            historical_min=historical,
            recent_prices=recent,
            alert_percent=config.alert_percent,
            alert_amount=config.alert_amount,
            sharp_rise_percent=config.sharp_rise_percent,
            sharp_rise_amount=config.sharp_rise_amount,
            confirmation_checks=config.confirmation_checks,
            checked_at=checked_at,
            last_notification_at=last_notification_at,
            cooldown_hours=config.cooldown_hours,
        )
        cheapest, run_id = save_run(connection, config, checked_at, quotes)

        should_notify = analysis.should_notify or force_notify
        message = format_message(
            config,
            cheapest,
            analysis,
            quotes=quotes,
            forced=force_notify and not analysis.should_notify,
        )
        print("\n" + message)
        if (
            should_notify
            and not no_notify
            and config.telegram_bot_token
            and config.telegram_chat_id
        ):
            send_telegram(config.telegram_bot_token, config.telegram_chat_id, message)
            reason = analysis.notification_reason if analysis.should_notify else "forced"
            mark_notification(connection, run_id, checked_at, reason, cheapest.price)
            print("Aviso enviado por Telegram")
        elif should_notify and no_notify:
            print("Aviso omitido por --no-notify")
        elif should_notify and not no_notify:
            print("Aviso no enviado: faltan las credenciales de Telegram")
        else:
            print("Sin cambios que requieran aviso")
        return cheapest
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Avisador gratuito de precios de vuelos")
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="consulta y guarda precios sin enviar mensajes a Telegram",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="envia un mensaje de prueba sin consultar vuelos",
    )
    parser.add_argument(
        "--force-notify",
        action="store_true",
        help="envia el estado actual aunque no haya cambios relevantes",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config()
        if args.test_telegram:
            if not config.telegram_bot_token or not config.telegram_chat_id:
                raise ValueError("Faltan TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID")
            send_telegram(
                config.telegram_bot_token,
                config.telegram_chat_id,
                f"Avisador configurado\n{config.origin} -> {config.destination}",
            )
            print("Mensaje de prueba enviado por Telegram")
            return 0

        departure_dates = generate_departure_dates(config)
        print(
            f"Consultando {len(departure_dates)} fechas "
            f"para {config.origin} -> {config.destination}"
        )
        run(
            config,
            no_notify=args.no_notify,
            force_notify=args.force_notify,
        )
        return 0
    except (ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
