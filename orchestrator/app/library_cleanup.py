from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path

_CLEANUP_BATCH_SIZE = 300


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def _chunks(values, size: int = _CLEANUP_BATCH_SIZE):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _termination_requested(should_terminate: Callable[[], bool] | None) -> bool:
    return bool(should_terminate and should_terminate())


def _read_session(db):
    read_session = getattr(db, "read_session", None)
    return read_session() if callable(read_session) else nullcontext()


def _referenced_paths(
    db,
    tables: set[str],
    paths,
    reference_tables: tuple[str, ...],
    should_terminate: Callable[[], bool] | None = None,
) -> tuple[set[str], bool]:
    """Find referenced paths in bounded set-based reads.

    A cleanup pass can encounter tens of thousands of cache files. Keep each
    SQL statement bounded and reuse one reader session so the pass cannot
    exhaust the read pool with one checkout per path.
    """
    sources = tuple(table for table in reference_tables if table in tables)
    values = sorted({str(path) for path in paths if path})
    if not sources or not values:
        return set(), True

    referenced: set[str] = set()
    with _read_session(db):
        for batch in _chunks(values):
            if _termination_requested(should_terminate):
                return referenced, False
            placeholders = _placeholders(batch)
            query = " UNION ALL ".join(
                f"SELECT local_path FROM {table} WHERE local_path IN ({placeholders})"
                for table in sources
            )
            params = [value for _table in sources for value in batch]
            referenced.update(
                str(row[0]) for row in db.execute(query, params) if row[0]
            )
    return referenced, True


def _safe_cache_candidates(root: Path, paths) -> dict[str, Path]:
    """Return only paths contained by a cache root, keyed by DB spelling."""
    candidates: dict[str, Path] = {}
    resolved_root = root.resolve()
    for raw_path in paths:
        try:
            path = Path(str(raw_path))
            resolved = path.resolve()
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        candidates[str(path)] = resolved
    return candidates


def _remove_unreferenced_candidates(
    candidates: dict[str, Path],
    referenced: set[str],
    should_terminate: Callable[[], bool] | None = None,
    lock_factory=None,
) -> bool:
    for raw_path, resolved in candidates.items():
        if _termination_requested(should_terminate):
            return False
        if raw_path in referenced:
            continue
        lock = lock_factory(resolved) if lock_factory else nullcontext()
        try:
            with lock:
                if raw_path not in referenced:
                    resolved.unlink(missing_ok=True)
        except OSError:
            continue
    return True


def _metadata_file_lock(path: Path):
    from app.metadata_services import MetadataImageIngestService

    return MetadataImageIngestService._file_lock(path)


def _table_names(db) -> set[str]:
    """Read schema state before opening a transaction.

    DatabaseHandler.execute commits after every call, so this must never be
    called while DatabaseHandler.transaction() is active.
    """
    return {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


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


def _image_paths(
    db, tables: set[str], provider_keys: list[tuple[str, str, str]]
) -> set[str]:
    if "metadata_images" not in tables:
        return set()
    paths = {
        row[0]
        for row in db.execute(
            "SELECT DISTINCT local_path FROM metadata_images WHERE local_path IS NOT NULL"
        )
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


def _person_image_paths(db, tables: set[str]) -> set[str]:
    if "people" not in tables:
        return set()
    return {
        row[0]
        for row in db.execute(
            "SELECT local_path FROM people WHERE local_path IS NOT NULL"
        )
        if row[0]
    }


def _delete_entity_rows(cursor, tables: set[str], entity_ids: list[str]) -> None:
    for batch in _chunks(entity_ids):
        placeholders = _placeholders(batch)
        if "metadata_hydration_requests" in tables:
            cursor.execute(
                f"DELETE FROM metadata_hydration_requests WHERE entity_id IN ({placeholders})",
                batch,
            )
        if "collection_members" in tables:
            cursor.execute(
                f"DELETE FROM collection_members WHERE collection_entity_id IN ({placeholders}) OR source_entity_id IN ({placeholders})",
                batch + batch,
            )
        if "catalog_search" in tables:
            cursor.execute(
                f"DELETE FROM catalog_search WHERE entity_id IN ({placeholders})",
                batch,
            )
        if "entity_person_credits" in tables:
            cursor.execute(
                f"DELETE FROM entity_person_credits WHERE entity_id IN ({placeholders})",
                batch,
            )
        if "music_artist_credits" in tables:
            cursor.execute(
                f"DELETE FROM music_artist_credits WHERE track_id IN ({placeholders}) OR artist_id IN ({placeholders})",
                batch + batch,
            )
        if "media_files" in tables:
            cursor.execute(
                f"DELETE FROM media_files WHERE entity_id IN ({placeholders})", batch
            )
        if "screen_extractor_assets" in tables:
            cursor.execute(
                f"DELETE FROM screen_extractor_assets WHERE entity_id IN ({placeholders})",
                batch,
            )
        cursor.execute(
            f"DELETE FROM entity_provider_ids WHERE entity_id IN ({placeholders})",
            batch,
        )
        cursor.execute(
            f"DELETE FROM library_entities WHERE id IN ({placeholders})", batch
        )


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
            library_clause = (
                " AND EXISTS (SELECT 1 FROM libraries l WHERE l.id=e.library_id)"
            )
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
    if "catalog_search" in tables:
        cursor.execute(
            f"DELETE FROM catalog_search AS s WHERE NOT {valid_entity('s.entity_id')}"
        )
    if "entity_provider_ids" in tables:
        cursor.execute(
            f"DELETE FROM entity_provider_ids AS p WHERE NOT {valid_entity('p.entity_id')}"
        )
    if "music_artist_credits" in tables:
        cursor.execute(
            f"DELETE FROM music_artist_credits AS c WHERE NOT ({valid_entity('c.track_id')} AND {valid_entity('c.artist_id')})"
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
    if {"people", "entity_person_credits"} <= tables:
        cursor.execute(
            "DELETE FROM people AS p WHERE NOT EXISTS (SELECT 1 FROM entity_person_credits c WHERE c.person_id=p.id)"
        )


def _remove_cached_files(
    db,
    tables: set[str],
    paths: set[str],
    should_terminate: Callable[[], bool] | None = None,
) -> bool:
    if not paths or "metadata_images" not in tables or not db.db_file:
        return True
    cache_root = (Path(db.db_file).parent / "metadata-cache" / "images").resolve()
    candidates = _safe_cache_candidates(cache_root, paths)
    referenced, complete = _referenced_paths(
        db,
        tables,
        candidates,
        ("metadata_images", "catalog_artwork_selection"),
        should_terminate,
    )
    if not complete:
        return False
    return _remove_unreferenced_candidates(
        candidates,
        referenced,
        should_terminate,
        _metadata_file_lock,
    )


def _sweep_metadata_cache_files(
    db,
    tables: set[str],
    grace_seconds: int = 86400,
    should_terminate: Callable[[], bool] | None = None,
) -> bool:
    """Remove old crash leftovers and unregistered metadata image files."""
    if "metadata_images" not in tables or not db.db_file:
        return True
    root = (Path(db.db_file).parent / "metadata-cache" / "images").resolve()
    if not root.is_dir():
        return True

    cutoff = time.time() - grace_seconds
    candidates = {}
    for path in root.iterdir():
        if _termination_requested(should_terminate):
            return False
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        candidates[str(path)] = path.resolve()

    referenced, complete = _referenced_paths(
        db,
        tables,
        candidates,
        ("metadata_images", "catalog_artwork_selection"),
        should_terminate,
    )
    if not complete:
        return False
    return _remove_unreferenced_candidates(
        candidates,
        referenced,
        should_terminate,
        _metadata_file_lock,
    )


def _remove_person_cached_files(
    db,
    tables: set[str],
    paths: set[str],
    should_terminate: Callable[[], bool] | None = None,
) -> bool:
    if not paths or "people" not in tables or not db.db_file:
        return True
    cache_root = (Path(db.db_file).parent / "people-cache").resolve()
    candidates = _safe_cache_candidates(cache_root, paths)
    referenced, complete = _referenced_paths(
        db, tables, candidates, ("people",), should_terminate
    )
    if not complete:
        return False
    return _remove_unreferenced_candidates(
        candidates,
        referenced,
        should_terminate,
        _metadata_file_lock,
    )


def _trickplay_media_ids(db, tables: set[str], entity_ids: list[str]) -> set[str]:
    if "trickplay_assets" not in tables or not entity_ids:
        return set()
    values = set()
    for batch in _chunks(entity_ids):
        values.update(
            row[0]
            for row in db.execute(
                f"SELECT media_file_id FROM trickplay_assets WHERE entity_id IN ({_placeholders(batch)})",
                batch,
            )
        )
    return values


def _remove_trickplay_files(
    db,
    media_file_ids: set[str],
    should_terminate: Callable[[], bool] | None = None,
) -> bool:
    if not media_file_ids or not db.db_file:
        return True
    root = (Path(db.db_file).parent / "trickplay-cache").resolve()
    for media_file_id in media_file_ids:
        if _termination_requested(should_terminate):
            return False
        try:
            target = (root / media_file_id).resolve()
            target.relative_to(root)
            if target.is_dir():
                import shutil

                shutil.rmtree(target, ignore_errors=True)
        except (OSError, ValueError):
            continue
    return True


def _screen_extractor_paths(db, tables: set[str], entity_ids: list[str]) -> set[str]:
    if "screen_extractor_assets" not in tables or not entity_ids:
        return set()
    return {
        row[0]
        for batch in _chunks(entity_ids)
        for row in db.execute(
            f"SELECT local_path FROM screen_extractor_assets WHERE entity_id IN ({_placeholders(batch)}) AND local_path IS NOT NULL",
            batch,
        )
        if row[0]
    }


def _remove_screen_extractor_files(
    db,
    tables: set[str],
    paths: set[str],
    should_terminate: Callable[[], bool] | None = None,
) -> bool:
    if not paths or "screen_extractor_assets" not in tables or not db.db_file:
        return True
    root = (Path(db.db_file).parent / "screen-extractor-cache").resolve()
    candidates = _safe_cache_candidates(root, paths)
    referenced, complete = _referenced_paths(
        db,
        tables,
        candidates,
        ("screen_extractor_assets", "catalog_artwork_selection"),
        should_terminate,
    )
    if not complete:
        return False
    return _remove_unreferenced_candidates(candidates, referenced, should_terminate)


def _sweep_screen_extractor_cache(
    db,
    tables: set[str],
    grace_seconds: int = 86400,
    should_terminate: Callable[[], bool] | None = None,
) -> bool:
    if "screen_extractor_assets" not in tables or not db.db_file:
        return True
    root = (Path(db.db_file).parent / "screen-extractor-cache").resolve()
    if not root.is_dir():
        return True
    cutoff = time.time() - grace_seconds
    candidates = {}
    for path in root.rglob("*.webp"):
        if _termination_requested(should_terminate):
            return False
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
            if path.stat().st_mtime > cutoff:
                continue
        except (OSError, ValueError):
            continue
        candidates[str(path)] = resolved

    referenced, complete = _referenced_paths(
        db,
        tables,
        candidates,
        ("screen_extractor_assets", "catalog_artwork_selection"),
        should_terminate,
    )
    if not complete:
        return False
    return _remove_unreferenced_candidates(candidates, referenced, should_terminate)


def _cleanup(
    db,
    entity_ids: list[str],
    library_id: str | None = None,
    progress=None,
    should_terminate: Callable[[], bool] | None = None,
) -> bool:
    tables = _table_names(db)
    if "library_entities" not in tables or "entity_provider_ids" not in tables:
        raise RuntimeError("Library inventory schema is incomplete.")
    if _termination_requested(should_terminate):
        return False

    entity_ids = (
        _entity_closure(db, list(dict.fromkeys(entity_ids))) if entity_ids else []
    )
    provider_keys = _provider_keys(db, entity_ids)
    image_paths = _image_paths(db, tables, provider_keys)
    if _termination_requested(should_terminate):
        return False
    person_image_paths = _person_image_paths(db, tables)
    if _termination_requested(should_terminate):
        return False
    trickplay_media_ids = _trickplay_media_ids(db, tables, entity_ids)
    screen_paths = _screen_extractor_paths(db, tables, entity_ids)
    if _termination_requested(should_terminate):
        return False

    with db.transaction() as cursor:
        if entity_ids:
            _delete_entity_rows(cursor, tables, entity_ids)
        if library_id is not None:
            # The library relationship is authoritative. The ID list above
            # is batched for dependent cleanup, while this final predicate
            # guarantees no inventory row for the library can survive.
            cursor.execute(
                "DELETE FROM library_entities WHERE library_id=?", (library_id,)
            )
            if "library_sources" in tables:
                cursor.execute(
                    "DELETE FROM library_sources WHERE library_id=? OR source_library_id=?",
                    (library_id, library_id),
                )
            if "library_jobs" in tables:
                cursor.execute(
                    "DELETE FROM library_jobs WHERE library_id=?", (library_id,)
                )
            cursor.execute("DELETE FROM libraries WHERE id=?", (library_id,))
        _purge_orphan_inventory(cursor, tables)
        _purge_orphan_metadata(cursor, tables)

    if _termination_requested(should_terminate):
        return False
    if progress:
        progress("database", "Cleaning database relationships")

    if not _remove_cached_files(db, tables, image_paths, should_terminate):
        return False
    if progress:
        progress("artwork", "Cleaning cached artwork")
    if not _remove_person_cached_files(
        db, tables, person_image_paths, should_terminate
    ):
        return False
    if progress:
        progress("portraits", "Cleaning cached portraits")
    if not _remove_trickplay_files(db, trickplay_media_ids, should_terminate):
        return False
    if progress:
        progress("trickplay", "Cleaning trickplay cache")
    if not _remove_screen_extractor_files(db, tables, screen_paths, should_terminate):
        return False
    if progress:
        progress("screen_extractor", "Cleaning screen-extractor cache")
    if not _sweep_screen_extractor_cache(db, tables, should_terminate=should_terminate):
        return False
    if not _sweep_metadata_cache_files(db, tables, should_terminate=should_terminate):
        return False
    if progress:
        progress("cache_sweep", "Sweeping metadata caches")
    if _termination_requested(should_terminate):
        return False
    from app.images import LocalArtworkCache

    LocalArtworkCache(db).prune()
    if progress:
        progress("local_artwork", "Pruning local artwork")
    return True


def cleanup_entities(db, entity_ids: list[str]) -> None:
    """Delete entities and all dependent data no longer reachable from them."""
    if entity_ids:
        _cleanup(db, entity_ids)


def cleanup_library(db, library_id: str) -> bool:
    """Atomically remove a library and every artifact owned by it."""
    if not db.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)):
        return False
    entity_ids = [
        row[0]
        for row in db.execute(
            "SELECT id FROM library_entities WHERE library_id=?", (library_id,)
        )
    ]
    return _cleanup(db, entity_ids, library_id)


def cleanup_orphans(
    db,
    progress=None,
    should_terminate: Callable[[], bool] | None = None,
) -> bool:
    """Purge leftovers from libraries/entities deleted before cascading cleanup existed."""
    return _cleanup(db, [], progress=progress, should_terminate=should_terminate)
