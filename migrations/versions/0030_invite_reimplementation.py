"""Add reusable invites with library grants and atomic usage metadata."""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0030_invite_reimplementation"
down_revision = "0029_ffmpeg_thread_limits"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("invites")}
    if "id" not in columns:
        op.add_column("invites", sa.Column("id", sa.Text(), nullable=True))
    if "max_uses" not in columns:
        op.add_column(
            "invites",
            sa.Column("max_uses", sa.Integer(), nullable=True, server_default="1"),
        )
    if "used_uses" not in columns:
        op.add_column(
            "invites",
            sa.Column("used_uses", sa.Integer(), nullable=True, server_default="0"),
        )
    if "created_at" not in columns:
        op.add_column("invites", sa.Column("created_at", sa.Text(), nullable=True))

    rows = bind.execute(
        sa.text("SELECT url,consumed_at FROM invites WHERE id IS NULL")
    ).fetchall()
    for url, consumed_at in rows:
        bind.execute(
            sa.text(
                "UPDATE invites SET id=:id, max_uses=1, used_uses=:used, "
                "created_at=:created WHERE url=:url"
            ),
            {
                "id": str(uuid.uuid4()),
                "used": 1 if consumed_at else 0,
                "created": "1970-01-01T00:00:00+00:00",
                "url": url,
            },
        )
    bind.execute(sa.text("UPDATE invites SET max_uses=1 WHERE max_uses IS NULL"))
    bind.execute(sa.text("UPDATE invites SET used_uses=0 WHERE used_uses IS NULL"))
    bind.execute(
        sa.text(
            "UPDATE invites SET created_at='1970-01-01T00:00:00+00:00' "
            "WHERE created_at IS NULL"
        )
    )
    op.create_index("uq_invites_id", "invites", ["id"], unique=True, if_not_exists=True)
    op.create_table(
        "invite_library_access",
        sa.Column("invite_id", sa.Text(), nullable=False),
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["invite_id"], ["invites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("invite_id", "library_id"),
    )
    op.create_index(
        "idx_invite_library_access_library",
        "invite_library_access",
        ["library_id"],
        if_not_exists=True,
    )


def downgrade():
    op.drop_index(
        "idx_invite_library_access_library", table_name="invite_library_access"
    )
    op.drop_table("invite_library_access")
    op.drop_index("uq_invites_id", table_name="invites")
    op.drop_column("invites", "created_at")
    op.drop_column("invites", "used_uses")
    op.drop_column("invites", "max_uses")
    op.drop_column("invites", "id")
