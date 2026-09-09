from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_PRODUCT_URL = (
    "https://www.mediamarkt.es/es/product/_consola-nintendo-switch-2-edicion-"
    "zelda-40-aniversario-79-full-hd-hdr-120-hz-256-gb-magnetic-joy-con-2-con-"
    "modo-raton-bateria-extraible-1674231.html"
)
DEFAULT_PRODUCT_NAME = "Nintendo Switch 2 - Edicion Zelda 40 Aniversario"


@dataclass(frozen=True)
class StockConfig:
    product_url: str
    product_name: str
    database_path: Path
    telegram_bot_token: str | None
    telegram_chat_id: str | None


class JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.documents: list[str] = []
        self.reading_json_ld = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        content_type = attributes.get("type", "").lower()
        if tag == "script" and content_type.startswith("application/ld+json"):
            self.reading_json_ld = True
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.reading_json_ld:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.reading_json_ld:
            self.documents.append("".join(self.parts))
            self.reading_json_ld = False
            self.parts = []


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


def load_config() -> StockConfig:
    load_dotenv()
    config = StockConfig(
        product_url=os.getenv("STOCK_PRODUCT_URL", DEFAULT_PRODUCT_URL),
        product_name=os.getenv("STOCK_PRODUCT_NAME", DEFAULT_PRODUCT_NAME),
        database_path=Path(os.getenv("STOCK_DATABASE_PATH", "data/stock-status.db")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    )
    validate_config(config)
    return config


def validate_config(config: StockConfig) -> None:
    parsed_url = urlparse(config.product_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc.endswith("mediamarkt.es"):
        raise ValueError("STOCK_PRODUCT_URL debe ser una URL HTTPS de MediaMarkt")
    if not config.product_name.strip():
        raise ValueError("STOCK_PRODUCT_NAME no puede estar vacio")
    if bool(config.telegram_bot_token) != bool(config.telegram_chat_id):
        raise ValueError("TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID deben definirse juntos")


def fetch_product_page(product_url: str) -> str:
    request = Request(
        product_url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; StockNotifier/1.0)",
            "Accept-Language": "es-ES,es;q=0.9",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as error:
        raise RuntimeError(f"MediaMarkt respondio con HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError("No se pudo consultar la ficha de MediaMarkt") from error


def get_json_ld_documents(page: str) -> list[Any]:
    parser = JsonLdParser()
    parser.feed(page)
    documents = []
    for raw_document in parser.documents:
        try:
            documents.append(json.loads(raw_document))
        except json.JSONDecodeError:
            continue
    return documents


def values_for_key(data: Any, key: str) -> list[Any]:
    values = []
    if isinstance(data, dict):
        for current_key, value in data.items():
            if current_key.lower() == key.lower():
                values.append(value)
            values.extend(values_for_key(value, key))
    elif isinstance(data, list):
        for value in data:
            values.extend(values_for_key(value, key))
    return values


def json_ld_availability(page: str) -> bool | None:
    availability_values = []
    for document in get_json_ld_documents(page):
        availability_values.extend(values_for_key(document, "availability"))

    normalized_values = {
        str(value).lower().replace(" ", "") for value in availability_values
    }
    if any("instock" in value for value in normalized_values):
        return True
    unavailable_values = ("outofstock", "preorder", "discontinued", "soldout")
    if any(marker in value for marker in unavailable_values for value in normalized_values):
        return False
    return None


def visible_text(page: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", page)
    text = html.unescape(without_tags).lower()
    text = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode()
    return " ".join(text.split())


def page_availability(page: str) -> bool | None:
    text = visible_text(page)
    unavailable_markers = (
        "disponible proximamente",
        "crear alerta de disponibilidad",
        "agotado",
        "sin stock",
        "no disponible online",
        "precompra",
        "pre-compra",
    )
    if any(marker in text for marker in unavailable_markers):
        return False

    structured_status = json_ld_availability(page)
    if structured_status is not None:
        return structured_status

    available_markers = (
        "anadir al carrito",
        "comprar ahora",
        "disponible online",
    )
    if any(marker in text for marker in available_markers):
        return True
    return None


def check_stock(config: StockConfig) -> bool:
    page = fetch_product_page(config.product_url)
    is_available = page_availability(page)
    if is_available is None:
        raise RuntimeError("No se pudo determinar el estado de stock de la ficha")
    return is_available


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            product_url TEXT NOT NULL,
            is_available INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_check_id INTEGER NOT NULL UNIQUE,
            sent_at TEXT NOT NULL,
            FOREIGN KEY (stock_check_id) REFERENCES stock_checks(id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stock_checks_product_time
        ON stock_checks(product_url, checked_at)
        """
    )
    return connection


def get_previous_availability(
    connection: sqlite3.Connection,
    product_url: str,
) -> bool | None:
    row = connection.execute(
        """
        SELECT is_available
        FROM stock_checks
        WHERE product_url = ?
        ORDER BY id DESC LIMIT 1
        """,
        (product_url,),
    ).fetchone()
    if row is None:
        return None
    return bool(row["is_available"])


def save_stock_check(
    connection: sqlite3.Connection,
    config: StockConfig,
    checked_at: datetime,
    is_available: bool,
) -> int:
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO stock_checks (checked_at, product_url, is_available)
            VALUES (?, ?, ?)
            """,
            (checked_at.isoformat(), config.product_url, int(is_available)),
        )
    return int(cursor.lastrowid)


def mark_notification(
    connection: sqlite3.Connection,
    stock_check_id: int,
    sent_at: datetime,
) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO stock_notifications (stock_check_id, sent_at)
            VALUES (?, ?)
            """,
            (stock_check_id, sent_at.isoformat()),
        )


def format_message(config: StockConfig, is_available: bool, forced: bool = False) -> str:
    if forced:
        heading = "Estado actual"
    elif is_available:
        heading = "Stock disponible"
    else:
        heading = "Sin stock"
    status = "Disponible" if is_available else "No disponible"
    return "\n".join(
        [
            heading,
            config.product_name,
            f"Estado en MediaMarkt: {status}",
            config.product_url,
        ]
    )


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
    config: StockConfig,
    no_notify: bool = False,
    force_notify: bool = False,
) -> bool:
    checked_at = datetime.now(timezone.utc)
    is_available = check_stock(config)
    connection = connect_database(config.database_path)

    try:
        previous_availability = get_previous_availability(connection, config.product_url)
        stock_check_id = save_stock_check(
            connection,
            config,
            checked_at,
            is_available,
        )
        should_notify = force_notify or (
            is_available and previous_availability is not True
        )
        message = format_message(config, is_available, forced=force_notify)
        print(message)

        if should_notify and not no_notify and config.telegram_bot_token and config.telegram_chat_id:
            send_telegram(config.telegram_bot_token, config.telegram_chat_id, message)
            mark_notification(connection, stock_check_id, checked_at)
            print("Aviso enviado por Telegram")
        elif should_notify and no_notify:
            print("Aviso omitido por --no-notify")
        elif should_notify:
            print("Aviso no enviado: faltan las credenciales de Telegram")
        else:
            print("Sin stock nuevo")
        return is_available
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Avisador de stock de MediaMarkt")
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="consulta y guarda el estado sin enviar Telegram",
    )
    parser.add_argument(
        "--force-notify",
        action="store_true",
        help="envia el estado actual aunque no haya vuelto el stock",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        run(
            load_config(),
            no_notify=args.no_notify,
            force_notify=args.force_notify,
        )
        return 0
    except (ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
