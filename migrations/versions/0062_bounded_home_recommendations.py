"""Add indexes for bounded Home recommendation reads."""

from alembic import op

revision = "0062_bounded_home_recommendations"
down_revision = "0061_refresh_attempt_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_item_state_activity "
        "ON user_item_state(user_id,COALESCE(last_played_at,updated_at) DESC,entity_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_item_state_completed_activity "
        "ON user_item_state(user_id,COALESCE(last_played_at,updated_at) DESC,entity_id) "
        "WHERE played=1"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_item_state_favorites "
        "ON user_item_state(user_id,updated_at DESC,entity_id) WHERE favorite=1"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_item_genres_recommend "
        "ON catalog_item_genres(locale,genre_key,library_id,entity_type,entity_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_entities_home_recent "
        "ON library_entities(library_id,entity_type,parent_id,created_at DESC,id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_entity_summary_home_added "
        "ON catalog_entity_summary(library_id,entity_type,parent_id,added_sort_ns DESC,entity_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_entity_summary_home_last "
        "ON catalog_entity_summary(library_id,entity_type,parent_id,last_added_sort_ns DESC,entity_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_catalog_entity_summary_home_last")
    op.execute("DROP INDEX IF EXISTS idx_catalog_entity_summary_home_added")
    op.execute("DROP INDEX IF EXISTS idx_library_entities_home_recent")
    op.execute("DROP INDEX IF EXISTS idx_catalog_item_genres_recommend")
    op.execute("DROP INDEX IF EXISTS idx_user_item_state_favorites")
    op.execute("DROP INDEX IF EXISTS idx_user_item_state_completed_activity")
    op.execute("DROP INDEX IF EXISTS idx_user_item_state_activity")
