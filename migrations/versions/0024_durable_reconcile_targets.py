from alembic import op


revision = "0024_durable_reconcile_targets"
down_revision = "0023_global_artwork_selection_rebuild"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS library_reconcile_targets (
            library_id TEXT NOT NULL,
            top_level_root TEXT NOT NULL,
            debounce_until REAL NOT NULL,
            event_count INTEGER NOT NULL DEFAULT 1,
            revision INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (library_id, top_level_root),
            FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_reconcile_due "
        "ON library_reconcile_targets(debounce_until, library_id)"
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS library_reconcile_targets")
