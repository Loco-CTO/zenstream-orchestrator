import sqlalchemy as sa
from alembic import op

revision = "0014_library_watcher_settings"
down_revision = "0013_invite_hardening"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("libraries")}
    added_safety_scan = False
    if "watch_mode" not in columns:
        op.add_column(
            "libraries",
            sa.Column("watch_mode", sa.Text(), nullable=False, server_default="auto"),
        )
    if "safety_scan_enabled" not in columns:
        added_safety_scan = True
        op.add_column(
            "libraries",
            sa.Column(
                "safety_scan_enabled", sa.Integer(), nullable=False, server_default="1"
            ),
        )
    op.execute(
        "UPDATE libraries SET watch_mode='auto' WHERE watch_mode IS NULL OR watch_mode=''"
    )
    if added_safety_scan:
        # Existing installations need the old watch_enabled value preserved;
        # the server default only applies to newly-created rows.
        op.execute("UPDATE libraries SET safety_scan_enabled=watch_enabled")
    else:
        op.execute(
            "UPDATE libraries SET safety_scan_enabled=watch_enabled "
            "WHERE safety_scan_enabled IS NULL"
        )


def downgrade():
    op.drop_column("libraries", "safety_scan_enabled")
    op.drop_column("libraries", "watch_mode")
