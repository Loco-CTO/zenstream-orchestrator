from alembic import op
import sqlalchemy as sa


revision = "0008_metadata_diagnostics"
down_revision = "0007_metadata_hydration"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("UPDATE metadata_cache SET payload=REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(payload, '\"type\": \"poster\"', '\"type\": \"Primary\"'), '\"type\": \"backdrop\"', '\"type\": \"Backdrop\"'), '\"type\": \"logo\"', '\"type\": \"Logo\"'), '\"type\": \"thumb\"', '\"type\": \"Banner\"'), '\"type\": \"Thumb\"', '\"type\": \"Banner\"') WHERE payload IS NOT NULL"))
    op.execute(sa.text("UPDATE metadata_images SET image_type='Primary' WHERE image_type='poster'"))
    op.execute(sa.text("UPDATE metadata_images SET image_type='Backdrop' WHERE image_type='backdrop'"))
    op.execute(sa.text("UPDATE metadata_images SET image_type='Logo' WHERE image_type='logo'"))
    op.execute(sa.text("UPDATE metadata_images SET image_type='Banner' WHERE image_type IN ('thumb', 'Thumb', 'thumbnail', 'banner')"))
    op.add_column("job_runs", sa.Column("error_details", sa.Text(), nullable=True))
    op.add_column("library_jobs", sa.Column("error_details", sa.Text(), nullable=True))
    op.add_column("metadata_hydration_requests", sa.Column("error_details", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("metadata_hydration_requests", "error_details")
    op.drop_column("library_jobs", "error_details")
    op.drop_column("job_runs", "error_details")
