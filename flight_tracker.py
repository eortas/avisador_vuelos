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
    trend_hours: int
    trend_points: int
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
    historical_min: float | None
    change_percent: float | None
    is_new_historical_min: bool
    is_significant_rise: bool
    is_sustained_rise: bool

    @property
    def should_notify(self) -> bool:
        return (
            self.previous_price is None
            or self.is_new_historical_min
            or self.is_significant_rise
            or self.is_sustained_rise
        )


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
        alert_percent=float(os.getenv("ALERT_PERCENT", "8")),
        trend_hours=int(os.getenv("TREND_HOURS", "72")),
        trend_points=int(os.getenv("TREND_POINTS", "3")),
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
    if config.alert_percent < 0:
        raise ValueError("ALERT_PERCENT no puede ser negativo")
    if config.trend_points < 2:
        raise ValueError("TREND_POINTS debe ser al menos 2")
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


def get_previous_prices(
    connection: sqlite3.Connection,
    config: Config,
    checked_at: datetime,
) -> tuple[float | None, float | None, list[float]]:
    route = (config.origin, config.destination)
    previous_row = connection.execute(
        """
        SELECT min_price FROM runs
        WHERE origin = ? AND destination = ? AND trip_type = 'one-way'
        ORDER BY checked_at DESC LIMIT 1
        """,
        route,
    ).fetchone()
    historical_row = connection.execute(
        """
        SELECT MIN(min_price) AS min_price FROM runs
        WHERE origin = ? AND destination = ? AND trip_type = 'one-way'
        """,
        route,
    ).fetchone()
    cutoff = (checked_at - timedelta(hours=config.trend_hours)).isoformat()
    recent_rows = connection.execute(
        """
        SELECT min_price FROM runs
        WHERE origin = ? AND destination = ? AND trip_type = 'one-way'
          AND checked_at >= ?
        ORDER BY checked_at DESC LIMIT ?
        """,
        (*route, cutoff, config.trend_points - 1),
    ).fetchall()

    previous = previous_row["min_price"] if previous_row else None
    historical = historical_row["min_price"] if historical_row else None
    recent = [row["min_price"] for row in reversed(recent_rows)]
    return previous, historical, recent


def analyse_price(
    current_price: float,
    previous_price: float | None,
    historical_min: float | None,
    recent_prices: list[float],
    alert_percent: float,
    trend_points: int,
) -> PriceAnalysis:
    change_percent = None
    if previous_price:
        change_percent = (current_price - previous_price) / previous_price * 100

    trend = [*recent_prices, current_price]
    sustained = (
        len(trend) >= trend_points
        and all(previous < current for previous, current in zip(trend, trend[1:]))
        and (trend[-1] - trend[0]) / trend[0] * 100 >= alert_percent
    )
    return PriceAnalysis(
        previous_price=previous_price,
        historical_min=historical_min,
        change_percent=change_percent,
        is_new_historical_min=historical_min is not None and current_price < historical_min,
        is_significant_rise=change_percent is not None and change_percent >= alert_percent,
        is_sustained_rise=sustained,
    )


def save_run(
    connection: sqlite3.Connection,
    config: Config,
    checked_at: datetime,
    quotes: list[Quote],
) -> Quote:
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
    return cheapest


def format_message(
    config: Config,
    quote: Quote,
    analysis: PriceAnalysis,
    forced: bool = False,
) -> str:
    if forced:
        heading = "Estado actual"
    elif analysis.previous_price is None:
        heading = "Seguimiento iniciado"
    elif analysis.is_new_historical_min:
        heading = "Nuevo minimo historico"
    elif analysis.is_sustained_rise:
        heading = "El precio lleva varias consultas subiendo"
    else:
        heading = "El precio minimo ha subido"

    lines = [
        f"{config.origin} -> {config.destination}",
        heading,
        "",
        f"Minimo actual: {format_amount(quote.price)} {config.currency}",
    ]
    if analysis.previous_price is not None and analysis.change_percent is not None:
        lines.append(
            f"Anterior: {format_amount(analysis.previous_price)} {config.currency} "
            f"({analysis.change_percent:+.1f} %)"
        )
    if analysis.historical_min is not None:
        best_historical = min(analysis.historical_min, quote.price)
        lines.append(
            f"Minimo historico: {format_amount(best_historical)} {config.currency}"
        )

    lines.extend(
        [
            "",
            "Mejor salida:",
            quote.departure_date.strftime("%d/%m/%Y"),
            quote.airlines or "Compania no indicada",
        ]
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
        previous, historical, recent = get_previous_prices(connection, config, checked_at)
        cheapest = min(quotes, key=lambda quote: quote.price)
        analysis = analyse_price(
            current_price=cheapest.price,
            previous_price=previous,
            historical_min=historical,
            recent_prices=recent,
            alert_percent=config.alert_percent,
            trend_points=config.trend_points,
        )
        save_run(connection, config, checked_at, quotes)
    finally:
        connection.close()

    should_notify = analysis.should_notify or force_notify
    message = format_message(
        config,
        cheapest,
        analysis,
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
        print("Aviso enviado por Telegram")
    elif should_notify and no_notify:
        print("Aviso omitido por --no-notify")
    elif should_notify and not no_notify:
        print("Aviso no enviado: faltan las credenciales de Telegram")
    else:
        print("Sin cambios que requieran aviso")
    return cheapest


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
