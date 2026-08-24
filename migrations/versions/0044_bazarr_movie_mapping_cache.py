import sqlalchemy as sa
from alembic import op

revision = "0044_bazarr_movie_mapping_cache"
down_revision = "0043_bazarr_mapping_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bazarr_movie_mappings",
        sa.Column("media_file_id", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("target_path", sa.Text(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("modified_ns", sa.Integer(), nullable=True),
        sa.Column("quick_fingerprint", sa.Text(), nullable=True),
        sa.Column("bazarr_movie_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("media_file_id"),
    )
    op.create_index(
        "idx_bazarr_movie_mappings_entity",
        "bazarr_movie_mappings",
        ["entity_id"],
    )
    op.create_index(
        "idx_bazarr_movie_mappings_library",
        "bazarr_movie_mappings",
        ["library_id"],
    )
    op.create_index(
        "idx_bazarr_movie_mappings_state",
        "bazarr_movie_mappings",
        ["state"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_bazarr_movie_mappings_state", table_name="bazarr_movie_mappings"
    )
    op.drop_index(
        "idx_bazarr_movie_mappings_library", table_name="bazarr_movie_mappings"
    )
    op.drop_index(
        "idx_bazarr_movie_mappings_entity", table_name="bazarr_movie_mappings"
    )
    op.drop_table("bazarr_movie_mappings")
