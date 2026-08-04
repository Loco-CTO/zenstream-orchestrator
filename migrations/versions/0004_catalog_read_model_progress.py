from alembic import op
from sqlalchemy import Column, Integer, Text


revision = "0004_catalog_read_model_progress"
down_revision = "0003_catalog_read_model"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "catalog_read_model_status",
        Column("stage", Text(), nullable=False, server_default="idle"),
    )
    op.add_column(
        "catalog_read_model_status",
        Column("processed", Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "catalog_read_model_status",
        Column("total", Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "catalog_read_model_status",
        Column("started_at", Text(), nullable=True),
    )
    op.add_column(
        "catalog_read_model_status",
        Column("heartbeat_at", Text(), nullable=True),
    )


def downgrade():
    op.drop_column("catalog_read_model_status", "heartbeat_at")
    op.drop_column("catalog_read_model_status", "started_at")
    op.drop_column("catalog_read_model_status", "total")
    op.drop_column("catalog_read_model_status", "processed")
    op.drop_column("catalog_read_model_status", "stage")
