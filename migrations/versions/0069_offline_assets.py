import sqlalchemy as sa
from alembic import op

revision = "0069_offline_assets"
down_revision = "0068_offline_selections"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "offline_assets",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("created_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_ms", sa.BigInteger(), nullable=False),
        sa.Column("lease_until", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("worker_id", sa.Text()),
    )
    op.create_index(
        "idx_offline_assets_pending", "offline_assets", ["state", "created_ms"]
    )
    op.create_table(
        "offline_asset_reservations",
        sa.Column(
            "asset_id",
            sa.Text(),
            sa.ForeignKey("offline_assets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "selection_id",
            sa.Text(),
            sa.ForeignKey("offline_selections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "idx_offline_reservations_expiry", "offline_asset_reservations", ["expires_ms"]
    )


def downgrade():
    op.drop_table("offline_asset_reservations")
    op.drop_table("offline_assets")
