from alembic import op
from sqlalchemy import Column, Integer

revision = "0006_analysis_worker_limits"
down_revision = "0005_sqlalchemy_persistence_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "playback_settings",
        Column("trickplay_workers", Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "intro_outro_settings",
        Column("intro_outro_workers", Integer(), nullable=False, server_default="1"),
    )


def downgrade():
    op.drop_column("intro_outro_settings", "intro_outro_workers")
    op.drop_column("playback_settings", "trickplay_workers")
