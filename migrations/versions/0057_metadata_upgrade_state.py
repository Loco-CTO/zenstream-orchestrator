import sqlalchemy as sa
from alembic import op

revision = "0057_metadata_upgrade_state"
down_revision = "0056_metadata_missing_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metadata_upgrade_state",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("upgrade_version", sa.Integer(), nullable=False),
        sa.Column("document_digest", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "entity_type", "provider_id", "locale"),
    )


def downgrade() -> None:
    op.drop_table("metadata_upgrade_state")
