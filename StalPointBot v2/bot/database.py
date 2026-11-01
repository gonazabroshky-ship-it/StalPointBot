from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


CATALOGS = {"resources", "boosts", "arsenal"}

COLUMNS = [
    "id",
    "name",
    "category",
    "price",
    "available",
    "stock_reputation",
]

ITEM_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class DatabaseError(Exception):
    pass


class ValidationError(DatabaseError):
    pass


class VersionConflict(DatabaseError):
    pass


class ItemNotFound(DatabaseError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_search(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def positive_integer(value: str, maximum: int) -> int:
    """
    Допустимо: 10000, 10 000, 10 000.
    Недопустимо: 0, -1, 1.5, 1e3, текст.
    """
    if not isinstance(value, str):
        raise ValidationError("Введите целое положительное число.")

    normalized = (
        value.strip()
        .replace(" ", "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
    )

    if not re.fullmatch(r"[0-9]+", normalized):
        raise ValidationError(
            "Введите целое положительное число без дробной части."
        )

    # Не преобразуем неограниченно длинный пользовательский ввод.
    if len(normalized) > len(str(maximum)) + 1:
        raise ValidationError(f"Максимум: {maximum:,}".replace(",", " "))

    result = int(normalized)

    if result <= 0:
        raise ValidationError("Число должно быть больше нуля.")

    if result > maximum:
        raise ValidationError(f"Максимум: {maximum:,}".replace(",", " "))

    return result


SCHEMA_V1 = """
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE catalog_imports (
    catalog TEXT PRIMARY KEY,
    imported_at TEXT NOT NULL
);

CREATE TABLE products (
    catalog TEXT NOT NULL,
    item_id TEXT NOT NULL,
    name TEXT NOT NULL,
    search_name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit TEXT NOT NULL CHECK(unit IN ('piece', 'limit')),
    price INTEGER NOT NULL CHECK(price > 0),
    available INTEGER NOT NULL CHECK(available IN (0, 1)),
    stock_reputation INTEGER
        CHECK(stock_reputation IS NULL OR stock_reputation >= 0),
    version INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(catalog, item_id)
);

CREATE INDEX products_catalog_category
ON products(catalog, category, sort_order);

CREATE TABLE price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog TEXT NOT NULL,
    item_id TEXT NOT NULL,
    old_price INTEGER NOT NULL CHECK(old_price > 0),
    new_price INTEGER NOT NULL CHECK(new_price > 0),
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('change', 'rollback', 'import')),
    previous_version INTEGER NOT NULL,
    new_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(catalog, item_id)
        REFERENCES products(catalog, item_id)
);

CREATE INDEX price_history_item
ON price_history(catalog, item_id, id DESC);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    actor_id INTEGER,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    discord_message_id INTEGER,
    delivered_at TEXT,
    delivery_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE panels (
    panel_key TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    state TEXT NOT NULL DEFAULT 'pending',
    updated_at TEXT NOT NULL
);

CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    topic TEXT,
    channel_id INTEGER UNIQUE,
    interface_message_id INTEGER,
    state TEXT NOT NULL DEFAULT 'creating',
    workflow_json TEXT NOT NULL DEFAULT '{}',
    game_nickname TEXT,
    assigned_to INTEGER,
    staff_status TEXT NOT NULL DEFAULT 'new',
    creation_token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    last_error TEXT
);

CREATE INDEX tickets_owner
ON tickets(guild_id, owner_id, direction, state);

CREATE INDEX tickets_recovery
ON tickets(state, updated_at);

CREATE TABLE creation_attempts (
    guild_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, owner_id, direction)
);

CREATE TABLE restrictions (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    PRIMARY KEY(guild_id, user_id, direction)
);

CREATE TABLE message_waits (
    ticket_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    notification_key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    source_message_id INTEGER,
    delivered_message_id INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT,
    UNIQUE(ticket_id, notification_key),
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE TABLE cart_meta (
    ticket_id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1,
    acknowledged_fingerprint TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE TABLE cart_items (
    ticket_id INTEGER NOT NULL,
    catalog TEXT NOT NULL,
    item_id TEXT NOT NULL,
    quantity_text TEXT NOT NULL,
    seen_price INTEGER NOT NULL,
    seen_version INTEGER NOT NULL,
    PRIMARY KEY(ticket_id, catalog, item_id),
    FOREIGN KEY(ticket_id) REFERENCES tickets(id),
    FOREIGN KEY(catalog, item_id)
        REFERENCES products(catalog, item_id)
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL UNIQUE,
    guild_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    game_nickname TEXT,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK(status IN ('new', 'in_progress', 'completed', 'cancelled')),
    currency TEXT NOT NULL,
    total INTEGER NOT NULL CHECK(total >= 0),
    snapshot_json TEXT NOT NULL,
    terms_json TEXT,
    terms_agreed_by INTEGER,
    terms_agreed_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE INDEX orders_owner_status
ON orders(guild_id, owner_id, status, id DESC);

CREATE TABLE order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(order_id) REFERENCES orders(id)
);

CREATE TABLE agreements (
    ticket_id INTEGER PRIMARY KEY,
    agreement_json TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    agreed_at TEXT NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE TABLE close_jobs (
    ticket_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('mail', 'manual', 'faq')),
    state TEXT NOT NULL DEFAULT 'prepared',
    reason TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    mail_parts_json TEXT NOT NULL DEFAULT '[]',
    close_mode TEXT NOT NULL CHECK(close_mode IN ('move', 'delete')),
    close_after TEXT,
    transcript_path TEXT,
    transcript_sha256 TEXT,
    transcript_manifest_json TEXT,
    instructions_complete_at TEXT,
    history_complete_at TEXT,
    channel_closed_at TEXT,
    final_message_id INTEGER,
    final_delivery_state TEXT NOT NULL DEFAULT 'pending',
    final_delivery_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE INDEX close_jobs_recovery
ON close_jobs(state, next_retry_at);

CREATE TABLE dm_parts (
    ticket_id INTEGER NOT NULL,
    part_index INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    message_id INTEGER,
    sent_at TEXT,
    last_error TEXT,
    PRIMARY KEY(ticket_id, part_index),
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE TABLE transcript_deliveries (
    ticket_id INTEGER NOT NULL,
    part_index INTEGER NOT NULL,
    message_id INTEGER,
    state TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    PRIMARY KEY(ticket_id, part_index),
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE TABLE transcript_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_message_id INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    last_error TEXT,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE TABLE messages (
    message_id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    is_bot INTEGER NOT NULL CHECK(is_bot IN (0, 1)),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    edited_at TEXT,
    deleted_at TEXT,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    embeds_json TEXT NOT NULL DEFAULT '[]',
    jump_url TEXT,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE INDEX messages_ticket
ON messages(ticket_id, message_id);

CREATE TABLE message_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    ticket_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    attachments_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE faq_topics (
    topic_id TEXT PRIMARY KEY,
    emoji TEXT NOT NULL,
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    answer TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE user_profiles (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    game_nickname TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, user_id)
);

CREATE TABLE operation_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER,
    operation TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by INTEGER
);

CREATE TABLE import_previews (
    token TEXT PRIMARY KEY,
    actor_id INTEGER NOT NULL,
    catalog TEXT NOT NULL,
    document_json TEXT NOT NULL,
    expected_versions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    applied_at TEXT
);

CREATE TABLE reservations (
    order_id INTEGER NOT NULL,
    catalog TEXT NOT NULL,
    item_id TEXT NOT NULL,
    reputation_amount INTEGER NOT NULL CHECK(reputation_amount > 0),
    state TEXT NOT NULL
        CHECK(state IN ('reserved', 'released', 'consumed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(order_id, catalog, item_id),
    FOREIGN KEY(order_id) REFERENCES orders(id),
    FOREIGN KEY(catalog, item_id)
        REFERENCES products(catalog, item_id)
);
"""


def validate_catalog(
    document: Any,
    expected_catalog: str | None = None,
    max_price: int = 1_000_000_000_000,
) -> tuple[str, str, list[dict[str, Any]]]:
    if not isinstance(document, dict):
        raise ValidationError("Каталог должен быть JSON-объектом.")

    if document.get("schema_version") != 1:
        raise ValidationError("Поддерживается schema_version=1.")

    catalog = document.get("catalog")

    if catalog not in CATALOGS:
        raise ValidationError("Неизвестный прайс.")

    if expected_catalog is not None and catalog != expected_catalog:
        raise ValidationError(
            f"Ожидался прайс {expected_catalog}, получен {catalog}."
        )

    expected_unit = "limit" if catalog == "arsenal" else "piece"

    if document.get("unit") != expected_unit:
        raise ValidationError(
            f"Для {catalog} требуется единица {expected_unit}."
        )

    if document.get("columns") != COLUMNS:
        raise ValidationError("Неверный порядок columns.")

    rows = document.get("items")

    if not isinstance(rows, list) or not rows:
        raise ValidationError("Каталог должен содержать товары.")

    if len(rows) > 10000:
        raise ValidationError("Максимум 10 000 товаров в одном импорте.")

    seen = set()
    validated = []

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, list) or len(row) != len(COLUMNS):
            raise ValidationError(f"Неверная строка товара №{index}.")

        item_id, name, category, price, available, stock = row

        if not isinstance(item_id, str) or not ITEM_ID_RE.fullmatch(item_id):
            raise ValidationError(f"Недопустимый ID в строке №{index}.")

        if item_id in seen:
            raise ValidationError(f"Повторяется ID {item_id}.")

        seen.add(item_id)

        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValidationError(f"Недопустимое название {item_id}.")

        if (
            not isinstance(category, str)
            or not 1 <= len(category.strip()) <= 80
        ):
            raise ValidationError(f"Недопустимая категория {item_id}.")

        if type(price) is not int or not 0 < price <= max_price:
            raise ValidationError(f"Недопустимая цена {item_id}.")

        if type(available) is not bool:
            raise ValidationError(
                f"available для {item_id} должен быть true или false."
            )

        if stock is not None:
            if (
                type(stock) is not int
                or stock < 0
                or stock > 9_000_000_000_000_000
            ):
                raise ValidationError(f"Недопустимый остаток {item_id}.")

        if catalog != "arsenal" and stock is not None:
            raise ValidationError(
                "stock_reputation применяется только к арсеналу."
            )

        validated.append(
            {
                "item_id": item_id,
                "name": name.strip(),
                "category": category.strip(),
                "price": price,
                "available": int(available),
                "stock_reputation": stock,
                "sort_order": index,
            }
        )

    return catalog, expected_unit, validated


class Database:
    """
    Синхронный слой SQLite.

    В асинхронных обработчиках вызывается через asyncio.to_thread().
    Одно соединение не разделяется между потоками.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()

        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        connection = self.connect()

        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )

            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version "
                "FROM schema_migrations"
            ).fetchone()

            version = row["version"]

            if version > 1:
                raise DatabaseError(
                    "База создана более новой версией приложения."
                )

            if version < 1:
                # Здесь нет триггеров или литералов с ';'.
                # Не используем executescript: он может нарушить
                # ожидаемую границу транзакции.
                for statement in SCHEMA_V1.split(";"):
                    statement = statement.strip()
                    if statement:
                        connection.execute(statement)

                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES(1, ?)",
                    (utc_now(),),
                )

            connection.commit()

        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def fetchone(
        self,
        sql: str,
        parameters: tuple = (),
    ) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            row = connection.execute(sql, parameters).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def fetchall(
        self,
        sql: str,
        parameters: tuple = (),
    ) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            return [
                dict(row)
                for row in connection.execute(sql, parameters).fetchall()
            ]
        finally:
            connection.close()

    def execute(self, sql: str, parameters: tuple = ()) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(sql, parameters)
            return cursor.rowcount

    def setting_get(self, key: str, default: Any = None) -> Any:
        row = self.fetchone(
            "SELECT value_json FROM settings WHERE key=?",
            (key,),
        )
        return json.loads(row["value_json"]) if row else default

    def setting_set(self, key: str, value: Any) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, json_dump(value), utc_now()),
            )

    def initial_import(
        self,
        data_directory: Path,
        max_price: int,
        seller_status: str,
    ) -> dict[str, int]:
        """
        Импортирует прайсы и FAQ только при первом запуске.

        После записи catalog_imports повторный запуск не перезаписывает
        актуальные цены, наличие и названия из SQLite.
        """
        imported = {}

        for catalog in ("resources", "boosts", "arsenal"):
            # После первичного импорта JSON не является источником
            # актуального состояния.
            if self.fetchone(
                "SELECT catalog FROM catalog_imports WHERE catalog=?",
                (catalog,),
            ):
                continue

            document = read_json(data_directory / f"{catalog}.json")
            _, unit, items = validate_catalog(
                document,
                expected_catalog=catalog,
                max_price=max_price,
            )

            with self.transaction() as connection:
                # Повторная проверка под блокировкой записи.
                already = connection.execute(
                    "SELECT catalog FROM catalog_imports WHERE catalog=?",
                    (catalog,),
                ).fetchone()

                if already:
                    continue

                existing = connection.execute(
                    "SELECT COUNT(*) AS amount "
                    "FROM products WHERE catalog=?",
                    (catalog,),
                ).fetchone()["amount"]

                if existing:
                    raise DatabaseError(
                        f"Прайс {catalog} уже содержит товары, "
                        "но отметка первичного импорта отсутствует. "
                        "Автоматическое перезаписывание запрещено."
                    )

                now = utc_now()

                for item in items:
                    connection.execute(
                        """
                        INSERT INTO products(
                            catalog, item_id, name, search_name,
                            category, unit, price, available,
                            stock_reputation, version,
                            sort_order, updated_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            catalog,
                            item["item_id"],
                            item["name"],
                            normalize_search(item["name"]),
                            item["category"],
                            unit,
                            item["price"],
                            item["available"],
                            item["stock_reputation"],
                            item["sort_order"],
                            now,
                        ),
                    )

                connection.execute(
                    "INSERT INTO catalog_imports(catalog, imported_at) "
                    "VALUES(?, ?)",
                    (catalog, now),
                )

                imported[catalog] = len(items)

        faq_imported = self.setting_get("faq_initial_imported", False)

        if not faq_imported:
            document = read_json(data_directory / "faq.json")

            if document.get("schema_version") != 1:
                raise ValidationError("Неверная версия faq.json.")

            topics = document.get("topics", [])
            expected_topics = [
                "support", "cooperation", "ideas", "other"
            ]

            if [topic.get("id") for topic in topics] != expected_topics:
                raise ValidationError(
                    "Темы FAQ должны идти в порядке: "
                    "support, cooperation, ideas, other."
                )

            for topic in topics:
                for field in (
                    "emoji", "label", "description", "answer"
                ):
                    if not isinstance(topic.get(field), str):
                        raise ValidationError(
                            f"В FAQ отсутствует строка {field}."
                        )

                if not 1 <= len(topic["label"]) <= 100:
                    raise ValidationError("Слишком длинное название FAQ.")

                if not 1 <= len(topic["description"]) <= 100:
                    raise ValidationError("Слишком длинное описание FAQ.")

                if not 1 <= len(topic["answer"]) <= 12000:
                    raise ValidationError("Недопустимая длина ответа FAQ.")

            with self.transaction() as connection:
                already = connection.execute(
                    "SELECT value_json FROM settings WHERE key=?",
                    ("faq_initial_imported",),
                ).fetchone()

                if not already:
                    now = utc_now()

                    for order, topic in enumerate(topics):
                        connection.execute(
                            """
                            INSERT INTO faq_topics(
                                topic_id, emoji, label,
                                description, answer,
                                sort_order, version, updated_at
                            )
                            VALUES(?, ?, ?, ?, ?, ?, 1, ?)
                            """,
                            (
                                topic["id"],
                                topic["emoji"],
                                topic["label"],
                                topic["description"],
                                topic["answer"],
                                order,
                                now,
                            ),
                        )

                    connection.execute(
                        "INSERT INTO settings"
                        "(key, value_json, updated_at) VALUES(?, ?, ?)",
                        ("faq_initial_imported", "true", now),
                    )

        if seller_status not in {"active", "inactive"}:
            raise ValidationError("Неверный статус продавца.")

        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO settings(
                    key, value_json, updated_at
                )
                VALUES(?, ?, ?)
                """,
                (
                    "arsenal_seller_status",
                    json_dump(seller_status),
                    utc_now(),
                ),
            )

        return imported

    def get_product(
        self,
        catalog: str,
        item_id: str,
    ) -> dict[str, Any]:
        row = self.fetchone(
            "SELECT * FROM products WHERE catalog=? AND item_id=?",
            (catalog, item_id),
        )

        if not row:
            raise ItemNotFound("Товар не найден.")

        return row

    def list_products(
        self,
        catalog: str,
        category: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        if catalog not in CATALOGS:
            raise ValidationError("Неизвестный прайс.")

        sql = "SELECT * FROM products WHERE catalog=?"
        parameters: list[Any] = [catalog]

        if category is not None:
            sql += " AND category=?"
            parameters.append(category)

        if query:
            # instr(), а не LIKE: '%' и '_' воспринимаются как
            # обычные символы, а не маски поиска.
            sql += " AND instr(search_name, ?) > 0"
            parameters.append(normalize_search(query))

        sql += " ORDER BY sort_order, item_id"

        return self.fetchall(sql, tuple(parameters))

    def list_categories(self, catalog: str) -> list[str]:
        rows = self.fetchall(
            """
            SELECT category, MIN(sort_order) AS first_position
            FROM products
            WHERE catalog=?
            GROUP BY category
            ORDER BY first_position, category
            """,
            (catalog,),
        )
        return [row["category"] for row in rows]

    def _write_price(
        self,
        connection: sqlite3.Connection,
        product: sqlite3.Row,
        new_price: int,
        actor_id: int,
        action: str,
    ) -> dict[str, Any]:
        now = utc_now()
        next_version = product["version"] + 1

        cursor = connection.execute(
            """
            UPDATE products
            SET price=?, version=?, updated_at=?
            WHERE catalog=? AND item_id=? AND version=?
            """,
            (
                new_price,
                next_version,
                now,
                product["catalog"],
                product["item_id"],
                product["version"],
            ),
        )

        if cursor.rowcount != 1:
            raise VersionConflict(
                "Товар уже изменён другим сотрудником."
            )

        history_cursor = connection.execute(
            """
            INSERT INTO price_history(
                catalog, item_id, old_price, new_price,
                actor_id, action,
                previous_version, new_version, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product["catalog"],
                product["item_id"],
                product["price"],
                new_price,
                actor_id,
                action,
                product["version"],
                next_version,
                now,
            ),
        )

        event = {
            "history_id": history_cursor.lastrowid,
            "catalog": product["catalog"],
            "item_id": product["item_id"],
            "name": product["name"],
            "old_price": product["price"],
            "new_price": new_price,
            "version": next_version,
            "action": action,
        }

        # Событие Discord-лога сохраняется той же транзакцией.
        # Отправлять его будет отдельный обработчик после commit.
        connection.execute(
            """
            INSERT INTO audit_events(
                kind, actor_id, payload_json, created_at
            )
            VALUES(?, ?, ?, ?)
            """,
            ("price_changed", actor_id, json_dump(event), now),
        )

        return event

    def change_price(
        self,
        catalog: str,
        item_id: str,
        new_price: int,
        expected_version: int,
        actor_id: int,
        max_price: int,
    ) -> dict[str, Any]:
        if type(new_price) is not int or not 0 < new_price <= max_price:
            raise ValidationError("Недопустимая новая цена.")

        with self.transaction() as connection:
            product = connection.execute(
                "SELECT * FROM products "
                "WHERE catalog=? AND item_id=?",
                (catalog, item_id),
            ).fetchone()

            if not product:
                raise ItemNotFound("Товар не найден.")

            if product["version"] != expected_version:
                raise VersionConflict(
                    "Данные изменились. Актуальная цена: "
                    f"{product['price']}. Откройте подтверждение заново."
                )

            if product["price"] == new_price:
                raise ValidationError("Новая цена совпадает с текущей.")

            return self._write_price(
                connection,
                product,
                new_price,
                actor_id,
                "change",
            )

    def rollback_price(
        self,
        catalog: str,
        item_id: str,
        expected_version: int,
        expected_history_id: int,
        actor_id: int,
        max_price: int,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            product = connection.execute(
                "SELECT * FROM products "
                "WHERE catalog=? AND item_id=?",
                (catalog, item_id),
            ).fetchone()

            if not product:
                raise ItemNotFound("Товар не найден.")

            if product["version"] != expected_version:
                raise VersionConflict(
                    "Товар изменился. Повторно откройте откат."
                )

            history = connection.execute(
                """
                SELECT * FROM price_history
                WHERE catalog=? AND item_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (catalog, item_id),
            ).fetchone()

            if not history:
                raise ValidationError(
                    "У товара ещё нет предыдущей цены."
                )

            if history["id"] != expected_history_id:
                raise VersionConflict(
                    "История изменилась. Повторно откройте откат."
                )

            previous_price = history["old_price"]

            if not 0 < previous_price <= max_price:
                raise ValidationError(
                    "Предыдущая цена превышает текущие ограничения."
                )

            if previous_price == product["price"]:
                raise ValidationError(
                    "Предыдущая цена совпадает с текущей."
                )

            return self._write_price(
                connection,
                product,
                previous_price,
                actor_id,
                "rollback",
            )

    def update_product_state(
        self,
        catalog: str,
        item_id: str,
        expected_version: int,
        actor_id: int,
        available: bool | None = None,
        stock_reputation: int | None = None,
    ) -> int:
        if available is not None and type(available) is not bool:
            raise ValidationError("Неверный флаг доступности.")

        if stock_reputation is not None:
            if catalog != "arsenal":
                raise ValidationError(
                    "Запас репутации задаётся только для арсенала."
                )
            if (
                type(stock_reputation) is not int
                or not 0 <= stock_reputation <= 9_000_000_000_000_000
            ):
                raise ValidationError("Недопустимый остаток.")

        if available is None and stock_reputation is None:
            raise ValidationError("Изменения не заданы.")

        with self.transaction() as connection:
            product = connection.execute(
                "SELECT * FROM products "
                "WHERE catalog=? AND item_id=?",
                (catalog, item_id),
            ).fetchone()

            if not product:
                raise ItemNotFound("Товар не найден.")

            if product["version"] != expected_version:
                raise VersionConflict(
                    "Товар изменён другим сотрудником."
                )

            next_available = (
                int(available)
                if available is not None
                else product["available"]
            )
            next_stock = (
                stock_reputation
                if stock_reputation is not None
                else product["stock_reputation"]
            )

            if (
                next_available == product["available"]
                and next_stock == product["stock_reputation"]
            ):
                raise ValidationError("Значения не изменились.")

            now = utc_now()
            next_version = product["version"] + 1

            connection.execute(
                """
                UPDATE products
                SET available=?, stock_reputation=?,
                    version=?, updated_at=?
                WHERE catalog=? AND item_id=? AND version=?
                """,
                (
                    next_available,
                    next_stock,
                    next_version,
                    now,
                    catalog,
                    item_id,
                    expected_version,
                ),
            )

            payload = {
                "catalog": catalog,
                "item_id": item_id,
                "name": product["name"],
                "old_available": bool(product["available"]),
                "new_available": bool(next_available),
                "old_stock": product["stock_reputation"],
                "new_stock": next_stock,
                "version": next_version,
            }

            connection.execute(
                """
                INSERT INTO audit_events(
                    kind, actor_id, payload_json, created_at
                )
                VALUES(?, ?, ?, ?)
                """,
                (
                    "product_state_changed",
                    actor_id,
                    json_dump(payload),
                    now,
                ),
            )

            return next_version

    def price_history(
        self,
        catalog: str,
        item_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self.fetchall(
            """
            SELECT * FROM price_history
            WHERE catalog=? AND item_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (catalog, item_id, max(1, min(limit, 100))),
        )

    def export_catalog(self, catalog: str) -> dict[str, Any]:
        if catalog not in CATALOGS:
            raise ValidationError("Неизвестный прайс.")

        products = self.list_products(catalog)

        return {
            "schema_version": 1,
            "catalog": catalog,
            "unit": "limit" if catalog == "arsenal" else "piece",
            "columns": COLUMNS,
            "items": [
                [
                    product["item_id"],
                    product["name"],
                    product["category"],
                    product["price"],
                    bool(product["available"]),
                    product["stock_reputation"],
                ]
                for product in products
            ],
        }

    def log_error(
        self,
        operation: str,
        details: str,
        ticket_id: int | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO operation_errors(
                    ticket_id, operation, details, created_at
                )
                VALUES(?, ?, ?, ?)
                """,
                (ticket_id, operation, details, utc_now()),
            )