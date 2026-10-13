import sqlalchemy as sa
from alembic import op

revision = "0063_sonarr_calendar_api_version"
down_revision = "0062_bounded_home_recommendations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calendar_connections",
        sa.Column("api_version", sa.Text(), nullable=False, server_default="v3"),
    )


def downgrade() -> None:
    op.drop_column("calendar_connections", "api_version")
