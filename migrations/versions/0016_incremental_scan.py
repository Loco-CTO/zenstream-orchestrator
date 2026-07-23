from alembic import op
import sqlalchemy as sa


revision = "0016_incremental_scan"
down_revision = "0015_metadata_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("media_files", sa.Column("file_hash", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("media_files", "file_hash")
