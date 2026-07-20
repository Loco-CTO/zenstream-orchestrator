from alembic import op
import sqlalchemy as sa

revision = "0003_admin_accounts"
down_revision = "0002_syncplay_watching_together"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text(
            "CREATE TABLE IF NOT EXISTS admins (username TEXT PRIMARY KEY NOT NULL, password TEXT NOT NULL, is_root INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0)"
        )
    )
    op.execute(
        sa.text(
            "CREATE TABLE IF NOT EXISTS admin_sessions (username TEXT NOT NULL, token TEXT UNIQUE NOT NULL, expiration TEXT NOT NULL)"
        )
    )
    conn = op.get_bind()
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
    if "disabled" not in columns:
        op.add_column(
            "users",
            sa.Column("disabled", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade():
    op.drop_table("admin_sessions")
    op.drop_table("admins")
