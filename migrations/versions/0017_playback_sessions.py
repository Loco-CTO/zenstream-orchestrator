from alembic import op
import sqlalchemy as sa


revision = "0017_playback_sessions"
down_revision = "0016_incremental_scan"
branch_labels = None
depends_on = None


def upgrade():
    for name, column in (
        ("process_id", sa.Column("process_id", sa.Integer(), nullable=True)),
        ("profile_hash", sa.Column("profile_hash", sa.Text(), nullable=True)),
        ("requested_start_seconds", sa.Column("requested_start_seconds", sa.Float(), nullable=True)),
        ("audio_stream_id", sa.Column("audio_stream_id", sa.Text(), nullable=True)),
        ("last_accessed_at", sa.Column("last_accessed_at", sa.Text(), nullable=True)),
        ("started_at", sa.Column("started_at", sa.Text(), nullable=True)),
        ("completed_at", sa.Column("completed_at", sa.Text(), nullable=True)),
        ("failure_code", sa.Column("failure_code", sa.Text(), nullable=True)),
        ("failure_detail", sa.Column("failure_detail", sa.Text(), nullable=True)),
    ):
        op.add_column("playback_sessions", column)


def downgrade():
    for name in ("failure_detail", "failure_code", "completed_at", "started_at", "last_accessed_at", "audio_stream_id", "requested_start_seconds", "profile_hash", "process_id"):
        op.drop_column("playback_sessions", name)
