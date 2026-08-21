import sqlalchemy as sa
from alembic import op

revision = "0039_remove_browser_push"
down_revision = "0038_follow_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_notification_push_outbox_due", table_name="notification_push_outbox"
    )
    op.drop_table("notification_push_outbox")
    op.drop_index(
        "ix_notification_push_subscriptions_user",
        table_name="notification_push_subscriptions",
    )
    op.drop_table("notification_push_subscriptions")


def downgrade() -> None:
    op.create_table(
        "notification_push_subscriptions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("expiration_time", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "endpoint", name="uq_notification_push_subscriptions_endpoint"
        ),
    )
    op.create_index(
        "ix_notification_push_subscriptions_user",
        "notification_push_subscriptions",
        ["user_id", "updated_at"],
    )

    op.create_table(
        "notification_push_outbox",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("notification_id", sa.Text(), nullable=False),
        sa.Column("subscription_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.Text(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("delivered_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["notification_id"], ["notifications.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["notification_push_subscriptions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "notification_id",
            "subscription_id",
            name="uq_notification_push_outbox_delivery",
        ),
        sa.CheckConstraint(
            "state IN ('queued','retry','delivered','failed')",
            name="ck_notification_push_outbox_state",
        ),
    )
    op.create_index(
        "ix_notification_push_outbox_due",
        "notification_push_outbox",
        ["state", "next_attempt_at"],
    )
