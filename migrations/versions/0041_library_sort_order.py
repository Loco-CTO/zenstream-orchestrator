import sqlalchemy as sa
from alembic import op

revision = "0041_library_sort_order"
down_revision = "0040_watch_history_preference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column(
            "sort_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("libraries", "sort_order")
