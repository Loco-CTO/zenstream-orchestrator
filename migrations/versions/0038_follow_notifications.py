import sqlalchemy as sa
from alembic import op

revision = "0038_follow_notifications"
down_revision = "0037_calendar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_follow_targets",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["library_entities.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "library_id",
            "target_type",
            "provider",
            "provider_id",
            name="uq_user_follow_targets_identity",
        ),
        sa.CheckConstraint(
            "target_type IN ('movie','series')",
            name="ck_user_follow_targets_type",
        ),
        sa.CheckConstraint(
            "provider IN ('tmdb','tvdb','entity')",
            name="ck_user_follow_targets_provider",
        ),
    )
    op.create_index(
        "ix_user_follow_targets_user",
        "user_follow_targets",
        ["user_id", "updated_at"],
    )
    op.create_index(
        "ix_user_follow_targets_identity",
        "user_follow_targets",
        ["library_id", "target_type", "provider", "provider_id"],
    )
    op.create_index(
        "ix_user_follow_targets_entity",
        "user_follow_targets",
        ["entity_id"],
    )

    op.create_table(
        "catalog_admissions",
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("admitted_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["library_entities.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("entity_id"),
        sa.CheckConstraint(
            "entity_type IN ('movie','episode')",
            name="ck_catalog_admissions_type",
        ),
    )
    op.create_index(
        "ix_catalog_admissions_library",
        "catalog_admissions",
        ["library_id", "admitted_at"],
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("series_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("subtitle", sa.Text(), nullable=True),
        sa.Column("season_number", sa.Integer(), nullable=True),
        sa.Column("episode_number", sa.Integer(), nullable=True),
        sa.Column("navigation_path", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("read_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["library_entities.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["series_id"], ["library_entities.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "dedupe_key", name="uq_notifications_user_dedupe"
        ),
        sa.CheckConstraint(
            "kind IN ('new_episode','new_movie')",
            name="ck_notifications_kind",
        ),
    )
    op.create_index(
        "ix_notifications_user_created",
        "notifications",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_notifications_user_unread",
        "notifications",
        ["user_id", "read_at", "created_at"],
    )

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


def downgrade() -> None:
    op.drop_index(
        "ix_notification_push_outbox_due", table_name="notification_push_outbox"
    )
    op.drop_table("notification_push_outbox")
    op.drop_index(
        "ix_notification_push_subscriptions_user",
        table_name="notification_push_subscriptions",
    )
    op.drop_table("notification_push_subscriptions")
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_catalog_admissions_library", table_name="catalog_admissions")
    op.drop_table("catalog_admissions")
    op.drop_index("ix_user_follow_targets_entity", table_name="user_follow_targets")
    op.drop_index("ix_user_follow_targets_identity", table_name="user_follow_targets")
    op.drop_index("ix_user_follow_targets_user", table_name="user_follow_targets")
    op.drop_table("user_follow_targets")
