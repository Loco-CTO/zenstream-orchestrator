import sqlalchemy as sa
from alembic import op

revision = "0058_refresh_tokens"
down_revision = "0057_metadata_upgrade_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_sessions",
        sa.Column("access_expires_at", sa.Text(), nullable=True),
    )
    op.add_column(
        "user_sessions",
        sa.Column("refresh_family_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "user_sessions",
        sa.Column("revoked_at", sa.Text(), nullable=True),
    )
    op.execute(
        "UPDATE user_sessions SET access_expires_at=expires_at "
        "WHERE access_expires_at IS NULL"
    )
    op.create_table(
        "user_refresh_tokens",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("family_id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("used_at", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.Text(), nullable=True),
        sa.Column("replaced_by_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["user_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "idx_user_sessions_access_expiry",
        "user_sessions",
        ["access_expires_at"],
    )
    op.create_index(
        "idx_user_refresh_tokens_session",
        "user_refresh_tokens",
        ["session_id", "expires_at"],
    )
    op.create_index(
        "idx_user_refresh_tokens_family",
        "user_refresh_tokens",
        ["family_id", "revoked_at"],
    )
    op.create_index(
        "idx_user_refresh_tokens_expiry",
        "user_refresh_tokens",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_user_refresh_tokens_expiry",
        table_name="user_refresh_tokens",
    )
    op.drop_index(
        "idx_user_refresh_tokens_family",
        table_name="user_refresh_tokens",
    )
    op.drop_index(
        "idx_user_refresh_tokens_session",
        table_name="user_refresh_tokens",
    )
    op.drop_index(
        "idx_user_sessions_access_expiry",
        table_name="user_sessions",
    )
    op.drop_table("user_refresh_tokens")
    with op.batch_alter_table("user_sessions") as batch:
        batch.drop_column("revoked_at")
        batch.drop_column("refresh_family_id")
        batch.drop_column("access_expires_at")
