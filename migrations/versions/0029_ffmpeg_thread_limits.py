from alembic import op
import sqlalchemy as sa

revision = "0029_ffmpeg_thread_limits"
down_revision = "0028_trigger_owned_scheduling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "playback_settings",
        sa.Column(
            "trickplay_ffmpeg_threads",
            sa.Integer(),
            nullable=False,
            server_default="4",
        ),
    )
    op.add_column(
        "intro_outro_settings",
        sa.Column(
            "intro_outro_ffmpeg_threads",
            sa.Integer(),
            nullable=False,
            server_default="4",
        ),
    )


def downgrade() -> None:
    op.drop_column("intro_outro_settings", "intro_outro_ffmpeg_threads")
    op.drop_column("playback_settings", "trickplay_ffmpeg_threads")
