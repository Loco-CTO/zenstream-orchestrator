"""Canonicalize metadata artwork values for the shared metadata policy."""

from alembic import op
import sqlalchemy as sa


revision = "0015_metadata_policy"
down_revision = "0014_refresh_trailer_metadata"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("UPDATE metadata_images SET image_type='Logo' WHERE image_type IN ('logo', 'clearlogo', 'clear-logo')"))
    op.execute(sa.text("UPDATE metadata_images SET image_type='Primary' WHERE image_type IN ('poster', 'primary')"))
    op.execute(sa.text("UPDATE metadata_images SET image_type='Backdrop' WHERE image_type IN ('backdrop', 'background')"))
    op.execute(sa.text("UPDATE metadata_images SET image_type='Banner' WHERE image_type IN ('banner', 'thumb', 'Thumb', 'thumbnail')"))
    op.execute(sa.text(
        "UPDATE metadata_cache SET payload=REPLACE(REPLACE(REPLACE(REPLACE(payload, '\"type\": \"logo\"', '\"type\": \"Logo\"'), '\"type\": \"clearlogo\"', '\"type\": \"Logo\"'), '\"type\": \"poster\"', '\"type\": \"Primary\"'), '\"type\": \"backdrop\"', '\"type\": \"Backdrop\"') WHERE payload IS NOT NULL"
    ))


def downgrade():
    pass
