import json

import sqlalchemy as sa
from alembic import op

revision = "0020_revert_library_watcher_extensions"
down_revision = "0019_library_watch_directory_cursor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "job_definitions" in tables and "libraries" in tables:
        libraries = {
            row[0]: row[1:]
            for row in bind.execute(
                sa.text("SELECT id, name, watch_enabled FROM libraries")
            ).fetchall()
        }
        definitions = bind.execute(
            sa.text(
                "SELECT id, job_key, kind, config, enabled "
                "FROM job_definitions WHERE kind='library_delta_verify'"
            )
        ).fetchall()
        for definition_id, _job_key, _kind, config, _enabled in definitions:
            try:
                library_id = json.loads(config or "{}").get("libraryId")
            except (TypeError, ValueError):
                library_id = None
            library = libraries.get(library_id)
            if library:
                name, watch_enabled = library
                bind.execute(
                    sa.text(
                        "UPDATE job_definitions SET kind='library_scan', "
                        "name=:name, description=:description, enabled=:enabled "
                        "WHERE id=:id"
                    ),
                    {
                        "id": definition_id,
                        "name": f"Scan {name}",
                        "description": (
                            "Index the library without moving or renaming files."
                        ),
                        "enabled": int(bool(watch_enabled)),
                    },
                )
            else:
                bind.execute(
                    sa.text(
                        "UPDATE job_definitions SET kind='library_scan' "
                        "WHERE id=:id"
                    ),
                    {"id": definition_id},
                )

    if "library_watch_pending_roots" in tables:
        op.drop_table("library_watch_pending_roots")
    if "library_watch_state" in tables:
        op.drop_table("library_watch_state")
    if "library_watch_directories" in tables:
        if "idx_library_watch_directories_root" in {
            index["name"]
            for index in inspector.get_indexes("library_watch_directories")
        }:
            op.drop_index(
                "idx_library_watch_directories_root",
                table_name="library_watch_directories",
            )
        op.drop_table("library_watch_directories")

    columns = {column["name"] for column in inspector.get_columns("libraries")}
    if "safety_scan_enabled" in columns:
        op.drop_column("libraries", "safety_scan_enabled")
    if "watch_mode" in columns:
        op.drop_column("libraries", "watch_mode")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("libraries")}
    if "watch_mode" not in columns:
        op.add_column(
            "libraries",
            sa.Column("watch_mode", sa.Text(), nullable=False, server_default="auto"),
        )
    if "safety_scan_enabled" not in columns:
        op.add_column(
            "libraries",
            sa.Column(
                "safety_scan_enabled", sa.Integer(), nullable=False, server_default="1"
            ),
        )
    bind.execute(
        sa.text(
            "UPDATE libraries SET safety_scan_enabled=watch_enabled "
            "WHERE safety_scan_enabled IS NULL"
        )
    )

    tables = set(sa.inspect(bind).get_table_names())
    if "library_watch_directories" not in tables:
        op.create_table(
            "library_watch_directories",
            sa.Column("library_id", sa.Text(), nullable=False),
            sa.Column("relative_path", sa.Text(), nullable=False),
            sa.Column("top_level_root", sa.Text(), nullable=False),
            sa.Column("mtime_ns", sa.Integer()),
            sa.Column("entry_signature", sa.Text()),
            sa.Column("observed_at", sa.Text()),
            sa.Column("complete", sa.Integer(), nullable=False, server_default="0"),
            sa.PrimaryKeyConstraint("library_id", "relative_path"),
            sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="CASCADE"),
        )
        op.create_index(
            "idx_library_watch_directories_root",
            "library_watch_directories",
            ["library_id", "top_level_root"],
        )
    if "library_watch_state" not in tables:
        op.create_table(
            "library_watch_state",
            sa.Column("library_id", sa.Text(), nullable=False),
            sa.Column("mount_identity", sa.Text()),
            sa.Column("cursor", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_complete_at", sa.Text()),
            sa.Column("native_verified_at", sa.Text()),
            sa.Column("phase", sa.Text(), nullable=False, server_default="media"),
            sa.Column("last_batch_at", sa.Text()),
            sa.Column("last_error_code", sa.Text()),
            sa.Column("directory_cursor", sa.Integer(), nullable=False, server_default="0"),
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
