import sqlalchemy as sa
from alembic import op

revision = "0043_bazarr_mapping_cache"
down_revision = "0042_bazarr_subtitles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bazarr_series_mappings",
        sa.Column("series_entity_id", sa.Text(), nullable=False),
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("target_path", sa.Text(), nullable=True),
        sa.Column("bazarr_series_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("synced_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["series_entity_id"],
            ["library_entities.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("series_entity_id"),
    )
    op.create_index(
        "idx_bazarr_series_mappings_library",
        "bazarr_series_mappings",
        ["library_id"],
    )
    op.create_table(
        "bazarr_episode_mappings",
        sa.Column("media_file_id", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("series_entity_id", sa.Text(), nullable=False),
        sa.Column("target_path", sa.Text(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("modified_ns", sa.Integer(), nullable=True),
        sa.Column("quick_fingerprint", sa.Text(), nullable=True),
        sa.Column("bazarr_series_id", sa.Integer(), nullable=True),
        sa.Column("bazarr_episode_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("season_number", sa.Integer(), nullable=True),
        sa.Column("episode_number", sa.Integer(), nullable=True),
        sa.Column("subtitles_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("synced_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["media_file_id"], ["media_files.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["library_entities.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["series_entity_id"],
            ["bazarr_series_mappings.series_entity_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("media_file_id"),
    )
    op.create_index(
        "idx_bazarr_episode_mappings_entity",
        "bazarr_episode_mappings",
        ["entity_id"],
    )
    op.create_index(
        "idx_bazarr_episode_mappings_series",
        "bazarr_episode_mappings",
        ["series_entity_id"],
    )
    op.create_index(
        "idx_bazarr_episode_mappings_state",
        "bazarr_episode_mappings",
        ["state"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_bazarr_episode_mappings_state", table_name="bazarr_episode_mappings"
    )
    op.drop_index(
        "idx_bazarr_episode_mappings_series", table_name="bazarr_episode_mappings"
    )
    op.drop_index(
        "idx_bazarr_episode_mappings_entity", table_name="bazarr_episode_mappings"
    )
    op.drop_table("bazarr_episode_mappings")
    op.drop_index(
        "idx_bazarr_series_mappings_library", table_name="bazarr_series_mappings"
    )
    op.drop_table("bazarr_series_mappings")
