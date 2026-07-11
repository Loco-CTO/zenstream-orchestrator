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
        self.lock = threading.RLock()
        self.connect()

    def connect(self):
        """Connect to the database."""
        if self.db_type == "sqlite":
            self._connect_sqlite(self.db_file)

    def _connect_sqlite(self, db_file):
        """Connect to a SQLite database."""
        try:
            self.connection = sqlite3.connect(db_file, check_same_thread=False)
            self.create_tables()
        except sqlite3.Error as e:
            print(f"Error connecting to SQLite: {e}")

    def create_tables(self):
        """Create the tables in the database."""
        for table, queries in self.create_query[self.db_type].items():
            self._create_or_update_table(table, queries)

    def _create_or_update_table(self, table_name, queries):
        """Create or update a table in the database."""
        if not self._table_exists(table_name):
            self._create_table(queries["create"])
        else:
            self._update_table(table_name, queries["columns"])

    def _table_exists(self, table_name):
        """Check if a table exists in the database."""
        cursor = self.connection.cursor()
        if self.db_type == "sqlite":
            cursor.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'"
            )
        elif self.db_type == "mysql":
            cursor.execute(f"SHOW TABLES LIKE '{table_name}'")
        exists = cursor.fetchone() is not None
        cursor.close()
        return exists

    def _create_table(self, create_query):
        """Create a table in the database."""
        cursor = self.connection.cursor()
        cursor.execute(create_query)
        self.connection.commit()
        cursor.close()

    def _update_table(self, table_name, columns):
        """Update a table in the database."""
        cursor = self.connection.cursor()
        existing_columns = self._get_existing_columns(table_name)
        for column, column_def in columns.items():
            if column not in existing_columns:
                alter_query = (
                    f"ALTER TABLE {table_name} ADD COLUMN {column} {column_def}"
                )
                cursor.execute(alter_query)
                self.connection.commit()

    def _get_existing_columns(self, table_name):
        """Get the existing columns in a table."""
        cursor = self.connection.cursor()
        if self.db_type == "sqlite":
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = {col[1] for col in cursor.fetchall()}
        elif self.db_type == "mysql":
            cursor.execute(f"DESCRIBE {table_name}")
            columns = {col[0] for col in cursor.fetchall()}
        cursor.close()
        return columns

    def execute(self, query, params=None):
        """Execute a query on the database."""
        with self.lock:
            cursor = self.connection.cursor()
            try:
                cursor.execute(query, params or ())
                self.connection.commit()
                return cursor.fetchall()
            except sqlite3.Error as e:
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
