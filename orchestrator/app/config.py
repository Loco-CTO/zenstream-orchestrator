from .database import DatabaseHandler
import os
import time
from pathlib import Path
from alembic import command
from alembic.config import Config as AlembicConfig


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Config:
    SCHEMA_GENERATION = "sqlalchemy-metadata-v2"
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

        # This release intentionally starts from a clean SQLAlchemy schema.
        # Preserve the old file for recovery/debugging, then let Alembic build
        # the fresh baseline below. WAL sidecars must move with the database.
        if database_file.exists() and database_file.stat().st_size:
            generation_rows = []
            try:
                has_schema_metadata = self._database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_metadata'"
                )
                if has_schema_metadata:
                    generation_rows = self._database.execute(
                        "SELECT value FROM schema_metadata WHERE key='generation'"
                    )
            except Exception:
                generation_rows = []
            generation = generation_rows[0][0] if generation_rows else None
            if generation != self.SCHEMA_GENERATION:
                self._database.close()
                archive = database_file.with_name(
                    f"{database_file.name}.pre-{time.strftime('%Y%m%d%H%M%S')}"
                )
                database_file.replace(archive)
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{database_file}{suffix}")
                    if sidecar.exists():
                        sidecar.replace(Path(f"{archive}{suffix}"))
                self._database = DatabaseHandler(
                    db_type="sqlite", create_query={}, db_file=str(database_file)
                )

        self._run_migrations()

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
        database = self._database
        # Test and embedded callers may provide a lightweight database facade
        # without a filesystem path. Keep the participant-key repair usable
        # for those callers while normal installations use Alembic.
        if not getattr(database, "db_file", None):
            columns = database.execute("PRAGMA table_info(syncplay_members)", ())
            if not columns:
                return
            names = [row[1] for row in columns]
            if "participant_id" not in names:
                with database.transaction() as cursor:
                    cursor.execute(
                        "ALTER TABLE syncplay_members ADD COLUMN participant_id TEXT NOT NULL DEFAULT ''"
                    )
                columns = database.execute("PRAGMA table_info(syncplay_members)", ())
                names = [row[1] for row in columns]
            primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
            indexes = database.execute("PRAGMA index_list(syncplay_members)", ())
            has_old_unique = any(
                bool(row[2]) and str(row[1]).lower() not in {"sqlite_autoindex_syncplay_members_1"}
                for row in indexes
            )
            if primary == ["group_id", "participant_id"] and not has_old_unique:
                return
            with database.transaction() as cursor:
                source_names = set(names)
                source_expr = lambda name, default: name if name in source_names else default
                cursor.execute("ALTER TABLE syncplay_members RENAME TO syncplay_members_legacy")
                cursor.execute(
                    """CREATE TABLE syncplay_members (
                        group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                        participant_id TEXT NOT NULL DEFAULT '', username TEXT NOT NULL,
                        watching_together INTEGER NOT NULL DEFAULT 1,
                        viewing INTEGER NOT NULL DEFAULT 0, loading INTEGER NOT NULL DEFAULT 0,
                        ready_generation INTEGER NOT NULL DEFAULT -1,
                        presence_sequence INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(group_id, participant_id)
                    )"""
                )
                cursor.execute(
                    """INSERT INTO syncplay_members(
                        group_id,user_id,participant_id,username,watching_together,
                        viewing,loading,ready_generation,presence_sequence
                    )
                    SELECT group_id,user_id,
                        CASE WHEN COALESCE(participant_id,'')='' THEN '__legacy__:' || user_id ELSE participant_id END,
                        username,COALESCE({watching_together},1),COALESCE({viewing},0),
                        COALESCE({loading},0),COALESCE({ready_generation},-1),COALESCE({presence_sequence},0)
                    FROM syncplay_members_legacy"""
                    .format(
                        watching_together=source_expr("watching_together", "1"),
                        viewing=source_expr("viewing", "0"),
                        loading=source_expr("loading", "0"),
                        ready_generation=source_expr("ready_generation", "-1"),
                        presence_sequence=source_expr("presence_sequence", "0"),
                    )
                )
                cursor.execute("DROP TABLE syncplay_members_legacy")
            return
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
