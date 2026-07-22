"""Persist administrator-selected metadata languages."""

from alembic import op
import sqlalchemy as sa


revision = "0010_metadata_languages"
down_revision = "0009_tvdb_primary_repair"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS metadata_settings (
            key TEXT PRIMARY KEY NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """))


def downgrade():
    op.execute(sa.text("DROP TABLE IF EXISTS metadata_settings"))
