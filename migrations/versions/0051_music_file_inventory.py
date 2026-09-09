import sqlalchemy as sa
from alembic import op

revision = "0051_music_file_inventory"
down_revision = "0050_music_scanner_correctness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "music_file_inventory",
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("path_key", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("modified_ns", sa.Integer(), nullable=False),
        sa.Column("tag_snapshot_version", sa.Integer(), nullable=False),
        sa.Column("tag_fingerprint", sa.Text(), nullable=False),
        sa.Column("tag_payload", sa.Text(), nullable=False),
        sa.Column("group_key", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("library_id", "path_key"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["library_entities.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "idx_music_file_inventory_entity",
        "music_file_inventory",
        ["library_id", "entity_id"],
    )
    op.create_index(
        "idx_music_file_inventory_group",
        "music_file_inventory",
        ["library_id", "group_key"],
    )


def downgrade() -> None:
    op.drop_index("idx_music_file_inventory_group", table_name="music_file_inventory")
    op.drop_index("idx_music_file_inventory_entity", table_name="music_file_inventory")
    op.drop_table("music_file_inventory")
