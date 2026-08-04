from .database import DatabaseHandler
import os
import sqlite3
from pathlib import Path
from alembic import command
from alembic.config import Config as AlembicConfig


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Config:
    _instance = None
    _isDev = True

    def __new__(
        cls,
    ):
        """Create a new instance of the configuration handler."""
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)

            cls._instance._initialize()
        return cls._instance

    def _initialize(
        self,
    ):
        """
        Initialize the configuration handler.

        Args:
            logger (Logger): The logger instance.
        """
        self._base_addresses = {
            "development": "http://172.0.0.1:3000",
            "production": "https://google.com",
        }

        self.create_database()
        from app.models.admin import Admin

        credentials = Admin.bootstrap()
        if credentials:
            print("\n=== ZENSTREAM ORCHESTRATOR ROOT ADMIN ===")
            print(f"Username: {credentials[0]}")
            print(f"Password: {credentials[1]}")
            print("Save these credentials now; they are shown only once.\n")

    def create_database(self):
        """Create the database handler."""
        database_directory = PROJECT_ROOT / "sqlite"
        database_directory.mkdir(exist_ok=True)
        database_file = database_directory / "orchestrator.db"
        self._prepare_fresh_database(database_file)
        self._database = DatabaseHandler(
            db_type="sqlite",
            create_query={
                "sqlite": {
                    "users": {
                        "create": """
                    CREATE TABLE IF NOT EXISTS users (
                        username TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL
                    )
                """,
                        "columns": {
                            "username": "TEXT UNIQUE NOT NULL",
                            "password": "TEXT NOT NULL",
                        },
                    },
                    "invites": {
                        "create": """
                        CREATE TABLE IF NOT EXISTS invites (
                            url TEXT UNIQUE NOT NULL
                            )""",
                        "columns": {"url": "TEXT UNIQUE NOT NULL"},
                    },
                    "syncplay_groups": {
                        "create": """CREATE TABLE IF NOT EXISTS syncplay_groups (id TEXT PRIMARY KEY, host_user_id TEXT NOT NULL, host_name TEXT NOT NULL, allow_controls INTEGER NOT NULL DEFAULT 0, item_id TEXT, position REAL NOT NULL DEFAULT 0, playing INTEGER NOT NULL DEFAULT 0, resume INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, timeline_revision INTEGER NOT NULL DEFAULT 0, media_generation INTEGER NOT NULL DEFAULT 0, anchor_position REAL NOT NULL DEFAULT 0, anchor_time REAL NOT NULL DEFAULT 0, effective_at REAL NOT NULL DEFAULT 0, playback_state TEXT NOT NULL DEFAULT 'paused', pause_reason TEXT, host_disconnected_at REAL, ended INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL)""",
                        "columns": {
                            "id": "TEXT",
                            "host_user_id": "TEXT",
                            "host_name": "TEXT",
                            "allow_controls": "INTEGER NOT NULL DEFAULT 0",
                            "item_id": "TEXT",
                            "position": "REAL NOT NULL DEFAULT 0",
                            "playing": "INTEGER NOT NULL DEFAULT 0",
                            "resume": "INTEGER NOT NULL DEFAULT 0",
                            "revision": "INTEGER NOT NULL DEFAULT 0",
                            "timeline_revision": "INTEGER NOT NULL DEFAULT 0",
                            "media_generation": "INTEGER NOT NULL DEFAULT 0",
                            "anchor_position": "REAL NOT NULL DEFAULT 0",
                            "anchor_time": "REAL NOT NULL DEFAULT 0",
                            "effective_at": "REAL NOT NULL DEFAULT 0",
                            "playback_state": "TEXT NOT NULL DEFAULT 'paused'",
                            "pause_reason": "TEXT",
                            "host_disconnected_at": "REAL",
                            "ended": "INTEGER NOT NULL DEFAULT 0",
                            "updated": "REAL NOT NULL DEFAULT 0",
                        },
                    },
                    "syncplay_members": {
                        "create": """CREATE TABLE IF NOT EXISTS syncplay_members (group_id TEXT NOT NULL, user_id TEXT NOT NULL, participant_id TEXT NOT NULL DEFAULT '', username TEXT NOT NULL, watching_together INTEGER NOT NULL DEFAULT 1, viewing INTEGER NOT NULL DEFAULT 0, loading INTEGER NOT NULL DEFAULT 0, ready_generation INTEGER NOT NULL DEFAULT -1, presence_sequence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (group_id, participant_id))""",
                        "columns": {
                            "group_id": "TEXT",
                            "user_id": "TEXT",
                            "participant_id": "TEXT NOT NULL DEFAULT ''",
                            "username": "TEXT",
                            "watching_together": "INTEGER NOT NULL DEFAULT 1",
                            "viewing": "INTEGER NOT NULL DEFAULT 0",
                            "loading": "INTEGER NOT NULL DEFAULT 0",
                            "ready_generation": "INTEGER NOT NULL DEFAULT -1",
                            "presence_sequence": "INTEGER NOT NULL DEFAULT 0",
                        },
                    },
                    "syncplay_operations": {
                        "create": """CREATE TABLE IF NOT EXISTS syncplay_operations (operation_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL, state TEXT NOT NULL)""",
                        "columns": {
                            "operation_id": "TEXT",
                            "group_id": "TEXT",
                            "user_id": "TEXT",
                            "state": "TEXT NOT NULL",
                        },
                    },
                },
            },
            db_file=str(database_file),
        )

        self._run_migrations()

    @staticmethod
    def _prepare_fresh_database(database_file: Path) -> None:
        if not database_file.is_file() or database_file.stat().st_size == 0:
            return
        compatible = False
        connection = None
        try:
            connection = sqlite3.connect(str(database_file), timeout=1.0)
            row = connection.execute(
                "SELECT value FROM schema_metadata WHERE key='generation'"
            ).fetchone()
            compatible = bool(row and row[0] == "catalog-projection-v1")
        except sqlite3.OperationalError as error:
            message = str(error).lower()
            if "locked" in message or "busy" in message:
                raise RuntimeError(
                    "SQLite schema could not be verified because the database is busy; refusing to archive it."
                ) from error
            compatible = False
        except sqlite3.Error:
            compatible = False
        finally:
            if connection is not None:
                connection.close()
        if not compatible:
            raise RuntimeError(
                "SQLite schema generation is not recognized; refusing to move the live database. "
                "Run the configured Alembic migrations or restore a compatible database backup."
            )

    @property
    def database(self):
        """Get the database handler."""
        return self._database

    def _run_migrations(self):
        """Bring the configured database to the latest Alembic revision."""
        alembic = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
        alembic.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        alembic.set_main_option("sqlalchemy.url", f"sqlite:///{self._database.db_file}")
        command.upgrade(alembic, "head")

    def _migrate_syncplay_members_participant_key(self):
        """Compatibility entry point for callers of the former one-off migration."""
        self._run_migrations()

    @property
    def base_address(self):
        """Get the base address configuration."""
        if self._isDev:
            return self._base_addresses["development"]
        return self._base_addresses["production"]


config = Config()


def load_config() -> Config:
    """Load the configuration handler."""
    return Config()
