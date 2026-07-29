from alembic import op
import sqlalchemy as sa


revision = "0023_subtitle_renderer"
down_revision = "0022_intro_outro_detection"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "account_preferences",
        sa.Column("subtitle_renderer", sa.Text(), nullable=False, server_default="native"),
    )


def downgrade():
    op.drop_column("account_preferences", "subtitle_renderer")
