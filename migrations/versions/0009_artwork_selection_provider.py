from alembic import op

revision = "0009_artwork_selection_provider"
down_revision = "0008_trigram_search"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE catalog_artwork_selection "
        "ADD COLUMN provider TEXT NOT NULL DEFAULT ''"
    )
    op.execute(
        "UPDATE catalog_artwork_selection SET provider=COALESCE(("
        "SELECT mi.provider FROM metadata_images mi "
        "JOIN entity_provider_ids ep ON ep.provider=mi.provider "
        "AND ep.provider_id=mi.provider_id "
        "WHERE ep.entity_id=catalog_artwork_selection.entity_id "
        "AND mi.image_type=catalog_artwork_selection.image_type "
        "AND mi.local_path=catalog_artwork_selection.local_path "
        "ORDER BY ep.is_primary DESC,mi.fetched_at DESC LIMIT 1"
        "),'')"
    )


def downgrade():
    op.execute("ALTER TABLE catalog_artwork_selection DROP COLUMN provider")
