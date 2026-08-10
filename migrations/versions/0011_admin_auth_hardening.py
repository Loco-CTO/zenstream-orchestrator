import sqlalchemy as sa
from alembic import op

revision = "0011_admin_auth_hardening"
down_revision = "0010_artwork_selection_rebuild"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "admins",
        sa.Column(
            "password_scheme",
            sa.Text(),
            nullable=False,
            server_default="sha256",
        ),
    )
    # Legacy administrator bearer tokens were stored in plaintext and their
    # recorded expiration was never enforced. Revoke them rather than carrying
    # reusable secrets into the hardened schema.
    op.drop_table("admin_sessions")
    op.create_table(
        "admin_sessions",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["username"],
            ["admins.username"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
    )
    op.create_index(
        "idx_admin_sessions_expiry", "admin_sessions", ["expires_at"], unique=False
    )


def downgrade():
    op.drop_index("idx_admin_sessions_expiry", table_name="admin_sessions")
    op.drop_table("admin_sessions")
    op.create_table(
        "admin_sessions",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False, unique=True),
        sa.Column("expiration", sa.Text(), nullable=False),
    )
    op.drop_column("admins", "password_scheme")
