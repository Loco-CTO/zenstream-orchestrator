from alembic import op

revision = "0025_screen_extractor_artwork"
down_revision = "0024_durable_reconcile_targets"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS screen_extractor_assets (
            entity_id TEXT PRIMARY KEY NOT NULL,
            desired_media_file_id TEXT,
            desired_source_fingerprint TEXT NOT NULL,
            desired_output_key TEXT NOT NULL,
            extraction_version INTEGER NOT NULL DEFAULT 1,
            generation INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL,
            ready_media_file_id TEXT,
            ready_source_fingerprint TEXT,
            local_path TEXT,
            blur_hash TEXT,
            version TEXT,
            width INTEGER,
            height INTEGER,
            seek_seconds REAL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error_code TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CONSTRAINT ck_screen_extractor_state
                CHECK(state IN ('queued','generating','ready','retry','failed')),
            CONSTRAINT ck_screen_extractor_generation
                CHECK(generation > 0),
            CONSTRAINT ck_screen_extractor_attempt_count
                CHECK(attempt_count >= 0),
            CONSTRAINT ck_screen_extractor_dimensions
                CHECK((width IS NULL OR width > 0) AND (height IS NULL OR height > 0)),
            CONSTRAINT ck_screen_extractor_seek
                CHECK(seek_seconds IS NULL OR seek_seconds >= 0),
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(desired_media_file_id) REFERENCES media_files(id) ON DELETE SET NULL,
            FOREIGN KEY(ready_media_file_id) REFERENCES media_files(id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_screen_extractor_assets_work "
        "ON screen_extractor_assets(state,next_attempt_at,updated_at)"
    )
    op.execute(
        "UPDATE catalog_read_model_status "
        "SET state='building',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=1"
    )
    op.execute(
        "UPDATE job_definitions SET next_run_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP "
        "WHERE job_key='metadata_missing'"
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS screen_extractor_assets")
    op.execute(
        "UPDATE catalog_read_model_status "
        "SET state='building',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=1"
    )
