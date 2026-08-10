from alembic import op

revision = "0002_catalog_read_indexes"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX idx_library_entities_parent_id "
        "ON library_entities(parent_id) WHERE parent_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_media_files_role_modified_entity "
        "ON media_files(role, modified_ns DESC, entity_id, id)"
    )
    op.execute(
        "CREATE INDEX idx_library_entities_admin_browse "
        "ON library_entities(library_id, parent_id, entity_type, "
        "relative_path COLLATE NOCASE, id)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_library_entities_admin_browse")
    op.execute("DROP INDEX IF EXISTS idx_media_files_role_modified_entity")
    op.execute("DROP INDEX IF EXISTS idx_library_entities_parent_id")
