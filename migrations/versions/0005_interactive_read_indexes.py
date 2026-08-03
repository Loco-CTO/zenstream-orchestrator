from alembic import op


revision = "0005_interactive_read_indexes"
down_revision = "0004_metadata_image_lookup_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("idx_user_sessions_expiry", "user_sessions", ["expires_at"])
    op.create_index(
        "idx_library_entities_hierarchy_order",
        "library_entities",
        ["library_id", "entity_type", "parent_id", "season_number", "episode_number"],
    )


def downgrade():
    op.drop_index("idx_library_entities_hierarchy_order", table_name="library_entities")
    op.drop_index("idx_user_sessions_expiry", table_name="user_sessions")
