import sqlalchemy as sa
from alembic import op

revision = "0036_user_avatars"
down_revision = "0035_subtitle_outline_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_avatars",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("file_format", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
        sa.CheckConstraint(
            "file_format IN ('webp','gif')",
            name="ck_user_avatars_file_format",
        ),
    )


def downgrade() -> None:
    op.drop_table("user_avatars")
