import sqlalchemy as sa
from alembic import op


revision = "0047_audio_play_events"
down_revision = "0046_metadata_refresh_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_play_events",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("playback_instance_id", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["library_entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "playback_instance_id"),
    )
    op.create_index(
        "idx_user_play_events_started_at",
        "user_play_events",
        ["started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_user_play_events_started_at",
        table_name="user_play_events",
    )
    op.drop_table("user_play_events")
