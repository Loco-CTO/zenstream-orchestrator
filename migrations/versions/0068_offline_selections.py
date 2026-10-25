import sqlalchemy as sa
from alembic import op

revision = "0068_offline_selections"
down_revision = "0067_playlist_sync_fields"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "offline_selections",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("root_id", sa.Text(), nullable=False),
        sa.Column("root_type", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
    )
    op.create_index(
        "idx_offline_selection_pending", "offline_selections", ["state", "created_at"]
    )
    op.create_table(
        "offline_selection_frontier",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "selection_id",
            sa.Text(),
            sa.ForeignKey("offline_selections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("page_offset", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done", sa.Boolean(), nullable=False, server_default="0"),
        sa.UniqueConstraint("selection_id", "entity_id"),
    )
    op.create_index(
        "idx_offline_frontier_pending",
        "offline_selection_frontier",
        ["selection_id", "done", "id"],
    )
    for table in ("offline_selection_items", "offline_selection_metadata"):
        columns = [
            sa.Column(
                "selection_id",
                sa.Text(),
                sa.ForeignKey("offline_selections.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("entity_id", sa.Text(), primary_key=True),
            sa.Column("payload", sa.Text(), nullable=False),
        ]
        if table.endswith("items"):
            columns.append(sa.Column("ordinal", sa.Integer(), nullable=False))
        op.create_table(table, *columns)
    op.create_index(
        "idx_offline_selection_order",
        "offline_selection_items",
        ["selection_id", "ordinal"],
    )


def downgrade():
    for name in (
        "offline_selection_metadata",
        "offline_selection_items",
        "offline_selection_frontier",
        "offline_selections",
    ):
        op.drop_table(name)
