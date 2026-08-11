"""Repair watcher ledger rows that incorrectly represented media files as directories."""

from pathlib import PurePosixPath

import sqlalchemy as sa
from alembic import op

revision = "0017_library_watch_ledger_repair"
down_revision = "0016_library_watch_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "library_watch_state" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("library_watch_state")}
    if "phase" not in columns:
        op.add_column(
            "library_watch_state",
            sa.Column("phase", sa.Text(), nullable=False, server_default="media"),
        )
    if "last_batch_at" not in columns:
        op.add_column("library_watch_state", sa.Column("last_batch_at", sa.Text()))
    if "last_error_code" not in columns:
        op.add_column("library_watch_state", sa.Column("last_error_code", sa.Text()))

    if not {"libraries", "library_entities", "media_files"}.issubset(tables):
        return

    rows = bind.execute(sa.text("SELECT id FROM libraries WHERE directory IS NOT NULL")).fetchall()
    for (library_id,) in rows:
        bind.execute(
            sa.text(
                "DELETE FROM library_watch_directories "
                "WHERE library_id=:library_id AND relative_path<>''"
            ),
            {"library_id": library_id},
        )
        media_rows = bind.execute(
            sa.text(
                "SELECT mf.relative_path FROM media_files mf "
                "JOIN library_entities e ON e.id=mf.entity_id "
                "WHERE e.library_id=:library_id AND mf.relative_path IS NOT NULL"
            ),
            {"library_id": library_id},
        ).fetchall()
        directories: set[tuple[str, str]] = {("", "")}
        for (relative_path,) in media_rows:
            parts = [part for part in PurePosixPath(str(relative_path).replace("\\", "/")).parts if part not in {"", "."}]
            for index in range(max(0, len(parts) - 1)):
                directories.add(("/".join(parts[: index + 1]), parts[0]))
        bind.execute(
            sa.text(
                "INSERT OR IGNORE INTO library_watch_directories "
                "(library_id,relative_path,top_level_root,complete) "
                "VALUES(:library_id,:relative_path,:top_level_root,0)"
            ),
            [
                {
                    "library_id": library_id,
                    "relative_path": relative_path,
                    "top_level_root": top_level_root,
                }
                for relative_path, top_level_root in directories
            ],
        )
        bind.execute(
            sa.text(
                "UPDATE library_watch_state SET cursor=0,generation=0," 
                "last_complete_at=NULL,phase='media',last_batch_at=NULL," 
                "last_error_code=NULL WHERE library_id=:library_id"
            ),
            {"library_id": library_id},
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("library_watch_state")}
    for name in ("last_error_code", "last_batch_at", "phase"):
        if name in columns:
            op.drop_column("library_watch_state", name)
