import sqlalchemy as sa
from alembic import op

revision = "0052_music_scan_stats"
down_revision = "0051_music_file_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("library_jobs", sa.Column("scan_stats", sa.Text(), nullable=True))
    op.add_column("job_runs", sa.Column("scan_stats", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("job_runs", "scan_stats")
    op.drop_column("library_jobs", "scan_stats")
