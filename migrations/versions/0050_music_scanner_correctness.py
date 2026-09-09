from alembic import op
import sqlalchemy as sa


revision = "0050_music_scanner_correctness"
down_revision = "0049_artist_follow_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "music_identity_keys",
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("identity_source", sa.Text(), nullable=False),
        sa.Column("identity_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["library_entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("entity_id", "entity_type", "identity_key"),
    )
    op.create_index(
        "idx_music_identity_lookup",
        "music_identity_keys",
        ["library_id", "entity_type", "identity_key"],
    )
    op.create_index(
        "idx_music_identity_entity",
        "music_identity_keys",
        ["entity_id", "entity_type"],
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_music_identity_release_key "
        "ON music_identity_keys(library_id, identity_key) "
        "WHERE entity_type='release'"
    )


def downgrade() -> None:
    op.drop_index("idx_music_identity_release_key", table_name="music_identity_keys")
    op.drop_index("idx_music_identity_entity", table_name="music_identity_keys")
    op.drop_index("idx_music_identity_lookup", table_name="music_identity_keys")
    op.drop_table("music_identity_keys")
