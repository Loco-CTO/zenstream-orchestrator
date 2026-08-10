"""Index series discovery by its added timestamp."""

from alembic import op

revision = "0015_catalog_discovery_added_index"
down_revision = "0014_library_watcher_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_catalog_entity_summary_type_added",
        "catalog_entity_summary",
        ["library_id", "entity_type", "added_sort_ns", "entity_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_catalog_entity_summary_type_added",
        table_name="catalog_entity_summary",
    )
