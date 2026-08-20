import sqlalchemy as sa
from alembic import op

revision = "0037_calendar"
down_revision = "0036_user_avatars"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_connections",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False, server_default="/"),
        sa.Column("use_ssl", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=False),
        sa.Column("validated_at", sa.Text(), nullable=True),
        sa.Column("last_sync_at", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("provider"),
        sa.CheckConstraint("provider IN ('sonarr','radarr')", name="ck_calendar_connections_provider"),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="ck_calendar_connections_port"),
        sa.CheckConstraint("use_ssl IN (0,1)", name="ck_calendar_connections_use_ssl"),
    )

    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("release_type", sa.Text(), nullable=False),
        sa.Column("event_at", sa.Text(), nullable=False),
        sa.Column("event_date", sa.Text(), nullable=False),
        sa.Column("all_day", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tvdb_id", sa.Text(), nullable=True),
        sa.Column("tmdb_id", sa.Text(), nullable=True),
        sa.Column("series_tvdb_id", sa.Text(), nullable=True),
        sa.Column("season_number", sa.Integer(), nullable=True),
        sa.Column("episode_number", sa.Integer(), nullable=True),
        sa.Column("has_file", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("monitored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.Text(), nullable=False, server_default="future"),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "library_id",
            "source_event_id",
            "release_type",
            "event_at",
            name="uq_calendar_events_source",
        ),
        sa.CheckConstraint("provider IN ('sonarr','radarr')", name="ck_calendar_events_provider"),
        sa.CheckConstraint("kind IN ('episode','movie')", name="ck_calendar_events_kind"),
        sa.CheckConstraint("state IN ('future','existing')", name="ck_calendar_events_state"),
        sa.CheckConstraint("has_file IN (0,1)", name="ck_calendar_events_has_file"),
        sa.CheckConstraint("monitored IN (0,1)", name="ck_calendar_events_monitored"),
        sa.CheckConstraint("all_day IN (0,1)", name="ck_calendar_events_all_day"),
    )
    op.create_index("ix_calendar_events_window", "calendar_events", ["library_id", "event_at"])
    op.create_index(
        "ix_calendar_events_identity",
        "calendar_events",
        ["library_id", "tvdb_id", "tmdb_id", "series_tvdb_id"],
    )

    op.create_table(
        "calendar_event_entities",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["calendar_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["library_entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id", "entity_id"),
    )
    op.create_index("ix_calendar_event_entities_entity", "calendar_event_entities", ["entity_id"])

    op.create_table(
        "future_metadata_cache",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "entity_type", "provider_id", "locale"),
    )
    op.create_table(
        "future_metadata_images",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False, server_default=""),
        sa.Column("image_type", sa.Text(), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.Text(), nullable=True),
        sa.Column("blur_hash", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint(
            "provider",
            "entity_type",
            "provider_id",
            "locale",
            "image_type",
            "image_url",
        ),
    )
    op.create_index(
        "ix_future_metadata_cache_expiry",
        "future_metadata_cache",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_future_metadata_cache_expiry", table_name="future_metadata_cache")
    op.drop_table("future_metadata_images")
    op.drop_table("future_metadata_cache")
    op.drop_index("ix_calendar_event_entities_entity", table_name="calendar_event_entities")
    op.drop_table("calendar_event_entities")
    op.drop_index("ix_calendar_events_identity", table_name="calendar_events")
    op.drop_index("ix_calendar_events_window", table_name="calendar_events")
    op.drop_table("calendar_events")
    op.drop_table("calendar_connections")
