from alembic import op
import sqlalchemy as sa


revision = "0007_metadata_hydration"
down_revision = "0006_scheduler_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS metadata_hydration_requests (
            entity_id TEXT NOT NULL,
            locale TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            requested_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            PRIMARY KEY(entity_id, locale),
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
        )
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_metadata_hydration_state ON metadata_hydration_requests(state, requested_at)"))


def downgrade():
    op.execute(sa.text("DROP TABLE IF EXISTS metadata_hydration_requests"))
