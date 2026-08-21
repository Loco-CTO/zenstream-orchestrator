import sqlalchemy as sa
from alembic import op

revision = "0040_watch_history_preference"
down_revision = "0039_remove_browser_push"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_preferences",
        sa.Column(
            "watch_history_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )


def downgrade() -> None:
    op.drop_column("account_preferences", "watch_history_enabled")
