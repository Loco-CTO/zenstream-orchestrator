from alembic import op
import sqlalchemy as sa


revision = "0002_syncplay_watching_together"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(syncplay_members)")}
    if "watching_together" not in columns:
        op.add_column(
            "syncplay_members",
            sa.Column("watching_together", sa.Integer(), nullable=False, server_default="1"),
        )


def downgrade():
    pass
