"""Accounts, permissions, preferences, user state, and media catalog.

Revision ID: 0011_accounts_catalog
Revises: 0010_metadata_languages
"""

from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa


revision = "0011_accounts_catalog"
down_revision = "0010_metadata_languages"
branch_labels = None
depends_on = None


def _columns(connection, table: str) -> set[str]:
    return {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}


def upgrade():
    connection = op.get_bind()
    user_columns = _columns(connection, "users")
    if "id" not in user_columns:
        op.add_column("users", sa.Column("id", sa.Text(), nullable=True))
    if "password_scheme" not in user_columns:
        op.add_column(
            "users",
            sa.Column("password_scheme", sa.Text(), nullable=False, server_default="sha256"),
        )
    if "disabled" not in user_columns:
        op.add_column(
            "users",
            sa.Column("disabled", sa.Integer(), nullable=False, server_default="0"),
        )
    for username, current_id in connection.exec_driver_sql("SELECT username,id FROM users"):
        if not current_id:
            connection.exec_driver_sql(
                "UPDATE users SET id=? WHERE username=?",
                (str(uuid.uuid4()), username),
            )
    op.execute(sa.text("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_id ON users(id)"))

    statements = [
        """CREATE TABLE IF NOT EXISTS user_sessions (
            id TEXT PRIMARY KEY NOT NULL,
            user_id TEXT NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS user_library_access (
            user_id TEXT NOT NULL,
            library_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id,library_id),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS account_preferences (
            user_id TEXT PRIMARY KEY NOT NULL,
            locale TEXT NOT NULL DEFAULT 'en',
            metadata_language TEXT,
            subtitle_font_family TEXT NOT NULL DEFAULT 'sans',
            subtitle_bold INTEGER NOT NULL DEFAULT 0,
            subtitle_text_scale REAL NOT NULL DEFAULT 100,
            subtitle_font_color TEXT NOT NULL DEFAULT '#ffffff',
            subtitle_border_size REAL NOT NULL DEFAULT 0,
            subtitle_border_color TEXT NOT NULL DEFAULT '#000000',
            subtitle_background_color TEXT NOT NULL DEFAULT '#000000',
            subtitle_background_opacity REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS user_item_state (
            user_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            favorite INTEGER NOT NULL DEFAULT 0,
            played INTEGER NOT NULL DEFAULT 0,
            play_count INTEGER NOT NULL DEFAULT 0,
            position_seconds REAL NOT NULL DEFAULT 0,
            duration_seconds REAL NOT NULL DEFAULT 0,
            last_played_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id,entity_id),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS media_sources (
            id TEXT PRIMARY KEY NOT NULL,
            entity_id TEXT NOT NULL,
            media_file_id TEXT NOT NULL,
            container TEXT,
            duration_seconds REAL,
            bitrate INTEGER,
            width INTEGER,
            height INTEGER,
            video_codec TEXT,
            audio_codec TEXT,
            probe_payload TEXT NOT NULL DEFAULT '{}',
            probed_at TEXT NOT NULL,
            UNIQUE(entity_id,media_file_id),
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(media_file_id) REFERENCES media_files(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS playback_sessions (
            id TEXT PRIMARY KEY NOT NULL,
            user_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            output_directory TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(source_id) REFERENCES media_sources(id) ON DELETE CASCADE
        )""",
    ]
    for statement in statements:
        op.execute(sa.text(statement))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id,expires_at)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_user_item_state_resume ON user_item_state(user_id,last_played_at)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_media_sources_entity ON media_sources(entity_id)"))
    op.execute(sa.text("CREATE VIRTUAL TABLE IF NOT EXISTS catalog_search USING fts5(entity_id UNINDEXED, library_id UNINDEXED, locale UNINDEXED, title, tokenize='trigram')"))


def downgrade():
    op.execute(sa.text("DROP TABLE IF EXISTS catalog_search"))
    for table in (
        "playback_sessions",
        "media_sources",
        "user_item_state",
        "account_preferences",
        "user_library_access",
        "user_sessions",
    ):
        op.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))

