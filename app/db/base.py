"""Подключение к SQLite.

Режим WAL включён намеренно: фоновый планировщик пишет в базу в своём потоке,
пока веб-запросы из неё читают. Без WAL читатели блокировали бы писателя.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import DATABASE_URL


log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DATABASE_URL,
    # соединения переиспользуются между потоками планировщика и веба
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Сессия с автоматическим commit/rollback."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """Зависимость FastAPI."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _add_missing_columns() -> None:
    """Простейшая миграция: дописывает появившиеся колонки в существующие таблицы.

    create_all() создаёт только недостающие ТАБЛИЦЫ и молча игнорирует новые колонки
    в уже существующих. Полноценный Alembic для базы одного пользователя — избыточен,
    а вот менять схему на живых данных всё равно приходится.

    Добавляются только nullable-колонки и колонки со скалярным значением по умолчанию:
    остальное SQLite через ALTER TABLE ADD COLUMN и не умеет.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        have_tables = {r[0] for r in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'"))}
        for table in Base.metadata.sorted_tables:
            if table.name not in have_tables:
                continue
            existing = {r[1] for r in conn.execute(text(f'PRAGMA table_info("{table.name}")'))}
            for col in table.columns:
                if col.name in existing:
                    continue
                default = None
                if col.default is not None and not col.default.is_callable:
                    default = col.default.arg
                if default is None and not col.nullable:
                    log.warning("[db] колонка %s.%s требует значения по умолчанию — "
                                "пропущена, добавьте вручную", table.name, col.name)
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                if default is not None:
                    lit = f"'{default}'" if isinstance(default, str) else \
                          ("1" if default is True else "0" if default is False else str(default))
                    ddl += f" DEFAULT {lit}"
                conn.execute(text(ddl))
                log.info("[db] добавлена колонка %s.%s", table.name, col.name)


def init_db() -> None:
    from app.db import models  # noqa: F401 — регистрирует таблицы в метаданных
    Base.metadata.create_all(engine)
    _add_missing_columns()
