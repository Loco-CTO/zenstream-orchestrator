import sqlalchemy as sa
from alembic import op

revision = "0054_catalog_search_write_indexes"
down_revision = "0053_music_album_page"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_catalog_root_search_grams_entity_locale",
        "catalog_root_search_grams",
        ["entity_id", "locale"],
    )
    op.create_table(
        "catalog_search_row_lookup",
        sa.Column("search_rowid", sa.Integer(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("search_rowid"),
    )
    op.create_index(
        "idx_catalog_search_row_lookup_entity_locale",
        "catalog_search_row_lookup",
        ["entity_id", "locale"],
    )
    op.execute(
        "INSERT OR IGNORE INTO catalog_search_row_lookup(entity_id,locale,search_rowid) "
        "SELECT entity_id,locale,rowid FROM catalog_search"
    )


def downgrade() -> None:
    op.drop_index(
        "idx_catalog_search_row_lookup_entity_locale",
        table_name="catalog_search_row_lookup",
    )
    op.drop_table("catalog_search_row_lookup")
    op.drop_index(
        "idx_catalog_root_search_grams_entity_locale",
        table_name="catalog_root_search_grams",
    )
