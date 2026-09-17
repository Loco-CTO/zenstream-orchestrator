import sqlalchemy as sa
from alembic import op

revision = "0056_metadata_missing_state"
down_revision = "0055_metadata_cleanup_path_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metadata_missing_state",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.Text(), nullable=False, server_default="retry"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.Text(), nullable=True),
        sa.Column("source_job_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "entity_type", "provider_id", "locale"),
    )
    op.create_index(
        "idx_metadata_missing_state_due",
        "metadata_missing_state",
        ["state", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_metadata_missing_state_due",
        table_name="metadata_missing_state",
    )
    op.drop_table("metadata_missing_state")
