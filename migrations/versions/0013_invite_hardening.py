"""Add expiry/consumption metadata for cryptographic invite tokens."""

import sqlalchemy as sa
from alembic import op

revision = "0013_invite_hardening"
down_revision = "0012_reverse_fk_indexes"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("invites")}
    if "expires_at" not in columns:
        op.add_column("invites", sa.Column("expires_at", sa.Text(), nullable=True))
    if "consumed_at" not in columns:
        op.add_column("invites", sa.Column("consumed_at", sa.Text(), nullable=True))
    op.create_index("idx_invites_expiry", "invites", ["expires_at"], if_not_exists=True)


def downgrade():
    op.drop_index("idx_invites_expiry", table_name="invites", if_exists=True)
    op.drop_column("invites", "consumed_at")
    op.drop_column("invites", "expires_at")
