import sqlalchemy as sa
from alembic import op

revision = "0059_playlists"
down_revision = "0058_refresh_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_playlists",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("share_token", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_user_playlists_name"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("share_token", name="uq_user_playlists_share_token"),
    )
    op.create_index(
        "ix_user_playlists_user_updated",
        "user_playlists",
        ["user_id", "updated_at"],
    )
    op.create_table(
        "user_playlist_items",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("playlist_id", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["playlist_id"], ["user_playlists.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["library_entities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "playlist_id", "entity_id", name="uq_user_playlist_items_entity"
        ),
    )
    op.create_index(
        "ix_user_playlist_items_order",
        "user_playlist_items",
        ["playlist_id", "position", "id"],
    )
    op.execute(
        "CREATE TRIGGER user_playlist_items_track_only_insert "
        "BEFORE INSERT ON user_playlist_items "
        "WHEN (SELECT entity_type FROM library_entities WHERE id=NEW.entity_id) != 'track' "
        "BEGIN SELECT RAISE(ABORT, 'playlist items must reference music tracks'); END"
    )
    op.execute(
        "CREATE TRIGGER user_playlist_items_track_only_update "
        "BEFORE UPDATE OF entity_id ON user_playlist_items "
        "WHEN (SELECT entity_type FROM library_entities WHERE id=NEW.entity_id) != 'track' "
        "BEGIN SELECT RAISE(ABORT, 'playlist items must reference music tracks'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS user_playlist_items_track_only_update")
    op.execute("DROP TRIGGER IF EXISTS user_playlist_items_track_only_insert")
    op.drop_index("ix_user_playlist_items_order", table_name="user_playlist_items")
    op.drop_table("user_playlist_items")
    op.drop_index("ix_user_playlists_user_updated", table_name="user_playlists")
    op.drop_table("user_playlists")
