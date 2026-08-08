from alembic import op


revision = "0005_sqlalchemy_persistence_indexes"
down_revision = "0004_catalog_read_model_progress"
branch_labels = None
depends_on = None


def _create_metadata_images(nullable_locale: bool) -> None:
    locale = "TEXT" if nullable_locale else "TEXT NOT NULL DEFAULT ''"
    op.execute(
        f"""
        CREATE TABLE metadata_images_next (
            provider TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            locale {locale},
            image_type TEXT NOT NULL,
            image_url TEXT NOT NULL,
            local_path TEXT,
            fetched_at TEXT,
            expires_at TEXT,
            blur_hash TEXT,
            PRIMARY KEY(provider, entity_type, provider_id, locale, image_type, image_url)
        )
        """
    )


def upgrade():
    bind = op.get_bind()
    queue_columns = {
        row[1]
        for row in bind.exec_driver_sql("PRAGMA table_info(enrichment_queue)")
    }
    for name, definition in (
        ("next_attempt_at", "TEXT"),
        ("lease_owner", "TEXT"),
        ("lease_expires_at", "TEXT"),
        ("source_job_id", "TEXT"),
    ):
        if name not in queue_columns:
            op.execute(f"ALTER TABLE enrichment_queue ADD COLUMN {name} {definition}")
    _create_metadata_images(False)
    op.execute(
        """
        INSERT INTO metadata_images_next(
            provider,entity_type,provider_id,locale,image_type,image_url,
            local_path,fetched_at,expires_at,blur_hash
        )
        SELECT provider,entity_type,provider_id,COALESCE(locale,''),image_type,image_url,
               local_path,fetched_at,expires_at,blur_hash
        FROM (
            SELECT metadata_images.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY provider,entity_type,provider_id,COALESCE(locale,''),image_type,image_url
                       ORDER BY fetched_at DESC,rowid DESC
                   ) AS duplicate_rank
            FROM metadata_images
        )
        WHERE duplicate_rank=1
        """
    )
    op.execute("DROP TABLE metadata_images")
    op.execute("ALTER TABLE metadata_images_next RENAME TO metadata_images")
    op.execute(
        "CREATE INDEX idx_metadata_images_url_path_ready "
        "ON metadata_images(image_url,local_path) WHERE blur_hash IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_metadata_images_type_url_fetched "
        "ON metadata_images(image_type,image_url,fetched_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_provider_ids_provider_id "
        "ON entity_provider_ids(provider,provider_id,entity_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_jobs_global_queue "
        "ON library_jobs(state,created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_search_grams_entity_locale "
        "ON catalog_search_grams(entity_id,locale)"
    )
    op.execute(
        "INSERT INTO schema_metadata(key,value) VALUES('generation','sqlalchemy-metadata-v2') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_catalog_search_grams_entity_locale")
    op.execute("DROP INDEX IF EXISTS idx_library_jobs_global_queue")
    op.execute("DROP INDEX IF EXISTS idx_entity_provider_ids_provider_id")
    _create_metadata_images(True)
    op.execute(
        """
        INSERT INTO metadata_images_next(
            provider,entity_type,provider_id,locale,image_type,image_url,
            local_path,fetched_at,expires_at,blur_hash
        )
        SELECT provider,entity_type,provider_id,NULLIF(locale,''),image_type,image_url,
               local_path,fetched_at,expires_at,blur_hash
        FROM metadata_images
        """
    )
    op.execute("DROP TABLE metadata_images")
    op.execute("ALTER TABLE metadata_images_next RENAME TO metadata_images")
