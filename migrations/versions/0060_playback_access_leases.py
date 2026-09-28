import sqlalchemy as sa
from alembic import op

revision = "0060_playback_access_leases"
down_revision = "0059_playlists"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "playback_access_leases",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("auth_session_id", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("playback_session_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_playback_access_leases_token_hash"),
    )
    op.create_index(
        "ix_playback_access_leases_expiry",
        "playback_access_leases",
        ["expires_at", "revoked_at"],
    )
    op.create_index(
        "ix_playback_access_leases_auth_session",
        "playback_access_leases",
        ["auth_session_id", "revoked_at"],
    )
    op.create_index(
        "ix_playback_access_leases_playback_session",
        "playback_access_leases",
        ["playback_session_id", "revoked_at"],
    )
    op.execute(
        "CREATE TRIGGER playback_access_lease_auth_session_revoked "
        "AFTER UPDATE OF revoked_at ON user_sessions "
        "WHEN NEW.revoked_at IS NOT NULL "
        "BEGIN UPDATE playback_access_leases SET revoked_at=NEW.revoked_at "
        "WHERE auth_session_id=NEW.id AND revoked_at IS NULL; END"
    )
    op.execute(
        "CREATE TRIGGER playback_access_lease_auth_session_deleted "
        "BEFORE DELETE ON user_sessions "
        "BEGIN UPDATE playback_access_leases SET revoked_at="
        "strftime('%Y-%m-%dT%H:%M:%f+00:00','now') "
        "WHERE auth_session_id=OLD.id AND revoked_at IS NULL; END"
    )
    op.execute(
        "CREATE TRIGGER playback_access_lease_worker_stopped "
        "AFTER UPDATE OF state ON playback_sessions "
        "WHEN NEW.state IN ('stopping','failed','expired') "
        "BEGIN UPDATE playback_access_leases SET revoked_at="
        "COALESCE(NEW.completed_at,strftime('%Y-%m-%dT%H:%M:%f+00:00','now')) "
        "WHERE playback_session_id=NEW.id AND revoked_at IS NULL; END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS playback_access_lease_worker_stopped")
    op.execute("DROP TRIGGER IF EXISTS playback_access_lease_auth_session_deleted")
    op.execute("DROP TRIGGER IF EXISTS playback_access_lease_auth_session_revoked")
    op.drop_index(
        "ix_playback_access_leases_playback_session",
        table_name="playback_access_leases",
    )
    op.drop_index(
        "ix_playback_access_leases_auth_session",
        table_name="playback_access_leases",
    )
    op.drop_index(
        "ix_playback_access_leases_expiry",
        table_name="playback_access_leases",
    )
    op.drop_table("playback_access_leases")
