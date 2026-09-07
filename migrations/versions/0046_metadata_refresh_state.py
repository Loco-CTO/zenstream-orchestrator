import sqlalchemy as sa
from alembic import op

revision = "0046_metadata_refresh_state"
down_revision = "0045_remove_subtitle_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metadata_refresh_state",
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("last_attempted_at", sa.Text(), nullable=True),
        sa.Column("last_completed_at", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["library_entities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("entity_id"),
    )
    op.create_index(
        "idx_metadata_refresh_state_attempted",
        "metadata_refresh_state",
        ["last_attempted_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_metadata_refresh_state_attempted",
        table_name="metadata_refresh_state",
    )
    op.drop_table("metadata_refresh_state")
