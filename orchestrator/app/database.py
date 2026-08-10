import threading
import time
from contextlib import contextmanager
from pathlib import Path

from app.database_session import create_sqlite_persistence
from app.logging_config import get_logger
from sqlalchemy.exc import SQLAlchemyError

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
        self._wal_maintenance_lock = threading.Lock()
        self._scheduled_maintenance_lock = threading.Lock()
        self._last_passive_checkpoint_at = 0.0
        self._last_passive_checkpoint_clean = False
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
        except Exception:
            logger.exception("could not connect to SQLite")
            raise

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
            except Exception:
                # sqlite3 leaves the connection inside the failed transaction
                # after constraint/locking errors. Always roll it back before
                # re-raising so the next serialized operation can begin cleanly.
                self.connection.rollback()
                logger.exception("SQLite write failed")
                raise
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
            self.read_local.connection = session.connection()
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
                self.read_local.connection = connection
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

    def wal_bytes(self) -> int:
        if not self.db_file or self.db_file == ":memory:":
            return 0
        wal = Path(f"{self.db_file}-wal")
        try:
            return wal.stat().st_size
        except OSError:
            return 0

    def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int]:
        selected = mode.upper()
        if selected not in {"PASSIVE", "TRUNCATE"}:
            raise ValueError("Unsupported SQLite checkpoint mode")
        wait_started = time.monotonic()
        wait_seconds = self.lock.acquire()
        acquired_at = time.monotonic()
        try:
            cursor = self.connection.cursor()
            try:
                row = cursor.execute(f"PRAGMA wal_checkpoint({selected})").fetchone()
                result = tuple(int(value or 0) for value in (row or (0, 0, 0)))
                logger.info(
                    "sqlite wal checkpoint mode=%s busy=%s log_frames=%s checkpointed_frames=%s wal_bytes=%s",
                    selected,
                    result[0],
                    result[1],
                    result[2],
                    self.wal_bytes(),
                )
                return result
            finally:
                cursor.close()
        finally:
            hold_seconds = time.monotonic() - acquired_at
            self.lock.release()
            self._log_writer_timing(
                "checkpoint", wait_seconds, hold_seconds, wait_started
            )

    def maintain_wal(
        self, *, scan_complete: bool = False
    ) -> tuple[int, int, int] | None:
        """Best-effort, rate-limited WAL maintenance outside request transactions."""
        threshold = 64 * 1024 * 1024
        if not self._wal_maintenance_lock.acquire(blocking=False):
            return None
        try:
            if scan_complete:
                try:
                    result = self.checkpoint("TRUNCATE")
                except Exception:
                    logger.exception("sqlite wal truncate checkpoint failed")
                    return None
                if result[0]:
                    logger.warning(
                        "sqlite wal truncate deferred busy=%s log_frames=%s checkpointed_frames=%s",
                        *result,
                    )
                else:
                    self._last_passive_checkpoint_clean = True
                return result

            if self.wal_bytes() < threshold:
                return None
            current = time.monotonic()
            cooldown = 300.0 if self._last_passive_checkpoint_clean else 30.0
            if current - self._last_passive_checkpoint_at < cooldown:
                return None
            self._last_passive_checkpoint_at = current
            try:
                result = self.checkpoint("PASSIVE")
            except Exception:
                logger.exception("sqlite wal passive checkpoint failed")
                self._last_passive_checkpoint_clean = False
                return None
            busy, log_frames, checkpointed_frames = result
            self._last_passive_checkpoint_clean = (
                busy == 0 and checkpointed_frames >= log_frames
            )
            if busy:
                logger.warning(
                    "sqlite wal passive checkpoint incomplete busy=%s log_frames=%s checkpointed_frames=%s",
                    busy,
                    log_frames,
                    checkpointed_frames,
                )
            elif checkpointed_frames < log_frames:
                logger.warning(
                    "sqlite wal passive checkpoint incomplete log_frames=%s checkpointed_frames=%s",
                    log_frames,
                    checkpointed_frames,
                )
            return result
        finally:
            self._wal_maintenance_lock.release()

    def optimize(self) -> None:
        self.write("PRAGMA optimize")

    def schedule_maintenance(self, *, scan_complete: bool = False) -> bool:
        """Run planner/WAL maintenance asynchronously and at most once."""
        if not self._scheduled_maintenance_lock.acquire(blocking=False):
            return False

        def maintain() -> None:
            try:
                self.optimize()
                result = self.maintain_wal(scan_complete=scan_complete)
                retries = 0
                while scan_complete and result and result[0] and retries < 5:
                    retries += 1
                    time.sleep(min(60.0, 15.0 * retries))
                    result = self.maintain_wal(scan_complete=True)
            except Exception:
                logger.exception("sqlite scheduled maintenance failed")
            finally:
                self._scheduled_maintenance_lock.release()

        threading.Thread(
            target=maintain,
            name="sqlite-maintenance",
            daemon=True,
        ).start()
        return True

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
