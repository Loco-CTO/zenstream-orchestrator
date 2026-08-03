from alembic import op


revision = "0004_metadata_image_lookup_indexes"
down_revision = "0003_catalog_media_date_index"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "idx_metadata_images_provider_lookup",
        "metadata_images",
        ["provider", "entity_type", "provider_id", "image_type", "image_url", "fetched_at"],
    )
    op.create_index(
        "idx_metadata_images_url_lookup",
        "metadata_images",
        ["image_type", "image_url", "fetched_at"],
    )


def downgrade():
    op.drop_index("idx_metadata_images_url_lookup", table_name="metadata_images")
    op.drop_index("idx_metadata_images_provider_lookup", table_name="metadata_images")
