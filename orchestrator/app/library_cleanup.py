"""Atomic relationship-aware cleanup for native library inventory and metadata."""

from __future__ import annotations

from pathlib import Path


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def _chunks(values, size: int = 300):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _table_names(db) -> set[str]:
    """Read schema state before opening a transaction.

    DatabaseHandler.execute commits after every call, so this must never be
    called while DatabaseHandler.transaction() is active.
    """
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _entity_closure(db, entity_ids: list[str]) -> list[str]:
    """Return roots and descendants before deletion or FK cascades run."""
    found = set(entity_ids)
    pending = list(found)
    while pending:
        children = []
        for batch in _chunks(pending):
            children.extend(
                db.execute(
                    f"SELECT id FROM library_entities WHERE parent_id IN ({_placeholders(batch)})",
                    batch,
                )
            )
        pending = [row[0] for row in children if row[0] not in found]
        found.update(pending)
    return list(found)


def _provider_keys(db, entity_ids: list[str]) -> list[tuple[str, str, str]]:
    keys = set()
    for batch in _chunks(entity_ids):
        keys.update(
            db.execute(
                f"SELECT DISTINCT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id IN ({_placeholders(batch)})",
                batch,
            )
        )
    return list(keys)


def _image_paths(db, tables: set[str], provider_keys: list[tuple[str, str, str]]) -> set[str]:
    if "metadata_images" not in tables:
        return set()
    paths = {
        row[0]
        for row in db.execute("SELECT DISTINCT local_path FROM metadata_images WHERE local_path IS NOT NULL")
        if row[0]
    }
    if not provider_keys:
        return paths
    paths.update(
        row[0]
        for batch in _chunks(provider_keys)
        for row in db.execute(
            "SELECT DISTINCT local_path FROM metadata_images WHERE (provider,entity_type,provider_id) IN "
            f"({','.join('(?,?,?)' for _ in batch)}) AND local_path IS NOT NULL",
            [value for key in batch for value in key],
        )
        if row[0]
    )
    return paths


def _delete_entity_rows(cursor, tables: set[str], entity_ids: list[str]) -> None:
    for batch in _chunks(entity_ids):
        placeholders = _placeholders(batch)
        if "metadata_hydration_requests" in tables:
            cursor.execute(f"DELETE FROM metadata_hydration_requests WHERE entity_id IN ({placeholders})", batch)
        if "collection_members" in tables:
            cursor.execute(
                f"DELETE FROM collection_members WHERE collection_entity_id IN ({placeholders}) OR source_entity_id IN ({placeholders})",
                batch + batch,
            )
        if "media_files" in tables:
            cursor.execute(f"DELETE FROM media_files WHERE entity_id IN ({placeholders})", batch)
        cursor.execute(f"DELETE FROM entity_provider_ids WHERE entity_id IN ({placeholders})", batch)
        cursor.execute(f"DELETE FROM library_entities WHERE id IN ({placeholders})", batch)


def _purge_orphan_metadata(cursor, tables: set[str]) -> None:
    """Remove every cache key no longer represented by an indexed entity."""
    reference = """NOT EXISTS (
        SELECT 1 FROM entity_provider_ids p
        WHERE p.provider = m.provider
          AND p.identifier_type = m.entity_type
          AND p.provider_id = m.provider_id
    )"""
    if "metadata_cache" in tables:
        cursor.execute(f"DELETE FROM metadata_cache AS m WHERE {reference}")
    if "metadata_images" in tables:
        cursor.execute(f"DELETE FROM metadata_images AS m WHERE {reference}")


def _purge_orphan_inventory(cursor, tables: set[str]) -> None:
    """Remove inventory rows whose owning library/entity no longer exists."""
    def valid_entity(column: str) -> str:
        library_clause = ""
        if "libraries" in tables:
            library_clause = " AND EXISTS (SELECT 1 FROM libraries l WHERE l.id=e.library_id)"
        return f"EXISTS (SELECT 1 FROM library_entities e WHERE e.id={column}{library_clause})"

    if "collection_members" in tables:
        cursor.execute(
            f"DELETE FROM collection_members AS m WHERE NOT ({valid_entity('m.collection_entity_id')} AND {valid_entity('m.source_entity_id')})"
        )
    if "metadata_hydration_requests" in tables:
        cursor.execute(
            f"DELETE FROM metadata_hydration_requests AS h WHERE NOT {valid_entity('h.entity_id')}"
        )
    if "media_files" in tables:
        cursor.execute(
            f"DELETE FROM media_files AS f WHERE NOT {valid_entity('f.entity_id')}"
        )
    if "entity_provider_ids" in tables:
        cursor.execute(
            f"DELETE FROM entity_provider_ids AS p WHERE NOT {valid_entity('p.entity_id')}"
        )
    if "library_entities" in tables and "libraries" in tables:
        cursor.execute(
            """DELETE FROM library_entities AS e
            WHERE NOT EXISTS (SELECT 1 FROM libraries l WHERE l.id=e.library_id)"""
        )
    if "library_jobs" in tables and "libraries" in tables:
        cursor.execute(
            """DELETE FROM library_jobs AS j
            WHERE NOT EXISTS (SELECT 1 FROM libraries l WHERE l.id=j.library_id)"""
        )
    if "library_sources" in tables and "libraries" in tables:
        cursor.execute(
            """DELETE FROM library_sources AS s
            WHERE NOT EXISTS (SELECT 1 FROM libraries l WHERE l.id=s.library_id)
               OR NOT EXISTS (SELECT 1 FROM libraries l WHERE l.id=s.source_library_id)"""
        )


def _remove_cached_files(db, tables: set[str], paths: set[str]) -> None:
    if not paths or "metadata_images" not in tables or not db.db_file:
        return
    cache_root = (Path(db.db_file).parent / "metadata-cache" / "images").resolve()
    for raw_path in paths:
        try:
            path = Path(raw_path)
            resolved = path.resolve()
            resolved.relative_to(cache_root)
            if not db.execute("SELECT 1 FROM metadata_images WHERE local_path=? LIMIT 1", (str(path),)):
                resolved.unlink(missing_ok=True)
        except (OSError, ValueError):
            continue


def _cleanup(db, entity_ids: list[str], library_id: str | None = None) -> bool:
    tables = _table_names(db)
    if "library_entities" not in tables or "entity_provider_ids" not in tables:
        raise RuntimeError("Library inventory schema is incomplete.")

    entity_ids = _entity_closure(db, list(dict.fromkeys(entity_ids))) if entity_ids else []
    provider_keys = _provider_keys(db, entity_ids)
    image_paths = _image_paths(db, tables, provider_keys)

    with db.transaction() as cursor:
        if entity_ids:
            _delete_entity_rows(cursor, tables, entity_ids)
        if library_id is not None:
            # The library relationship is authoritative. The ID list above
            # is batched for dependent cleanup, while this final predicate
            # guarantees no inventory row for the library can survive.
            cursor.execute("DELETE FROM library_entities WHERE library_id=?", (library_id,))
            if "library_sources" in tables:
                cursor.execute("DELETE FROM library_sources WHERE library_id=? OR source_library_id=?", (library_id, library_id))
            if "library_jobs" in tables:
                cursor.execute("DELETE FROM library_jobs WHERE library_id=?", (library_id,))
            cursor.execute("DELETE FROM libraries WHERE id=?", (library_id,))
        _purge_orphan_inventory(cursor, tables)
        _purge_orphan_metadata(cursor, tables)

    _remove_cached_files(db, tables, image_paths)
    return True


def cleanup_entities(db, entity_ids: list[str]) -> None:
    """Delete entities and all dependent data no longer reachable from them."""
    if entity_ids:
        _cleanup(db, entity_ids)


def cleanup_library(db, library_id: str) -> bool:
    """Atomically remove a library and every artifact owned by it."""
    if not db.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)):
        return False
    entity_ids = [row[0] for row in db.execute("SELECT id FROM library_entities WHERE library_id=?", (library_id,))]
    return _cleanup(db, entity_ids, library_id)


def cleanup_orphans(db) -> None:
    """Purge leftovers from libraries/entities deleted before cascading cleanup existed."""
    _cleanup(db, [])
