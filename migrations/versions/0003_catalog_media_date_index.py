from alembic import op


revision = "0003_catalog_media_date_index"
down_revision = "0002_quick_fingerprint"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "idx_media_files_entity_role_modified",
        "media_files",
        ["entity_id", "role", "modified_ns"],
    )


def downgrade():
    op.drop_index("idx_media_files_entity_role_modified", table_name="media_files")
