import sqlalchemy as sa
from alembic import op

revision = "0042_bazarr_subtitles"
down_revision = "0041_library_sort_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bazarr_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("use_ssl", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_bazarr_settings_singleton"),
        sa.CheckConstraint(
            "port BETWEEN 1 AND 65535", name="ck_bazarr_settings_port"
        ),
        sa.CheckConstraint("use_ssl IN (0,1)", name="ck_bazarr_settings_use_ssl"),
    )
    op.create_table(
        "bazarr_library_mappings",
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("bazarr_root_path", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("library_id"),
    )


def downgrade() -> None:
    op.drop_table("bazarr_library_mappings")
    op.drop_table("bazarr_settings")
