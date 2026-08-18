import sqlalchemy as sa
from alembic import op

revision = "0035_subtitle_outline_default"
down_revision = "0034_intro_outro_comparison_state"
branch_labels = None
depends_on = None


def _set_outline_default(value: str) -> None:
    with op.batch_alter_table("account_preferences", recreate="always") as batch:
        batch.alter_column(
            "subtitle_border_size",
            existing_type=sa.Float(),
            existing_nullable=False,
            server_default=sa.text(value),
        )


def upgrade() -> None:
    _set_outline_default("2")


def downgrade() -> None:
    _set_outline_default("0")
