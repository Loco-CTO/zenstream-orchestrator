import threading
import time
from contextlib import contextmanager

from sqlalchemy.exc import SQLAlchemyError

from app.database_session import create_sqlite_persistence
from app.logging_config import get_logger


logger = get_logger("database")


class FairWriteGate:
    """Provide re-entrant FIFO admission to SQLite's single writer."""

    def __init__(self):
        self._condition = threading.Condition()
        self._next_ticket = 0
        self._serving_ticket = 0
        self._owner = None
        self._depth = 0

    def acquire(self) -> float:
        thread_id = threading.get_ident()
        started = time.monotonic()
        with self._condition:
            if self._owner == thread_id:
                self._depth += 1
                return 0.0
            ticket = self._next_ticket
            self._next_ticket += 1
            while ticket != self._serving_ticket:
                self._condition.wait()
            self._owner = thread_id
            self._depth = 1
        return time.monotonic() - started

    def release(self) -> None:
        thread_id = threading.get_ident()
        with self._condition:
            if self._owner != thread_id:
                raise RuntimeError("SQLite writer released by a non-owner thread")
            self._depth -= 1
            if self._depth:
                return
            self._owner = None
            self._serving_ticket += 1
            self._condition.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()


class DatabaseHandler:
    def __init__(self, db_type, create_query, db_file=None):
        """
        Initialize the database handler.

        Args:
            db_type (str): The type of database to connect to.
            create_query (dict): A dictionary containing the create query for each table.
            db_file (str): The path to the database file.
        """
        self.db_type = db_type
        self.create_query = create_query
        self.db_file = db_file
        self.connection = None
        self.read_connection = None
        self.persistence = None
        self.lock = FairWriteGate()
        self.read_lock = threading.RLock()
        self.read_local = threading.local()
        self.read_connections = []
        self.read_connections_lock = threading.RLock()
        self.connect()

    def connect(self):
        """Connect to the database."""
        if self.db_type == "sqlite":
            self._connect_sqlite(self.db_file)

    def _connect_sqlite(self, db_file):
        """Connect to a SQLite database."""
        try:
            self.persistence = create_sqlite_persistence(db_file)
            self.connection = self.persistence.writer_engine.raw_connection()
            if db_file != ":memory:":
                self.connection.execute("PRAGMA journal_mode = WAL")
        except Exception as e:
            print(f"Error connecting to SQLite: {e}")

    def execute(self, query, params=None):
        """Execute a statement through the appropriate read or write path."""
        if self._is_read_query(query):
            return self.read_execute(query, params)
        return self.write(query, params)

    @staticmethod
    def _is_read_query(query) -> bool:
        statement = str(query or "").lstrip().upper()
        return statement.startswith(("SELECT", "PRAGMA", "EXPLAIN"))

    def write(self, query, params=None):
        """Execute one serialized mutation and commit it."""
        wait_started = time.monotonic()
        wait_seconds = self.lock.acquire()
        acquired_at = time.monotonic()
        try:
            cursor = self.connection.cursor()
            try:
                cursor.execute(query, params or ())
                self.connection.commit()
                return cursor.fetchall()
            except Exception as e:
                # sqlite3 leaves the connection inside the failed transaction
                # after constraint/locking errors. Always roll it back before
                # returning so the next serialized operation can begin cleanly.
                self.connection.rollback()
                print(f"Database error: {e}")
                return e
            finally:
                cursor.close()
        finally:
            hold_seconds = time.monotonic() - acquired_at
            self.lock.release()
            self._log_writer_timing(
                "statement", wait_seconds, hold_seconds, wait_started
            )

    def write_many(self, statements):
        """Commit a bounded batch of mutations in one short transaction."""
        statements = list(statements)
        if not statements:
            return True
        with self.transaction() as cursor:
            for query, params in statements:
                cursor.execute(query, params or ())
        return True

    @contextmanager
    def transaction(self):
        """Run a sequence of statements as one serialized SQLite transaction."""
        wait_started = time.monotonic()
        wait_seconds = self.lock.acquire()
        acquired_at = time.monotonic()
        try:
            cursor = self.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                yield cursor
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()
        finally:
            hold_seconds = time.monotonic() - acquired_at
            self.lock.release()
            self._log_writer_timing(
                "transaction", wait_seconds, hold_seconds, wait_started
            )

    @contextmanager
    def read_session(self):
        """Reuse one query-only SQLAlchemy session for a top-level read."""
        if self.db_file == ":memory:":
            yield
            return
        if self.persistence is None or self.persistence.read_sessions is None:
            raise RuntimeError("SQLite read sessions are not available")
        active = getattr(self.read_local, "session", None)
        if active is not None:
            self.read_local.depth = getattr(self.read_local, "depth", 1) + 1
            try:
                yield
            finally:
                self.read_local.depth -= 1
            return
        with self.persistence.read_sessions() as session:
            self.read_local.session = session
            self.read_local.depth = 1
            try:
                yield
            except Exception:
                session.rollback()
                raise
            finally:
                self.read_local.session = None
                self.read_local.depth = 0

    def read_execute(self, query, params=None):
        """Execute a read without waiting on writer or unrelated reader locks."""
        if self.db_file == ":memory:":
            connection = self.connection
            lock = self.read_lock
        else:
            if self.persistence is None or self.persistence.read_sessions is None:
                raise RuntimeError("SQLite read sessions are not available")
            active = getattr(self.read_local, "session", None)
            if active is not None:
                try:
                    result = active.connection().exec_driver_sql(
                        query, tuple(params or ())
                    )
                    return [tuple(row) for row in result.fetchall()]
                except SQLAlchemyError as e:
                    active.rollback()
                    print(f"Database read error: {e}")
                    raise
            with self.persistence.read_sessions() as session:
                connection = session.connection()
                try:
                    result = connection.exec_driver_sql(query, tuple(params or ()))
                    return [tuple(row) for row in result.fetchall()]
                except SQLAlchemyError as e:
                    session.rollback()
                    print(f"Database read error: {e}")
                    raise

        def execute_read():
            cursor = connection.cursor()
            try:
                cursor.execute(query, params or ())
                return cursor.fetchall()
            except Exception as e:
                connection.rollback()
                print(f"Database read error: {e}")
                raise
            finally:
                cursor.close()

        if lock is not None:
            with lock:
                return execute_read()
        return execute_read()

    @staticmethod
    def _log_writer_timing(
        operation: str, wait_seconds: float, hold_seconds: float, started: float
    ) -> None:
        if wait_seconds >= 0.1 or hold_seconds >= 0.25:
            logger.warning(
                "sqlite writer timing operation=%s wait_seconds=%.3f hold_seconds=%.3f total_seconds=%.3f",
                operation,
                wait_seconds,
                hold_seconds,
                time.monotonic() - started,
            )

    def fetchall(self):
        """Fetch all rows from the database."""
        cursor = self.connection.cursor()
        try:
            return cursor.fetchall() if cursor else None
        except Exception as e:
            print(f"Database error: {e}")
            return e
        finally:
            cursor.close()

    def fetchone(self):
        """Fetch one row from the database."""
        cursor = self.connection.cursor()
        try:
            return cursor.fetchone() if cursor else None
        except Exception as e:
            print(f"Database error: {e}")
            return None
        finally:
            cursor.close()

    def close(self):
        """Close the database connection."""
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.persistence:
            self.persistence.close()
            self.persistence = None
        self.read_connections.clear()
