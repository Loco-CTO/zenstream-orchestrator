import sqlalchemy as sa
from alembic import op


revision = "0048_music_artist_credits"
down_revision = "0047_audio_play_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "music_artist_credits",
        sa.Column("track_id", sa.Text(), nullable=False),
        sa.Column("artist_id", sa.Text(), nullable=False),
        sa.Column("credit_order", sa.Integer(), nullable=False),
        sa.Column("credited_name", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["track_id"], ["library_entities.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["artist_id"], ["library_entities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("track_id", "artist_id"),
    )
    op.create_index(
        "idx_music_artist_credits_artist_order",
        "music_artist_credits",
        ["artist_id", "credit_order", "track_id"],
    )
    op.create_index(
        "idx_music_artist_credits_track_order",
        "music_artist_credits",
        ["track_id", "credit_order"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_music_artist_credits_track_order",
        table_name="music_artist_credits",
    )
    op.drop_index(
        "idx_music_artist_credits_artist_order",
        table_name="music_artist_credits",
    )
    op.drop_table("music_artist_credits")
