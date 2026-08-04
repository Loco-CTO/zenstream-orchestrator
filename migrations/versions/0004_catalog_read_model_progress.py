from alembic import op


revision = "0004_catalog_read_model_progress"
down_revision = "0003_catalog_read_model"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "catalog_read_model_status",
        op.Column("stage", op.Text(), nullable=False, server_default="idle"),
    )
    op.add_column(
        "catalog_read_model_status",
        op.Column("processed", op.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "catalog_read_model_status",
        op.Column("total", op.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "catalog_read_model_status",
        op.Column("started_at", op.Text(), nullable=True),
    )
    op.add_column(
        "catalog_read_model_status",
        op.Column("heartbeat_at", op.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("catalog_read_model_status", "heartbeat_at")
    op.drop_column("catalog_read_model_status", "started_at")
    op.drop_column("catalog_read_model_status", "total")
    op.drop_column("catalog_read_model_status", "processed")
    op.drop_column("catalog_read_model_status", "stage")
