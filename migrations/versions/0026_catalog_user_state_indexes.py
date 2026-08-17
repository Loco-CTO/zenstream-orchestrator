from alembic import op

revision = "0026_catalog_user_state_indexes"
down_revision = "0025_screen_extractor_artwork"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_item_state_continue "
        "ON user_item_state(user_id,COALESCE(last_played_at,updated_at) DESC,entity_id) "
        "WHERE played=0 AND duration_seconds>0 AND position_seconds>0"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_entities_season_order "
        "ON library_entities(parent_id,(season_number IS NULL),season_number,relative_path COLLATE NOCASE,id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_entities_episode_order "
        "ON library_entities(parent_id,(episode_number IS NULL),episode_number,relative_path COLLATE NOCASE,id)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_user_item_state_continue")
    op.execute("DROP INDEX IF EXISTS idx_library_entities_season_order")
    op.execute("DROP INDEX IF EXISTS idx_library_entities_episode_order")
