from alembic import op
import sqlalchemy as sa


revision = "0019_hls_seek_diagnostics"
down_revision = "0018_playback_settings"
branch_labels = None
depends_on = None


def upgrade():
    for name, column in (
        ("actual_start_seconds", sa.Column("actual_start_seconds", sa.Float(), nullable=True)),
        ("seek_generation", sa.Column("seek_generation", sa.Integer(), nullable=True)),
        ("first_segment_duration_seconds", sa.Column("first_segment_duration_seconds", sa.Float(), nullable=True)),
    ):
        op.add_column("playback_sessions", column)


def downgrade():
    for name in ("first_segment_duration_seconds", "seek_generation", "actual_start_seconds"):
        op.drop_column("playback_sessions", name)
