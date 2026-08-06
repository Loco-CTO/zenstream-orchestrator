import sqlite3
import threading
import time
from contextlib import contextmanager

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
            self.connection = sqlite3.connect(db_file, check_same_thread=False, timeout=5.0)
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA busy_timeout = 5000")
            if db_file != ":memory:":
                self.connection.execute("PRAGMA journal_mode = WAL")
                self.connection.execute("PRAGMA synchronous = NORMAL")
                self.connection.execute("PRAGMA wal_autocheckpoint = 1000")
                self.connection.execute("PRAGMA cache_size = -64000")
                self.connection.execute("PRAGMA temp_store = MEMORY")
                self.read_connection = sqlite3.connect(
                    db_file, check_same_thread=False, timeout=0.5
                )
                self.read_connection.execute("PRAGMA query_only = ON")
                self.read_connection.execute("PRAGMA busy_timeout = 500")
                self.read_connection.execute("PRAGMA cache_size = -64000")
                self.read_connection.execute("PRAGMA temp_store = MEMORY")
            else:
                self.read_connection = self.connection
        except sqlite3.Error as e:
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
            except sqlite3.Error as e:
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

    def read_execute(self, query, params=None):
        """Execute a read without waiting on writer or unrelated reader locks."""
        if self.db_file == ":memory:":
            connection = self.connection
            lock = self.read_lock
        else:
            connection = getattr(self.read_local, "connection", None)
            if connection is None:
                connection = sqlite3.connect(self.db_file, check_same_thread=False, timeout=0.5)
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA busy_timeout = 500")
                connection.execute("PRAGMA cache_size = -64000")
                connection.execute("PRAGMA temp_store = MEMORY")
                self.read_local.connection = connection
                with self.read_connections_lock:
                    self.read_connections.append(connection)
            lock = None

        def execute_read():
            cursor = connection.cursor()
            try:
                cursor.execute(query, params or ())
                return cursor.fetchall()
            except sqlite3.Error as e:
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
        except sqlite3.Error as e:
            print(f"Database error: {e}")
            return e
        finally:
            cursor.close()

    def fetchone(self):
        """Fetch one row from the database."""
        cursor = self.connection.cursor()
        try:
            return cursor.fetchone() if cursor else None
        except sqlite3.Error as e:
            print(f"Database error: {e}")
            return None
        finally:
            cursor.close()

    def close(self):
        """Close the database connection."""
        if self.connection:
            self.connection.close()
        if self.read_connection and self.read_connection is not self.connection:
            self.read_connection.close()
        with self.read_connections_lock:
            for connection in self.read_connections:
                connection.close()
            self.read_connections.clear()
