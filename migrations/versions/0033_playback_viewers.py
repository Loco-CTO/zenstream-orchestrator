"""Add persistent client devices and live playback viewers."""

from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0033_playback_viewers"
down_revision = "0032_playback_language_preferences"
branch_labels = None
depends_on = None


def _columns(bind, table):
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _backfill_legacy_devices(bind):
    now = datetime.now(timezone.utc).isoformat()
    sessions = bind.execute(
        sa.text(
            """
            SELECT user_id, MIN(created_at), MAX(last_seen_at)
              FROM user_sessions
             WHERE device_id IS NULL
             GROUP BY user_id
            """
        )
    ).fetchall()
    for user_id, first_seen_at, last_seen_at in sessions:
        existing = bind.execute(
            sa.text(
                "SELECT id FROM user_devices WHERE user_id=:user_id AND device_key='legacy'"
            ),
            {"user_id": user_id},
        ).scalar()
        device_id = existing or str(uuid4())
        if existing is None:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO user_devices(
                        id,user_id,device_key,device_type,first_seen_at,last_seen_at
                    ) VALUES(:id,:user_id,'legacy','unknown',:first_seen_at,:last_seen_at)
                    """
                ),
                {
                    "id": device_id,
                    "user_id": user_id,
                    "first_seen_at": first_seen_at or now,
                    "last_seen_at": last_seen_at or first_seen_at or now,
                },
            )
        bind.execute(
            sa.text(
                "UPDATE user_sessions SET device_id=:device_id WHERE user_id=:user_id AND device_id IS NULL"
            ),
            {"device_id": device_id, "user_id": user_id},
        )


def upgrade():
    bind = op.get_bind()
    if "device_id" not in _columns(bind, "user_sessions"):
        op.add_column("user_sessions", sa.Column("device_id", sa.Text(), nullable=True))

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_devices (
            id TEXT PRIMARY KEY NOT NULL,
            user_id TEXT NOT NULL,
            device_key TEXT NOT NULL,
            device_type TEXT NOT NULL DEFAULT 'unknown',
            browser TEXT,
            operating_system TEXT,
            device_name TEXT,
            client_name TEXT,
            client_version TEXT,
            ip_address TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(user_id, device_key),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS playback_viewer_sessions (
            id TEXT PRIMARY KEY NOT NULL,
            user_id TEXT NOT NULL,
            auth_session_id TEXT,
            device_id TEXT,
            entity_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            worker_session_id TEXT,
            mode TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            engine TEXT,
            position_seconds REAL NOT NULL DEFAULT 0,
            duration_seconds REAL,
            paused INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_heartbeat_at TEXT NOT NULL,
            ended_at TEXT,
            requested_bitrate INTEGER,
            audio_stream_id TEXT,
            requested_mode TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(device_id) REFERENCES user_devices(id) ON DELETE SET NULL,
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(source_id) REFERENCES media_sources(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS playback_viewer_commands (
            id TEXT PRIMARY KEY NOT NULL,
            viewer_session_id TEXT NOT NULL,
            action TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            delivered_at TEXT,
            acknowledged_at TEXT,
            error TEXT,
            FOREIGN KEY(viewer_session_id) REFERENCES playback_viewer_sessions(id) ON DELETE CASCADE,
            CHECK(action IN ('pause','resume','stop'))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_devices_user_key ON user_devices(user_id,device_key)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_devices_user_active ON user_devices(user_id,last_seen_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_sessions_device ON user_sessions(device_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_playback_viewer_active ON playback_viewer_sessions(state,last_heartbeat_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_playback_viewer_user ON playback_viewer_sessions(user_id,state)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_playback_viewer_device ON playback_viewer_sessions(device_id,state)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_playback_viewer_worker ON playback_viewer_sessions(worker_session_id,state)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_playback_viewer_command_delivery ON playback_viewer_commands(viewer_session_id,state,expires_at)"
    )
    _backfill_legacy_devices(bind)


def downgrade():
    op.drop_index("idx_playback_viewer_command_delivery", table_name="playback_viewer_commands")
    op.drop_index("idx_playback_viewer_worker", table_name="playback_viewer_sessions")
    op.drop_index("idx_playback_viewer_device", table_name="playback_viewer_sessions")
    op.drop_index("idx_playback_viewer_user", table_name="playback_viewer_sessions")
    op.drop_index("idx_playback_viewer_active", table_name="playback_viewer_sessions")
    op.drop_index("idx_user_sessions_device", table_name="user_sessions")
    op.drop_index("idx_user_devices_user_active", table_name="user_devices")
    op.drop_index("idx_user_devices_user_key", table_name="user_devices")
    op.drop_table("playback_viewer_commands")
    op.drop_table("playback_viewer_sessions")
    op.drop_table("user_devices")
    op.drop_column("user_sessions", "device_id")
