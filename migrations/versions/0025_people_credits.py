from alembic import op
import sqlalchemy as sa


revision = "0025_people_credits"
down_revision = "0024_artwork_blurhash"
branch_labels = None
depends_on = None


def upgrade():
    statements = [
        """CREATE TABLE IF NOT EXISTS people (
            id TEXT PRIMARY KEY NOT NULL,
            provider TEXT NOT NULL,
            provider_person_id TEXT NOT NULL,
            image_url TEXT,
            local_path TEXT,
            image_blur_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(provider,provider_person_id)
        )""",
        """CREATE TABLE IF NOT EXISTS person_localizations (
            person_id TEXT NOT NULL,
            locale TEXT NOT NULL,
            name TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(person_id,locale),
            FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS entity_person_credits (
            id TEXT PRIMARY KEY NOT NULL,
            entity_id TEXT NOT NULL,
            person_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            locale TEXT NOT NULL,
            credit_type TEXT NOT NULL,
            role TEXT,
            department TEXT,
            credit_order INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE CASCADE
        )""",
    ]
    for statement in statements:
        op.execute(sa.text(statement))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_entity_person_credits_item ON entity_person_credits(entity_id,locale,credit_type,credit_order)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_entity_person_credits_person ON entity_person_credits(person_id)"))


def downgrade():
    op.execute(sa.text("DROP TABLE IF EXISTS entity_person_credits"))
    op.execute(sa.text("DROP TABLE IF EXISTS person_localizations"))
    op.execute(sa.text("DROP TABLE IF EXISTS people"))
