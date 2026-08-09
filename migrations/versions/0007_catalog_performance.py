from alembic import op


revision = "0007_catalog_performance"
down_revision = "0006_analysis_worker_limits"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_library_summary (
            library_id TEXT PRIMARY KEY NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            supports_last_added INTEGER NOT NULL DEFAULT 0,
            last_root_entity_id TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE,
            FOREIGN KEY(last_root_entity_id) REFERENCES library_entities(id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_root_search_grams (
            gram TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            locale TEXT NOT NULL,
            library_id TEXT NOT NULL,
            title_sort TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(gram, entity_id, locale),
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_root_search_grams_lookup "
        "ON catalog_root_search_grams(gram, locale, library_id, entity_id)"
    )
    op.execute(
        "INSERT OR IGNORE INTO catalog_root_search_grams(gram,entity_id,locale,library_id,title_sort) "
        "SELECT g.gram,g.entity_id,g.locale,g.library_id,p.title_sort "
        "FROM catalog_search_grams g "
        "JOIN catalog_item_projection p ON p.entity_id=g.entity_id AND p.locale=g.locale "
        "WHERE p.parent_id IS NULL AND p.entity_type IN ('movie','series','collection')"
    )
    op.execute("ALTER TABLE catalog_item_genres ADD COLUMN library_id TEXT")
    op.execute("ALTER TABLE catalog_item_genres ADD COLUMN entity_type TEXT")
    op.execute(
        "UPDATE catalog_item_genres SET "
        "library_id=(SELECT p.library_id FROM catalog_item_projection p "
        "WHERE p.entity_id=catalog_item_genres.entity_id AND p.locale=catalog_item_genres.locale),"
        "entity_type=(SELECT p.entity_type FROM catalog_item_projection p "
        "WHERE p.entity_id=catalog_item_genres.entity_id AND p.locale=catalog_item_genres.locale)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_item_genres_covering "
        "ON catalog_item_genres(locale, library_id, entity_type, genre_key, entity_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_artwork_selection (
            entity_id TEXT NOT NULL,
            locale TEXT NOT NULL,
            image_type TEXT NOT NULL,
            local_path TEXT NOT NULL,
            blur_hash TEXT,
            version TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(entity_id, locale, image_type),
            FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_artwork_selection_lookup "
        "ON catalog_artwork_selection(entity_id, locale, image_type, version)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_collection_member_projection (
            collection_entity_id TEXT NOT NULL,
            source_entity_id TEXT NOT NULL,
            source_library_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(collection_entity_id, source_entity_id),
            FOREIGN KEY(collection_entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(source_entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_collection_member_projection_page "
        "ON catalog_collection_member_projection(collection_entity_id, position, source_entity_id)"
    )
    op.execute(
        "INSERT OR IGNORE INTO catalog_collection_member_projection(collection_entity_id,source_entity_id,source_library_id,position,updated_at) "
        "SELECT m.collection_entity_id,m.source_entity_id,e.library_id,m.position,CURRENT_TIMESTAMP "
        "FROM collection_members m JOIN library_entities e ON e.id=m.source_entity_id"
    )
    op.execute(
        "INSERT OR IGNORE INTO catalog_library_summary(library_id,generation,supports_last_added,last_root_entity_id,updated_at) "
        "SELECT l.id,COALESCE((SELECT generation FROM catalog_read_model_status WHERE id=1),0),"
        "CASE WHEN EXISTS(SELECT 1 FROM catalog_entity_summary s WHERE s.library_id=l.id AND s.parent_id IS NOT NULL) THEN 1 ELSE 0 END,"
        "NULL,CURRENT_TIMESTAMP FROM libraries l"
    )
def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_catalog_collection_member_projection_page")
    op.execute("DROP TABLE IF EXISTS catalog_collection_member_projection")
    op.execute("DROP TABLE IF EXISTS catalog_artwork_selection")
    op.execute("DROP INDEX IF EXISTS idx_catalog_item_genres_covering")
    op.execute("ALTER TABLE catalog_item_genres DROP COLUMN entity_type")
    op.execute("ALTER TABLE catalog_item_genres DROP COLUMN library_id")
    op.execute("DROP INDEX IF EXISTS idx_catalog_root_search_grams_lookup")
    op.execute("DROP TABLE IF EXISTS catalog_root_search_grams")
    op.execute("DROP TABLE IF EXISTS catalog_library_summary")
