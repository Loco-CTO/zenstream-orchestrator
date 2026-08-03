from alembic import op


revision = "0003_catalog_read_model"
down_revision = "0002_catalog_read_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_entity_summary (
            entity_id TEXT PRIMARY KEY NOT NULL,
            library_id TEXT NOT NULL,
            parent_id TEXT,
            entity_type TEXT NOT NULL,
            playable_leaf_count INTEGER NOT NULL DEFAULT 0,
            media_file_count INTEGER NOT NULL DEFAULT 0,
            media_added_ns INTEGER,
            media_last_added_ns INTEGER,
            added_sort_ns INTEGER NOT NULL,
            last_added_sort_ns INTEGER NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE,
            FOREIGN KEY(parent_id) REFERENCES library_entities(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_item_projection (
            entity_id TEXT NOT NULL,
            locale TEXT NOT NULL,
            library_id TEXT NOT NULL,
            parent_id TEXT,
            entity_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            title_sort TEXT NOT NULL DEFAULT '',
            rating_sort REAL NOT NULL DEFAULT 0,
            release_sort TEXT NOT NULL DEFAULT '',
            runtime_sort REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(entity_id, locale),
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_user_summary (
            user_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            played_leaf_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, entity_id),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_collection_summary (
            collection_entity_id TEXT NOT NULL,
            collection_library_id TEXT NOT NULL,
            source_library_id TEXT NOT NULL,
            playable_leaf_count INTEGER NOT NULL DEFAULT 0,
            media_file_count INTEGER NOT NULL DEFAULT 0,
            added_sort_ns INTEGER NOT NULL,
            last_added_sort_ns INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(collection_entity_id, source_library_id),
            FOREIGN KEY(collection_entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(collection_library_id) REFERENCES libraries(id) ON DELETE CASCADE,
            FOREIGN KEY(source_library_id) REFERENCES libraries(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_item_genres (
            entity_id TEXT NOT NULL,
            locale TEXT NOT NULL,
            genre_key TEXT NOT NULL,
            genre_name TEXT NOT NULL,
            PRIMARY KEY(entity_id, locale, genre_key),
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_search_grams (
            gram TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            locale TEXT NOT NULL,
            library_id TEXT NOT NULL,
            parent_id TEXT,
            PRIMARY KEY(gram, entity_id, locale),
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_read_model_status (
            id INTEGER PRIMARY KEY CHECK(id=1),
            state TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            error TEXT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_catalog_entity_summary_parent_added
        ON catalog_entity_summary(library_id, parent_id, added_sort_ns DESC, entity_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_catalog_entity_summary_parent_last
        ON catalog_entity_summary(library_id, parent_id, last_added_sort_ns DESC, entity_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_catalog_entity_summary_type_last
        ON catalog_entity_summary(library_id, entity_type, last_added_sort_ns DESC, entity_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_catalog_entity_summary_parent
        ON catalog_entity_summary(parent_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_catalog_item_projection_title
        ON catalog_item_projection(library_id, parent_id, locale, title_sort, entity_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_catalog_item_projection_rating
        ON catalog_item_projection(library_id, parent_id, locale, rating_sort DESC, title_sort, entity_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_catalog_item_projection_release
        ON catalog_item_projection(library_id, parent_id, locale, release_sort DESC, title_sort, entity_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_catalog_item_projection_runtime
        ON catalog_item_projection(library_id, parent_id, locale, runtime_sort DESC, title_sort, entity_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_catalog_collection_summary_source
        ON catalog_collection_summary(source_library_id, collection_entity_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_catalog_collection_summary_library
        ON catalog_collection_summary(collection_library_id, source_library_id, collection_entity_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_catalog_item_genres_lookup
        ON catalog_item_genres(locale, genre_key, entity_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_catalog_search_grams_lookup
        ON catalog_search_grams(gram, locale, library_id, parent_id, entity_id)
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collection_members_source "
        "ON collection_members(source_entity_id, collection_entity_id)"
    )
    op.execute(
        "INSERT INTO catalog_read_model_status(id,state,generation,updated_at,error) "
        "VALUES(1,'building',0,CURRENT_TIMESTAMP,NULL) "
        "ON CONFLICT(id) DO UPDATE SET state='building',error=NULL,updated_at=excluded.updated_at"
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS catalog_search_grams")
    op.execute("DROP TABLE IF EXISTS catalog_item_genres")
    op.execute("DROP TABLE IF EXISTS catalog_collection_summary")
    op.execute("DROP TABLE IF EXISTS catalog_user_summary")
    op.execute("DROP TABLE IF EXISTS catalog_item_projection")
    op.execute("DROP TABLE IF EXISTS catalog_entity_summary")
    op.execute("DROP TABLE IF EXISTS catalog_read_model_status")
