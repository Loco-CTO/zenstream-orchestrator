import sqlalchemy as sa
from alembic import op

revision = "0053_music_album_page"
down_revision = "0052_music_scan_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_music_album_page",
        sa.Column("release_id", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("artist_id", sa.Text(), nullable=False),
        sa.Column("title_sort", sa.Text(), nullable=False, server_default=""),
        sa.Column("release_sort", sa.Text(), nullable=False, server_default=""),
        sa.Column("added_sort_ns", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "last_added_sort_ns", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("release_id", "locale"),
        sa.ForeignKeyConstraint(
            ["release_id"], ["library_entities.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["artist_id"], ["library_entities.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "idx_music_album_page_title",
        "catalog_music_album_page",
        ["library_id", "locale", "title_sort", "release_id"],
    )
    op.create_index(
        "idx_music_album_page_release",
        "catalog_music_album_page",
        ["library_id", "locale", "release_sort", "title_sort", "release_id"],
    )
    op.create_index(
        "idx_music_album_page_added",
        "catalog_music_album_page",
        ["library_id", "locale", "added_sort_ns", "title_sort", "release_id"],
    )
    op.create_index(
        "idx_music_album_page_last_added",
        "catalog_music_album_page",
        [
            "library_id",
            "locale",
            "last_added_sort_ns",
            "title_sort",
            "release_id",
        ],
    )
    op.create_table(
        "catalog_music_album_page_status",
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("library_id"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("catalog_music_album_page_status")
    op.drop_index(
        "idx_music_album_page_last_added", table_name="catalog_music_album_page"
    )
    op.drop_index("idx_music_album_page_added", table_name="catalog_music_album_page")
    op.drop_index("idx_music_album_page_release", table_name="catalog_music_album_page")
    op.drop_index("idx_music_album_page_title", table_name="catalog_music_album_page")
    op.drop_table("catalog_music_album_page")
