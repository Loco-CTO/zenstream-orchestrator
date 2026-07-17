from alembic import op
import sqlalchemy as sa

revision = "0004_user_disabled"
down_revision = "0003_admin_accounts"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
    if "disabled" not in columns:
        op.add_column("users", sa.Column("disabled", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    pass
