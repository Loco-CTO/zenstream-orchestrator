"""Metadata providers, native library inventory, and scan jobs."""

from alembic import op
import sqlalchemy as sa


revision = "0005_metadata_libraries"
down_revision = "0004_user_disabled"
branch_labels = None
depends_on = None


def upgrade():
    statements = [
        """
        CREATE TABLE IF NOT EXISTS metadata_credentials (
            provider TEXT PRIMARY KEY NOT NULL,
            ciphertext TEXT NOT NULL,
            credential_type TEXT NOT NULL DEFAULT 'api_key',
            validated_at TEXT,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS libraries (
            id TEXT PRIMARY KEY NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('tv_series','movies','music','collection')),
            directory TEXT,
            watch_enabled INTEGER NOT NULL DEFAULT 1,
            scan_interval_minutes INTEGER NOT NULL DEFAULT 1440,
            scan_state TEXT NOT NULL DEFAULT 'idle',
            scan_error TEXT,
            last_scan_started_at TEXT,
            last_scan_finished_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK((type = 'collection' AND directory IS NULL) OR (type <> 'collection' AND directory IS NOT NULL))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS library_sources (
            library_id TEXT NOT NULL,
            source_library_id TEXT NOT NULL,
            PRIMARY KEY(library_id, source_library_id),
            FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE,
            FOREIGN KEY(source_library_id) REFERENCES libraries(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS library_entities (
            id TEXT PRIMARY KEY NOT NULL,
            library_id TEXT NOT NULL,
            parent_id TEXT,
            entity_type TEXT NOT NULL,
            relative_path TEXT,
            season_number INTEGER,
            episode_number INTEGER,
            episode_end_number INTEGER,
            disc_number INTEGER,
            track_number INTEGER,
            match_status TEXT NOT NULL DEFAULT 'unresolved',
            match_confidence REAL,
            match_method TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(library_id, entity_type, relative_path),
            FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE,
            FOREIGN KEY(parent_id) REFERENCES library_entities(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS entity_provider_ids (
            entity_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            identifier_type TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(entity_id, provider, identifier_type),
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS media_files (
            id TEXT PRIMARY KEY NOT NULL,
            entity_id TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            role TEXT NOT NULL,
            language TEXT,
            flags TEXT,
            size INTEGER NOT NULL DEFAULT 0,
            modified_ns INTEGER NOT NULL DEFAULT 0,
            UNIQUE(entity_id, relative_path, role),
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS collection_members (
            collection_entity_id TEXT NOT NULL,
            source_entity_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY(collection_entity_id, source_entity_id),
            FOREIGN KEY(collection_entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(source_entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS library_jobs (
            id TEXT PRIMARY KEY NOT NULL,
            library_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            progress_current INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            message TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS metadata_cache (
            provider TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            locale TEXT NOT NULL,
            payload TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY(provider, entity_type, provider_id, locale)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS metadata_images (
            provider TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            locale TEXT,
            image_type TEXT NOT NULL,
            image_url TEXT NOT NULL,
            local_path TEXT,
            fetched_at TEXT,
            expires_at TEXT,
            PRIMARY KEY(provider, entity_type, provider_id, locale, image_type, image_url)
        )
        """,
    ]
    for statement in statements:
        op.execute(sa.text(statement))

    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_library_entities_parent ON library_entities(library_id, parent_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_library_entities_status ON library_entities(library_id, match_status)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_entity_provider_ids_lookup ON entity_provider_ids(provider, identifier_type, provider_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_library_jobs_state ON library_jobs(library_id, state, created_at)"))


def downgrade():
    for table in (
        "metadata_images",
        "metadata_cache",
        "library_jobs",
        "collection_members",
        "media_files",
        "entity_provider_ids",
        "library_entities",
        "library_sources",
        "libraries",
        "metadata_credentials",
    ):
        op.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
