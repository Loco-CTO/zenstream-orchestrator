"""Remove the audit-only discovery index while preserving migration history."""

from alembic import op
import sqlalchemy as sa

revision = "0021_remove_catalog_discovery_added_index"
down_revision = "0020_revert_library_watcher_extensions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    names = {
        index.get("name")
        for index in inspector.get_indexes("catalog_entity_summary")
    }
    if "idx_catalog_entity_summary_type_added" in names:
        op.drop_index(
            "idx_catalog_entity_summary_type_added",
            table_name="catalog_entity_summary",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    names = {
        index.get("name")
        for index in inspector.get_indexes("catalog_entity_summary")
    }
    if "idx_catalog_entity_summary_type_added" not in names:
        op.create_index(
            "idx_catalog_entity_summary_type_added",
            "catalog_entity_summary",
            ["library_id", "entity_type", "added_sort_ns", "entity_id"],
        )
