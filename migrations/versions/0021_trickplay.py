from alembic import op
import sqlalchemy as sa


revision = "0021_trickplay"
down_revision = "0020_unlimited_playback_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "playback_settings",
        sa.Column("trickplay_frame_width", sa.Integer(), nullable=False, server_default="320"),
    )
    op.add_column(
        "playback_settings",
        sa.Column("trickplay_frame_height", sa.Integer(), nullable=False, server_default="180"),
    )
    op.add_column(
        "playback_settings",
        sa.Column("trickplay_interval_seconds", sa.Integer(), nullable=False, server_default="10"),
    )
    op.create_table(
        "trickplay_assets",
        sa.Column("media_file_id", sa.Text(), primary_key=True),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.Text(), nullable=False),
        sa.Column("frame_width", sa.Integer(), nullable=False),
        sa.Column("frame_height", sa.Integer(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("state", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("output_key", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["media_file_id"], ["media_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["library_entities.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "trickplay_sheets",
        sa.Column("media_file_id", sa.Text(), nullable=False),
        sa.Column("output_key", sa.Text(), nullable=False),
        sa.Column("sheet_index", sa.Integer(), nullable=False),
        sa.Column("first_frame", sa.Integer(), nullable=False),
        sa.Column("frame_count", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("media_file_id", "output_key", "sheet_index"),
        sa.ForeignKeyConstraint(["media_file_id"], ["media_files.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_trickplay_assets_state", "trickplay_assets", ["state", "updated_at"])
    op.create_index("idx_trickplay_assets_entity", "trickplay_assets", ["entity_id"])


def downgrade():
    op.drop_index("idx_trickplay_assets_entity", table_name="trickplay_assets")
    op.drop_index("idx_trickplay_assets_state", table_name="trickplay_assets")
    op.drop_table("trickplay_sheets")
    op.drop_table("trickplay_assets")
    op.drop_column("playback_settings", "trickplay_interval_seconds")
    op.drop_column("playback_settings", "trickplay_frame_height")
    op.drop_column("playback_settings", "trickplay_frame_width")
