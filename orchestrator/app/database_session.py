from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import URL, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool, StaticPool


@dataclass
class SQLitePersistence:
    writer_engine: Engine
    read_engine: Engine | None
    read_sessions: sessionmaker[Session] | None

    def close(self) -> None:
        if self.read_engine is not None:
            self.read_engine.dispose()
        self.writer_engine.dispose()


def _writer_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA wal_autocheckpoint = 1000")
        cursor.execute("PRAGMA cache_size = -64000")
        cursor.execute("PRAGMA temp_store = MEMORY")
    finally:
        cursor.close()


def _reader_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA query_only = ON")
        cursor.execute("PRAGMA busy_timeout = 500")
        cursor.execute("PRAGMA cache_size = -64000")
        cursor.execute("PRAGMA temp_store = MEMORY")
    finally:
        cursor.close()


def create_sqlite_persistence(db_file: str) -> SQLitePersistence:
    """Create SQLAlchemy-managed SQLite engines and scoped read sessions."""
    connect_args = {"check_same_thread": False, "timeout": 5.0}
    if db_file == ":memory:":
        writer_engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args=connect_args,
            poolclass=StaticPool,
        )
        event.listen(writer_engine, "connect", _writer_pragmas)
        return SQLitePersistence(writer_engine, None, None)

    url = URL.create("sqlite+pysqlite", database=db_file)
    writer_engine = create_engine(
        url,
        connect_args=connect_args,
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
    )
    event.listen(writer_engine, "connect", _writer_pragmas)

    read_engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 0.5},
        poolclass=QueuePool,
        pool_size=16,
        max_overflow=16,
        pool_timeout=5,
    )
    event.listen(read_engine, "connect", _reader_pragmas)
    return SQLitePersistence(
        writer_engine,
        read_engine,
        sessionmaker(bind=read_engine, autoflush=False, expire_on_commit=False),
    )
