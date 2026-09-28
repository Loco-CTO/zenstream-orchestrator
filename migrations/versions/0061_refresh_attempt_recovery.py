import sqlalchemy as sa
from alembic import op

revision = "0061_refresh_attempt_recovery"
down_revision = "0060_playback_access_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_refresh_tokens",
        sa.Column("rotation_attempt_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "user_refresh_tokens",
        sa.Column("rotation_response_ciphertext", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("user_refresh_tokens") as batch:
        batch.drop_column("rotation_response_ciphertext")
        batch.drop_column("rotation_attempt_id")
