from alembic import op


revision = "0002_quick_fingerprint"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("media_files") as batch_op:
        batch_op.alter_column("file_hash", new_column_name="quick_fingerprint")


def downgrade():
    with op.batch_alter_table("media_files") as batch_op:
        batch_op.alter_column("quick_fingerprint", new_column_name="file_hash")
