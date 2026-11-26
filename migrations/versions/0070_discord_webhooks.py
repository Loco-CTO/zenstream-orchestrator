import sqlalchemy as sa
from alembic import op

revision = "0070_discord_webhooks"
down_revision = "0069_offline_assets"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "discord_webhooks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url_ciphertext", sa.Text(), nullable=False),
        sa.Column("discord_id", sa.Text(), nullable=False, unique=True),
        sa.Column("events", sa.Text(), nullable=False),
        sa.Column("send_after", sa.Float(), nullable=False, server_default="0"),
        sa.Column("last_result", sa.Text()),
        sa.Column("blocked", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_table(
        "discord_webhook_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("webhook_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created", sa.Float(), nullable=False),
        sa.Column("due", sa.Float(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_discord_delivery_due", "discord_webhook_deliveries", ["due", "id"]
    )
    op.create_index(
        "idx_discord_delivery_hook", "discord_webhook_deliveries", ["webhook_id"]
    )
    op.create_table(
        "discord_viewer_activity",
        sa.Column("viewer_id", sa.Text(), primary_key=True),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("milestone", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("discord_viewer_activity")
    op.drop_table("discord_webhook_deliveries")
    op.drop_table("discord_webhooks")
