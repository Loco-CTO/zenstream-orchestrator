from alembic import op


revision = "0055_metadata_cleanup_path_indexes"
down_revision = "0054_catalog_search_write_indexes"
branch_labels = None
depends_on = None


_INDEXES = (
    ("idx_metadata_images_local_path", "metadata_images"),
    ("idx_catalog_artwork_selection_local_path", "catalog_artwork_selection"),
    ("idx_people_local_path", "people"),
    ("idx_screen_extractor_assets_local_path", "screen_extractor_assets"),
)


def upgrade() -> None:
    for name, table in _INDEXES:
        op.create_index(name, table, ["local_path"])


def downgrade() -> None:
    for name, table in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
