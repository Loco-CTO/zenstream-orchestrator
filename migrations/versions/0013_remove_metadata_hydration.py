from alembic import op
import sqlalchemy as sa


revision = "0013_remove_metadata_hydration"
down_revision = "0012_remove_gateway_tables"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("DROP TABLE IF EXISTS metadata_hydration_requests"))


def downgrade():
    op.execute(sa.text(
        """CREATE TABLE IF NOT EXISTS metadata_hydration_requests (
            entity_id TEXT NOT NULL,
            locale TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            error_details TEXT,
            requested_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            PRIMARY KEY(entity_id, locale)
        )"""
    ))
