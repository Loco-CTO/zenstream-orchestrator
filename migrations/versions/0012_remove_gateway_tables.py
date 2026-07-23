from alembic import op
import sqlalchemy as sa


revision = "0012_remove_gateway_tables"
down_revision = "0011_accounts_catalog"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("client_secrets", "user_preferences", "settings"):
        op.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))


def downgrade():
    op.execute(sa.text("CREATE TABLE IF NOT EXISTS settings (servername TEXT NOT NULL, origin_type INTEGER NOT NULL, origin_url TEXT NOT NULL, api_key TEXT NOT NULL)"))
    op.execute(sa.text("CREATE TABLE IF NOT EXISTS client_secrets (username TEXT NOT NULL, client_secret TEXT NOT NULL, expiration TEXT NOT NULL)"))
    op.execute(sa.text("CREATE TABLE IF NOT EXISTS user_preferences (account_key TEXT PRIMARY KEY NOT NULL, locale TEXT NOT NULL DEFAULT 'en')"))
