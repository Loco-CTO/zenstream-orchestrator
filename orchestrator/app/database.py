import sqlite3
import threading
from contextlib import contextmanager


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
        self.lock = threading.RLock()
        self.read_lock = threading.RLock()
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
                self.read_connection = sqlite3.connect(
                    db_file, check_same_thread=False, timeout=2.0
                )
                self.read_connection.execute("PRAGMA query_only = ON")
                self.read_connection.execute("PRAGMA busy_timeout = 2000")
            else:
                self.read_connection = self.connection
        except sqlite3.Error as e:
            print(f"Error connecting to SQLite: {e}")

    def execute(self, query, params=None):
        """Execute a query on the database."""
        with self.lock:
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

    @contextmanager
    def transaction(self):
        """Run a sequence of statements as one serialized SQLite transaction."""
        with self.lock:
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

    def read_execute(self, query, params=None):
        """Execute a read without waiting on the writer connection lock."""
        connection = self.read_connection or self.connection
        with self.read_lock:
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
