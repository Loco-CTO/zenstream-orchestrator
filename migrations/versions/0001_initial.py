from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

TABLES = [
    "CREATE TABLE IF NOT EXISTS users (username TEXT UNIQUE NOT NULL, password TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS invites (url TEXT UNIQUE NOT NULL)",
    "CREATE TABLE IF NOT EXISTS settings (servername TEXT NOT NULL, origin_type INTEGER NOT NULL, origin_url TEXT NOT NULL, api_key TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS client_secrets (username TEXT NOT NULL, client_secret TEXT NOT NULL, expiration TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS user_preferences (jellyfin_user_id TEXT PRIMARY KEY NOT NULL, locale TEXT NOT NULL DEFAULT 'en', subtitle_font_family TEXT NOT NULL DEFAULT 'sans', subtitle_bold INTEGER NOT NULL DEFAULT 0, subtitle_text_scale REAL NOT NULL DEFAULT 100, subtitle_font_color TEXT NOT NULL DEFAULT '#ffffff', subtitle_border_size REAL NOT NULL DEFAULT 0, subtitle_border_color TEXT NOT NULL DEFAULT '#000000', subtitle_background_color TEXT NOT NULL DEFAULT '#000000', subtitle_background_opacity REAL NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS syncplay_groups (id TEXT PRIMARY KEY, host_user_id TEXT NOT NULL, host_name TEXT NOT NULL, allow_controls INTEGER NOT NULL DEFAULT 0, item_id TEXT, position REAL NOT NULL DEFAULT 0, playing INTEGER NOT NULL DEFAULT 0, resume INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, timeline_revision INTEGER NOT NULL DEFAULT 0, media_generation INTEGER NOT NULL DEFAULT 0, anchor_position REAL NOT NULL DEFAULT 0, anchor_time REAL NOT NULL DEFAULT 0, effective_at REAL NOT NULL DEFAULT 0, playback_state TEXT NOT NULL DEFAULT 'paused', pause_reason TEXT, host_disconnected_at REAL, ended INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS syncplay_operations (operation_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL, state TEXT NOT NULL)",
]


def upgrade():
    for sql in TABLES:
        op.execute(sa.text(sql))
    conn = op.get_bind()
    columns = {
        row[1] for row in conn.exec_driver_sql("PRAGMA table_info(syncplay_members)")
    }
    if not columns:
        op.execute(
            sa.text(
                "CREATE TABLE syncplay_members (group_id TEXT NOT NULL, user_id TEXT NOT NULL, participant_id TEXT NOT NULL DEFAULT '', username TEXT NOT NULL, viewing INTEGER NOT NULL DEFAULT 0, loading INTEGER NOT NULL DEFAULT 0, ready_generation INTEGER NOT NULL DEFAULT -1, presence_sequence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (group_id, participant_id))"
            )
        )
    elif "participant_id" not in columns or "user_id" in {
        row[1]
        for row in conn.exec_driver_sql(
            "PRAGMA index_info(sqlite_autoindex_syncplay_members_1)"
        )
    }:
        op.execute(
            sa.text("ALTER TABLE syncplay_members RENAME TO syncplay_members_legacy")
        )
        op.execute(
            sa.text(
                "CREATE TABLE syncplay_members (group_id TEXT NOT NULL, user_id TEXT NOT NULL, participant_id TEXT NOT NULL DEFAULT '', username TEXT NOT NULL, viewing INTEGER NOT NULL DEFAULT 0, loading INTEGER NOT NULL DEFAULT 0, ready_generation INTEGER NOT NULL DEFAULT -1, presence_sequence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (group_id, participant_id))"
            )
        )
        participant = (
            "CASE WHEN participant_id IS NULL OR participant_id = '' THEN '__legacy__:' || rowid ELSE participant_id END"
            if "participant_id" in columns
            else "'__legacy__:' || rowid"
        )
        op.execute(
            sa.text(
                f"INSERT INTO syncplay_members SELECT group_id, user_id, {participant}, username, viewing, loading, ready_generation, presence_sequence FROM syncplay_members_legacy"
            )
        )
        op.execute(sa.text("DROP TABLE syncplay_members_legacy"))


def downgrade():
    pass
