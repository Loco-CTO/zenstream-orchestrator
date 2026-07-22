"""Relationship-aware cleanup for native library inventory and metadata."""

from __future__ import annotations

from pathlib import Path


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def _entity_closure(db, entity_ids: list[str]) -> list[str]:
    """Return roots and all descendants before the foreign-key cascade runs."""
    found = set(entity_ids)
    pending = list(found)
    while pending:
        children = db.execute(
            f"SELECT id FROM library_entities WHERE parent_id IN ({_placeholders(pending)})",
            pending,
        )
        pending = [row[0] for row in children if row[0] not in found]
        found.update(pending)
    return list(found)


def _table_exists(db, name: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def cleanup_entities(db, entity_ids: list[str]) -> None:
    """Delete entities and all cache rows/files no longer reachable from them.

    Provider metadata is shared between libraries, so it is retained while any
    remaining entity still references the same provider/entity/provider-id key.
    """
    roots = list(dict.fromkeys(entity_ids))
    if not roots:
        return
    entity_ids = _entity_closure(db, roots)
    placeholders = _placeholders(entity_ids)
    provider_keys = db.execute(
        f"SELECT DISTINCT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id IN ({placeholders})",
        entity_ids,
    )
    image_paths = db.execute(
        f"SELECT DISTINCT local_path FROM metadata_images WHERE (provider,entity_type,provider_id) IN "
        f"({','.join('(?,?,?)' for _ in provider_keys)}) AND local_path IS NOT NULL",
        [value for key in provider_keys for value in key],
    ) if provider_keys and _table_exists(db, "metadata_images") else []

    with db.transaction() as cursor:
        if _table_exists(db, "metadata_hydration_requests"):
            cursor.execute(f"DELETE FROM metadata_hydration_requests WHERE entity_id IN ({placeholders})", entity_ids)
        if _table_exists(db, "collection_members"):
            cursor.execute(
                f"DELETE FROM collection_members WHERE collection_entity_id IN ({placeholders}) OR source_entity_id IN ({placeholders})",
                entity_ids + entity_ids,
            )
        cursor.execute(f"DELETE FROM media_files WHERE entity_id IN ({placeholders})", entity_ids)
        cursor.execute(f"DELETE FROM entity_provider_ids WHERE entity_id IN ({placeholders})", entity_ids)
        cursor.execute(f"DELETE FROM library_entities WHERE id IN ({placeholders})", entity_ids)

        for provider, entity_type, provider_id in provider_keys:
            if cursor.execute(
                "SELECT 1 FROM entity_provider_ids WHERE provider=? AND identifier_type=? AND provider_id=? LIMIT 1",
                (provider, entity_type, provider_id),
            ).fetchone():
                continue
            if _table_exists(db, "metadata_cache"):
                cursor.execute(
                    "DELETE FROM metadata_cache WHERE provider=? AND entity_type=? AND provider_id=?",
                    (provider, entity_type, provider_id),
                )
            if _table_exists(db, "metadata_images"):
                cursor.execute(
                    "DELETE FROM metadata_images WHERE provider=? AND entity_type=? AND provider_id=?",
                    (provider, entity_type, provider_id),
                )

    cache_root = (Path(db.db_file).parent / "metadata-cache" / "images").resolve() if db.db_file else None
    if cache_root:
        for (raw_path,) in image_paths:
            if not raw_path:
                continue
            path = Path(raw_path)
            try:
                resolved = path.resolve()
                resolved.relative_to(cache_root)
                if not _table_exists(db, "metadata_images") or not db.execute("SELECT 1 FROM metadata_images WHERE local_path=? LIMIT 1", (str(path),)):
                    resolved.unlink(missing_ok=True)
            except (OSError, ValueError):
                continue


def cleanup_library(db, library_id: str) -> bool:
    """Remove a library and every entity/cache artifact owned by it."""
    rows = db.execute("SELECT id FROM library_entities WHERE library_id=?", (library_id,))
    if not db.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)):
        return False
    cleanup_entities(db, [row[0] for row in rows])
    with db.transaction() as cursor:
        cursor.execute("DELETE FROM library_sources WHERE library_id=? OR source_library_id=?", (library_id, library_id))
        cursor.execute("DELETE FROM library_jobs WHERE library_id=?", (library_id,))
        cursor.execute("DELETE FROM libraries WHERE id=?", (library_id,))
    return True
