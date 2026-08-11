"""Persist shallow filesystem observations for offline watcher catch-up."""
import sqlalchemy as sa
from alembic import op

revision = "0016_library_watch_ledger"
down_revision = "0015_catalog_discovery_added_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "library_watch_directories" not in tables:
        op.create_table(
        "library_watch_directories",
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("top_level_root", sa.Text(), nullable=False),
        sa.Column("mtime_ns", sa.Integer(), nullable=True),
        sa.Column("entry_signature", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.Text(), nullable=True),
        sa.Column("complete", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("library_id", "relative_path"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        )
    if "idx_library_watch_directories_root" not in {x["name"] for x in inspector.get_indexes("library_watch_directories")}:
        op.create_index("idx_library_watch_directories_root", "library_watch_directories", ["library_id", "top_level_root"])
    if "library_watch_state" not in tables:
        op.create_table(
        "library_watch_state",
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("mount_identity", sa.Text(), nullable=True),
        sa.Column("cursor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_complete_at", sa.Text(), nullable=True),
        sa.Column("native_verified_at", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("library_id"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        )
    if "library_watch_pending_roots" not in tables:
        op.create_table(
        "library_watch_pending_roots",
        sa.Column("library_id", sa.Text(), nullable=False),
        sa.Column("top_level_root", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("library_id", "top_level_root"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "library_watch_pending_roots" in tables:
        op.drop_table("library_watch_pending_roots")
    if "library_watch_state" in tables:
        op.drop_table("library_watch_state")
    if "library_watch_directories" in tables:
        if "idx_library_watch_directories_root" in {x["name"] for x in inspector.get_indexes("library_watch_directories") }:
            op.drop_index("idx_library_watch_directories_root", table_name="library_watch_directories")
        op.drop_table("library_watch_directories")
