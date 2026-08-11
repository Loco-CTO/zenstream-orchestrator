"""Keep directory and media delta cursors independent."""

import sqlalchemy as sa
from alembic import op

revision = "0019_library_watch_directory_cursor"
down_revision = "0018_library_watch_pending_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "library_watch_state" not in set(sa.inspect(bind).get_table_names()):
        return
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("library_watch_state")
    }
    if "directory_cursor" not in columns:
        op.add_column(
            "library_watch_state",
            sa.Column("directory_cursor", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("library_watch_state")
    }
    if "directory_cursor" in columns:
        op.drop_column("library_watch_state", "directory_cursor")
