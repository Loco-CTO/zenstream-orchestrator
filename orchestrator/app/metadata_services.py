from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlparse

from app.images import LocalArtworkCache, blurhash_for_image, encode_webp_bytes
from app.language_registry import normalize_language
from app.logging_config import get_logger
from app.metadata_domain import (
    ARTWORK_CATEGORIES,
    ARTWORK_CATEGORY_SET,
    fallback_tiers,
    language_family,
    locale_variants,
    nonempty,
    rank_artwork_candidates,
    usable_text,
)
from app.models.metadata import (
    IMAGE_LANGUAGE_SCHEMA,
    MetadataCache,
    MetadataLanguageSettings,
    iso_now,
)
from app.search_scoring import normalize_search_text, search_grams
from app.worker_config import configured_worker_limit

logger = get_logger("metadata")


def _ready_file(path: Path | str | None) -> bool:
    try:
        value = Path(path) if path is not None else None
        return bool(value and value.is_file() and value.stat().st_size > 0)
    except OSError:
        return False


CATALOG_ITEM_PROJECTION_SCHEMA = 2

LOCAL_ARTWORK_NAMES = {
    "Primary": {"poster", "folder", "cover", "primary", "tvshow", "movie", "season"},
    "Backdrop": {"backdrop", "fanart", "background"},
    "Logo": {"logo", "clearlogo", "clear-logo"},
    "Banner": {"banner"},
}

TEXT_FIELDS = {
    "title",
    "originalTitle",
    "overview",
    "description",
    "status",
    "tags",
    "genres",
    "studios",
    "networks",
    "productionCompanies",
    "people",
}

FACT_FIELDS = {
    "date",
    "releaseDate",
    "firstAired",
    "lastAired",
    "airTime",
    "runtimeMinutes",
    "seasonNumber",
    "episodeNumber",
    "originalCountry",
    "year",
    "originalLanguage",
    "communityRating",
    "criticRating",
    "provider",
    "providerId",
    "ids",
    "children",
    "trailers",
}

PROVIDER_PRIORITIES = {
    "series": ["tvdb", "tmdb"],
    "season": ["tvdb", "tmdb"],
    "episode": ["tvdb", "tmdb"],
    "movie": ["tmdb", "tvdb"],
    "collection": ["tvdb"],
    "artist": ["musicbrainz"],
    "release": ["musicbrainz"],
    "track": ["musicbrainz"],
}

_fetch_activity_lock = threading.Lock()
_active_fetches = 0
_metadata_fetch_slots = threading.BoundedSemaphore(
    configured_worker_limit("METADATA_FETCH_WORKERS", 64, default=12)
)


def active_metadata_fetches() -> int:
    with _fetch_activity_lock:
        return _active_fetches


@contextmanager
def metadata_fetch_activity():
    global _active_fetches
    with _fetch_activity_lock:
        _active_fetches += 1
    try:
        yield
    finally:
        with _fetch_activity_lock:
            _active_fetches -= 1


def metadata_task_results(tasks, work, should_terminate=None, max_workers=None):
    """Run metadata I/O concurrently without creating an unbounded future queue."""
    should_terminate = should_terminate or (lambda: False)
    workers = (
        configured_worker_limit("METADATA_FETCH_WORKERS", 64, default=12)
        if max_workers is None
        else max(1, min(64, max_workers))
    )
    iterator = iter(tasks)

    def admitted_work(task):
        with _metadata_fetch_slots:
            return work(task)

    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="zenstream-metadata-fetch"
    ) as executor:
        while True:
            if should_terminate():
                return
            batch = []
            for _ in range(workers * 4):
                try:
                    task = next(iterator)
                except StopIteration:
                    break
                batch.append((task, executor.submit(admitted_work, task)))
            if not batch:
                return
            by_future = {future: task for task, future in batch}
            for future in as_completed(by_future):
                if should_terminate():
                    for pending in by_future:
                        pending.cancel()
                    return
                task = by_future[future]
                try:
                    yield task, future.result(), None
                except Exception as error:
                    yield task, None, error


class MetadataAssetExecutor:
    STATE_RETENTION_SECONDS = 15 * 60
    MAX_STATE_ENTRIES = 4096

    def __init__(self, max_workers: int | None = None):
        self.max_workers = (
            configured_worker_limit("METADATA_ASSET_WORKERS", 64, default=12)
            if max_workers is None
            else max(1, min(64, max_workers))
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="zenstream-metadata-assets",
        )
        self._lock = threading.RLock()
        self._pending: dict[tuple, Future] = {}
        self._states: dict[tuple, str] = {}
        self._state_times: dict[tuple, float] = {}

    def _prune_states_locked(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        cutoff = current - self.STATE_RETENTION_SECONDS
        stale = [
            key
            for key, updated in self._state_times.items()
            if updated < cutoff and key not in self._pending
        ]
        for key in stale:
            self._state_times.pop(key, None)
            self._states.pop(key, None)
        if len(self._states) > self.MAX_STATE_ENTRIES:
            candidates = sorted(
                (
                    updated,
                    key,
                )
                for key, updated in self._state_times.items()
                if key not in self._pending
            )
            for _updated, key in candidates[
                : max(0, len(self._states) - self.MAX_STATE_ENTRIES)
            ]:
                self._state_times.pop(key, None)
                self._states.pop(key, None)

    def submit_future(self, key: tuple, work) -> Future:
        with self._lock:
            self._prune_states_locked()
            current = self._pending.get(key)
            if current is not None and not current.done():
                return current
            self._states[key] = "pending"
            self._state_times[key] = time.monotonic()

            future = self._executor.submit(work)
            self._pending[key] = future

            def finished(done: Future) -> None:
                state = "complete"
                try:
                    done.result()
                except Exception as error:
                    state = "failed"
                    logger.warning(
                        "metadata asset work failed key=%s error=%s", key, error
                    )
                with self._lock:
                    self._states[key] = state
                    self._state_times[key] = time.monotonic()
                    self._pending.pop(key, None)
                    self._prune_states_locked()

            future.add_done_callback(finished)
            return future

    def submit(self, key: tuple, work) -> str:
        self.submit_future(key, work)
        return "pending"

    def submit_wait(self, key: tuple, work):
        return self.submit_future(key, work).result()

    def drain(self, timeout: float | None = None) -> None:
        with self._lock:
            futures = list(self._pending.values())
        if futures:
            wait(futures, timeout=timeout)

    def shutdown(self, wait_timeout: float = 5) -> None:
        self.drain(wait_timeout)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def state(self, key: tuple) -> str | None:
        with self._lock:
            self._prune_states_locked()
            return self._states.get(key)

    def prune(self) -> None:
        with self._lock:
            self._prune_states_locked()


asset_executor = MetadataAssetExecutor()


def _canonical_metadata_language(value: object) -> str | None:
    if not str(value or "").strip():
        return None
    try:
        return normalize_language(value, allow_unsupported=True)
    except ValueError:
        return None


def _usable_projection_value(value) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _asset_version(local_path: object, fallback: object) -> str:
    """Return a content-derived version for immutable authenticated assets."""
    digest = hashlib.sha256()
    try:
        with open(str(local_path), "rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()[:12]
    except (OSError, TypeError, ValueError):
        return hashlib.sha256(str(fallback or "").encode("utf-8")).hexdigest()[:12]


class MetadataSearchProjection:
    """Project cached titles into the transactional catalog search index."""

    def __init__(self, db):
        self.db = db

    def _ready_artwork(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        images: list[dict],
        locale: str,
        image_type: str,
        original: str | None,
        columns: set[str],
    ) -> tuple[dict, tuple] | None:
        """Return the first provider-ordered candidate with a ready file."""
        prefer_no_language_for_backdrop = (
            MetadataLanguageSettings().prefer_no_language_for_backdrop()
        )
        for candidate in rank_artwork_candidates(
            images,
            locale,
            image_type,
            original,
            [provider],
            include_english=any(
                language_family(value) == "en"
                for value in MetadataLanguageSettings().get()
            ),
            prefer_no_language_for_backdrop=prefer_no_language_for_backdrop,
        ):
            path_column = "local_path" in columns
            selected = self.db.execute(
                "SELECT "
                + ("local_path" if path_column else "NULL")
                + (",blur_hash" if "blur_hash" in columns else ",NULL")
                + (",fetched_at" if "fetched_at" in columns else ",NULL")
                + " FROM metadata_images WHERE provider=? AND entity_type=? "
                "AND provider_id=? AND image_type=? AND image_url=? "
                + ("AND local_path IS NOT NULL " if path_column else "")
                + "LIMIT 1",
                (
                    provider,
                    entity_type,
                    provider_id,
                    image_type,
                    candidate.get("url"),
                ),
            )
            if not selected:
                continue
            row = selected[0]
            if path_column and not _ready_file(row[0]):
                continue
            return candidate, row
        return None

    def _global_ready_artwork(
        self,
        entity_id: str,
        entity_type: str,
        locale: str,
        payload: dict,
        provider_ids: list[dict],
        current_provider: str | None = None,
    ) -> dict[str, tuple[dict, tuple, str]]:
        """Resolve ready artwork across all provider identities in one pass."""
        reader = MetadataReadService(self.db)
        has_cache = bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_cache'"
            )
        )
        raw = reader.resolve_raw(entity_type, provider_ids, locale) if has_cache else {}
        images = list(raw.get("images") or [])
        current_images = payload.get("images") if isinstance(payload, dict) else None
        if isinstance(current_images, list):
            images.extend(
                (
                    {
                        **image,
                        "provider": image.get("provider") or current_provider,
                    }
                    if isinstance(image, dict)
                    else image
                )
                for image in current_images
            )
        original = raw.get("originalLanguage") or payload.get("originalLanguage")
        result: dict[str, tuple[dict, tuple, str]] = {}
        has_selection = bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_artwork_selection'"
            )
        )
        existing = (
            self.db.execute(
                "SELECT s.image_type,s.provider,s.local_path,s.blur_hash,s.version "
                "FROM catalog_artwork_selection s WHERE s.entity_id=? AND s.locale=?",
                (entity_id, locale),
            )
            if has_selection
            else []
        )
        for image_type, selected_provider, local_path, blur_hash, version in existing:
            if image_type not in ARTWORK_CATEGORIES or not _ready_file(local_path):
                continue
            provider_id = next(
                (
                    str(identity.get("id"))
                    for identity in provider_ids
                    if identity.get("provider") == selected_provider
                ),
                None,
            )
            if not provider_id:
                continue
            selected = self.db.execute(
                "SELECT image_url FROM metadata_images WHERE provider=? AND "
                "entity_type=? AND provider_id=? AND image_type=? AND local_path=? "
                "ORDER BY rowid DESC LIMIT 1",
                (selected_provider, entity_type, provider_id, image_type, local_path),
            )
            if selected:
                result[image_type] = (
                    {
                        "type": image_type,
                        "url": selected[0][0],
                        "language": locale,
                        "provider": selected_provider,
                    },
                    (local_path, blur_hash, None),
                    selected_provider,
                )
        for image_type in ARTWORK_CATEGORIES:
            if (
                image_type in result
                and not raw.get("images")
                and current_provider != result[image_type][2]
            ):
                continue
            choice = reader.ready_artwork(
                entity_type,
                provider_ids,
                images,
                locale,
                image_type,
                original,
                reader.providers(entity_type),
                entity_id=entity_id,
            )
            if not choice:
                continue
            provider = str(choice.get("provider") or "")
            if provider == "screen_extractor":
                generated_path = choice.get("localPath")
                if generated_path and _ready_file(generated_path):
                    result[image_type] = (
                        choice,
                        (generated_path, choice.get("blurHash"), None),
                        provider,
                    )
                continue
            provider_id = next(
                (
                    str(identity.get("id"))
                    for identity in provider_ids
                    if identity.get("provider") == provider
                ),
                None,
            )
            if not provider_id:
                continue
            rows = self.db.execute(
                "SELECT local_path,blur_hash,fetched_at FROM metadata_images "
                "WHERE provider=? AND entity_type=? AND provider_id=? "
                "AND image_type=? AND image_url=? AND local_path IS NOT NULL "
                "ORDER BY rowid DESC LIMIT 1",
                (provider, entity_type, provider_id, image_type, choice.get("url")),
            )
            if rows and _ready_file(rows[0][0]):
                result[image_type] = (choice, rows[0], provider)
        return result

    def reproject_entity_artwork(
        self, entity_id: str, locales: Iterable[str] | None = None
    ) -> int:
        """Rebuild cached artwork selections for one entity.

        Scanner and metadata-repair paths can create a Screen Extractor file
        after the catalog projection has already been written.  The normal
        provider projection is not invoked for that language-neutral asset,
        so keep this small, idempotent repair at the entity boundary.
        """
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "catalog_artwork_selection" not in tables:
            return 0
        entity_rows = self.db.execute(
            "SELECT library_id,parent_id,entity_type FROM library_entities WHERE id=?",
            (entity_id,),
        )
        if not entity_rows:
            return 0
        library_id, parent_id, entity_type = entity_rows[0]
        if entity_type not in {"movie", "episode"}:
            return 0
        configured = list(locales or MetadataLanguageSettings().get()) or ["en"]
        identities = [
            {"provider": row[0], "id": str(row[1])}
            for row in self.db.execute(
                "SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=?",
                (entity_id,),
            )
        ]
        reader = MetadataReadService(self.db)
        local_cache = LocalArtworkCache(self.db)
        projection_exists = "catalog_item_projection" in tables
        selection_columns = {
            row[1]
            for row in self.db.execute("PRAGMA table_info(catalog_artwork_selection)")
        }
        if not {"provider", "local_path", "blur_hash", "version"}.issubset(
            selection_columns
        ):
            return 0

        def local_choice(image_type: str) -> dict | None:
            if "media_files" not in tables:
                return None
            columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(media_files)")
            }
            if "quick_fingerprint" not in columns:
                return None
            blur_field = ",image_blur_hash" if "image_blur_hash" in columns else ",NULL"
            for relative_path, fingerprint, blur_hash in self.db.execute(
                "SELECT relative_path,quick_fingerprint"
                + blur_field
                + " FROM media_files WHERE entity_id=? AND role='image' "
                "ORDER BY relative_path COLLATE NOCASE",
                (entity_id,),
            ):
                stem = Path(relative_path or "").stem.casefold()
                if (
                    stem not in LOCAL_ARTWORK_NAMES.get(image_type, set())
                    or not fingerprint
                ):
                    continue
                path = local_cache.path(str(fingerprint))
                if path and _ready_file(path):
                    return {
                        "provider": "local",
                        "localPath": str(path),
                        "version": str(fingerprint)[:12],
                        "blurHash": blur_hash,
                        "language": None,
                    }
            return None

        changed = 0
        for locale in configured:
            payload_rows = (
                self.db.execute(
                    "SELECT payload FROM catalog_item_projection WHERE entity_id=? AND locale=?",
                    (entity_id, locale),
                )
                if projection_exists
                else []
            )
            try:
                payload = json.loads(payload_rows[0][0]) if payload_rows else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            images = payload.get("images")
            images = dict(images) if isinstance(images, dict) else {}
            owners = payload.get("_catalogArtworkProviders")
            owners = dict(owners) if isinstance(owners, dict) else {}
            fallbacks = payload.get("_catalogArtworkFallbacks")
            fallbacks = dict(fallbacks) if isinstance(fallbacks, dict) else {}
            raw = reader.resolve_raw(entity_type, identities, locale)
            existing_rows = self.db.execute(
                "SELECT image_type,provider,local_path,blur_hash,version "
                "FROM catalog_artwork_selection WHERE entity_id=? AND locale=?",
                (entity_id, locale),
            )
            existing = {row[0]: row[1:] for row in existing_rows}
            selection_rows = []
            for image_type in ARTWORK_CATEGORIES:
                choice = local_choice(image_type)
                if choice is None:
                    choice = reader.ready_artwork(
                        entity_type,
                        identities,
                        raw.get("images", []),
                        locale,
                        image_type,
                        raw.get("originalLanguage"),
                        reader.providers(entity_type),
                        entity_id=entity_id,
                    )
                selected = None
                if choice:
                    provider = str(choice.get("provider") or "")
                    path = choice.get("localPath")
                    if provider not in ("local", "screen_extractor"):
                        provider_id = next(
                            (
                                identity["id"]
                                for identity in identities
                                if identity.get("provider") == provider
                            ),
                            None,
                        )
                        if provider_id and choice.get("url"):
                            rows = self.db.execute(
                                "SELECT local_path,blur_hash FROM metadata_images "
                                "WHERE provider=? AND entity_type=? AND provider_id=? "
                                "AND image_type=? AND image_url=? AND local_path IS NOT NULL "
                                "ORDER BY rowid DESC LIMIT 1",
                                (
                                    provider,
                                    entity_type,
                                    provider_id,
                                    image_type,
                                    choice["url"],
                                ),
                            )
                            if rows:
                                path, blur_hash = rows[0]
                                selected = (
                                    provider,
                                    path,
                                    blur_hash,
                                    _asset_version(path, choice["url"]),
                                )
                    elif path:
                        selected = (
                            provider,
                            path,
                            choice.get("blurHash"),
                            choice.get("version") or _asset_version(path, path),
                        )
                    if selected and not _ready_file(selected[1]):
                        selected = None
                if selected is None:
                    prior = existing.get(image_type)
                    if (
                        prior
                        and prior[0] != "screen_extractor"
                        and _ready_file(prior[1])
                    ):
                        selected = (
                            prior[0],
                            prior[1],
                            prior[2],
                            prior[3] or _asset_version(prior[1], prior[1]),
                        )
                if selected is None:
                    if owners.get(image_type) in {"screen_extractor", "local"}:
                        images.pop(image_type, None)
                        owners.pop(image_type, None)
                    continue
                provider, path, blur_hash, version = selected
                if (
                    image_type != "Logo"
                    and images.get(image_type)
                    and owners.get(image_type)
                    not in {provider, "local", "screen_extractor"}
                ):
                    fallbacks.setdefault(
                        image_type,
                        {
                            "image": images[image_type],
                            "provider": owners.get(image_type),
                        },
                    )
                projected = {
                    "url": f"/api/catalog/items/{entity_id}/images/{image_type}?language={locale}&v={version}",
                    "language": choice.get("language")
                    if choice and provider not in {"local", "screen_extractor"}
                    else None,
                    "width": (choice or {}).get("width") or 0,
                    "height": (choice or {}).get("height") or 0,
                }
                if image_type != "Logo" and blur_hash:
                    projected["blurHash"] = blur_hash
                images[image_type] = projected
                owners[image_type] = provider
                selection_rows.append(
                    (entity_id, locale, image_type, provider, path, blur_hash, version)
                )
            if projection_exists:
                payload["images"] = images
                payload["_catalogArtworkProviders"] = owners
                payload["_catalogArtworkFallbacks"] = fallbacks
                with self.db.transaction() as cursor:
                    cursor.execute(
                        "UPDATE catalog_item_projection SET payload=?,updated_at=CURRENT_TIMESTAMP "
                        "WHERE entity_id=? AND locale=?",
                        (json.dumps(payload, ensure_ascii=False), entity_id, locale),
                    )
                    cursor.execute(
                        "DELETE FROM catalog_artwork_selection WHERE entity_id=? AND locale=?",
                        (entity_id, locale),
                    )
                    cursor.executemany(
                        "INSERT INTO catalog_artwork_selection(entity_id,locale,image_type,provider,local_path,blur_hash,version,updated_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                        selection_rows,
                    )
            changed += 1
        return changed

    def project(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        payload: dict,
        *,
        preserve_artwork: set[str] | None = None,
        replace_metadata: bool = False,
    ) -> None:
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "catalog_search" not in tables:
            return
        entities = self.db.execute(
            "SELECT e.id,e.library_id,p.is_primary FROM entity_provider_ids p JOIN library_entities e ON e.id=p.entity_id WHERE p.provider=? AND p.provider_id=? AND e.entity_type=?",
            (provider, provider_id, entity_type),
        )
        has_projection = "catalog_item_projection" in tables
        has_genres = "catalog_item_genres" in tables
        genre_columns = (
            {
                row[1]
                for row in self.db.execute("PRAGMA table_info(catalog_item_genres)")
            }
            if has_genres
            else set()
        )
        has_root_grams = "catalog_root_search_grams" in tables
        has_artwork_selection = "catalog_artwork_selection" in tables
        artwork_selection_has_provider = has_artwork_selection and "provider" in {
            row[1]
            for row in self.db.execute("PRAGMA table_info(catalog_artwork_selection)")
        }
        metadata_image_columns = (
            {row[1] for row in self.db.execute("PRAGMA table_info(metadata_images)")}
            if "metadata_images" in tables
            else set()
        )
        has_blur_hash = "blur_hash" in metadata_image_columns
        has_fetched_at = "fetched_at" in metadata_image_columns
        for entity_id, library_id, is_primary in entities:
            while True:
                previous_text = None
                entity = None
                payload_text = None
                title_sort = ""
                rating_sort = 0.0
                release_sort = ""
                runtime_sort = 0.0
                gram_rows = []
                genre_rows = []
                artwork_rows = []
                artwork_removals = []
                if has_projection:
                    entity_rows = self.db.execute(
                        "SELECT parent_id,entity_type FROM library_entities WHERE id=?",
                        (entity_id,),
                    )
                    entity = entity_rows[0] if entity_rows else None
                    previous = self.db.execute(
                        "SELECT payload FROM catalog_item_projection WHERE entity_id=? AND locale=?",
                        (entity_id, locale),
                    )
                    previous_text = previous[0][0] if previous else None
                    try:
                        merged = json.loads(previous_text) if previous_text else {}
                    except (TypeError, ValueError, json.JSONDecodeError):
                        merged = {}
                    if not isinstance(merged, dict):
                        merged = {}
                    if replace_metadata and is_primary:
                        for field in (TEXT_FIELDS | FACT_FIELDS) - {"trailers"}:
                            merged.pop(field, None)
                    for field in (TEXT_FIELDS | FACT_FIELDS) - {"trailers"}:
                        if (
                            field in payload
                            and _usable_projection_value(payload[field])
                            and (
                                is_primary
                                or not _usable_projection_value(merged.get(field))
                            )
                        ):
                            merged[field] = payload[field]
                    merged["_catalogItemProjectionSchema"] = (
                        CATALOG_ITEM_PROJECTION_SCHEMA
                    )
                    provider_ids = [
                        {"provider": row[0], "id": row[1]}
                        for row in self.db.execute(
                            "SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=?",
                            (entity_id,),
                        )
                    ]
                    trailer_reader = MetadataReadService(self.db)
                    trailer_payloads = (
                        trailer_reader.payloads(entity_type, provider_ids)
                        if "metadata_cache" in tables
                        else {}
                    )
                    trailer_payloads[(provider, locale)] = payload
                    trailer_original = next(
                        (
                            _canonical_metadata_language(value.get("originalLanguage"))
                            for value in trailer_payloads.values()
                            if value.get("originalLanguage")
                        ),
                        None,
                    )
                    merged["trailers"] = MetadataReadService._localized_trailers(
                        trailer_payloads,
                        trailer_reader.providers(entity_type),
                        locale,
                        trailer_original,
                    )
                    current_images = merged.get("images")
                    if not isinstance(current_images, dict):
                        current_images = {}
                    artwork_providers = merged.get("_catalogArtworkProviders")
                    if not isinstance(artwork_providers, dict):
                        artwork_providers = {}
                    # Projections written before the provider provenance field
                    # was introduced can still have valid images but no owner.
                    # Seed missing ownership from the indexed selection rows so
                    # a refresh by a secondary provider can replace its own
                    # categories without disturbing primary-owned artwork.
                    if artwork_selection_has_provider:
                        selected_rows = self.db.execute(
                            "SELECT image_type,provider,local_path FROM catalog_artwork_selection "
                            "WHERE entity_id=? AND locale=? AND provider<>''",
                            (entity_id, locale),
                        )
                        selected_types: set[str] = set()
                        for (
                            selected_type,
                            selected_provider,
                            selected_path,
                        ) in selected_rows:
                            if _ready_file(selected_path):
                                selected_types.add(selected_type)
                                artwork_providers.setdefault(
                                    selected_type, selected_provider
                                )
                            elif (
                                artwork_providers.get(selected_type)
                                == selected_provider
                            ):
                                current_images.pop(selected_type, None)
                                artwork_providers.pop(selected_type, None)
                        for owned_type in [
                            image_type
                            for image_type, owner in artwork_providers.items()
                            if owner == provider and image_type not in selected_types
                        ]:
                            current_images.pop(owned_type, None)
                            artwork_providers.pop(owned_type, None)
                    artwork_fallbacks = merged.get("_catalogArtworkFallbacks")
                    if not isinstance(artwork_fallbacks, dict):
                        artwork_fallbacks = {}
                    for local_type in [
                        image_type
                        for image_type, owner in artwork_providers.items()
                        if owner == "local"
                    ]:
                        fallback = artwork_fallbacks.pop(local_type, None)
                        if (
                            isinstance(fallback, dict)
                            and isinstance(fallback.get("image"), dict)
                            and fallback.get("provider")
                        ):
                            current_images[local_type] = fallback["image"]
                            artwork_providers[local_type] = fallback["provider"]
                        else:
                            current_images.pop(local_type, None)
                            artwork_providers.pop(local_type, None)
                    global_artwork = self._global_ready_artwork(
                        entity_id,
                        entity_type,
                        locale,
                        payload,
                        provider_ids,
                        provider,
                    )
                    for image_type, (
                        choice,
                        cached_row,
                        selected_provider,
                    ) in global_artwork.items():
                        if preserve_artwork and image_type in preserve_artwork:
                            continue
                        projected = {
                            "url": f"/api/catalog/items/{entity_id}/images/{image_type}?language={locale}",
                            "language": choice.get("language"),
                            "width": choice.get("width") or 0,
                            "height": choice.get("height") or 0,
                        }
                        version = _asset_version(
                            cached_row[0],
                            f"{choice.get('url') or ''}:{cached_row[2] or ''}",
                        )
                        projected["url"] += f"&v={version}"
                        if image_type != "Logo" and cached_row[1]:
                            projected["blurHash"] = cached_row[1]
                        if has_artwork_selection:
                            artwork_rows.append(
                                (
                                    entity_id,
                                    locale,
                                    image_type,
                                    selected_provider,
                                    cached_row[0],
                                    cached_row[1],
                                    version,
                                )
                            )
                        current_images[image_type] = projected
                        artwork_providers[image_type] = selected_provider
                    if "media_files" in tables:
                        media_columns = {
                            row[1]
                            for row in self.db.execute("PRAGMA table_info(media_files)")
                        }
                        if "quick_fingerprint" in media_columns:
                            blur_field = (
                                ",image_blur_hash"
                                if "image_blur_hash" in media_columns
                                else ",NULL"
                            )
                            selected_local: set[str] = set()
                            for (
                                relative_path,
                                fingerprint,
                                blur_hash,
                            ) in self.db.execute(
                                "SELECT relative_path,quick_fingerprint"
                                + blur_field
                                + " FROM media_files WHERE entity_id=? AND role='image' ORDER BY relative_path COLLATE NOCASE",
                                (entity_id,),
                            ):
                                stem = Path(relative_path or "").stem.casefold()
                                for image_type, names in LOCAL_ARTWORK_NAMES.items():
                                    if (
                                        image_type in selected_local
                                        or stem not in names
                                        or not fingerprint
                                    ):
                                        continue
                                    if image_type in current_images:
                                        artwork_fallbacks[image_type] = {
                                            "image": current_images[image_type],
                                            "provider": artwork_providers.get(
                                                image_type
                                            ),
                                        }
                                    local_image = {
                                        "url": f"/api/catalog/items/{entity_id}/images/{image_type}?language={locale}&v={str(fingerprint)[:12]}",
                                        "language": None,
                                        "width": 0,
                                        "height": 0,
                                    }
                                    if image_type != "Logo" and blur_hash:
                                        local_image["blurHash"] = blur_hash
                                    current_images[image_type] = local_image
                                    artwork_providers[image_type] = "local"
                                    selected_local.add(image_type)
                                    break
                    merged["images"] = current_images
                    merged["_catalogArtworkProviders"] = artwork_providers
                    merged["_catalogArtworkFallbacks"] = artwork_fallbacks
                    payload_text = json.dumps(merged, ensure_ascii=False)
                    title_sort = normalize_search_text(merged.get("title") or "")
                    rating_sort = float(merged.get("communityRating") or 0)
                    release_sort = str(
                        merged.get("date") or merged.get("releaseDate") or ""
                    )
                    runtime_sort = float(merged.get("runtimeMinutes") or 0)
                    if (
                        entity
                        and entity[0] is None
                        and entity[1] in {"movie", "series", "collection"}
                    ):
                        documents = [(locale, merged.get("title") or "")]
                        if merged.get("originalTitle"):
                            documents.append(("original", merged["originalTitle"]))
                        gram_rows = [
                            (
                                gram,
                                entity_id,
                                document_locale,
                                library_id,
                                normalize_search_text(document_title),
                            )
                            for document_locale, document_title in documents
                            for gram in search_grams(document_title)
                        ]
                    genres = merged.get("genres") or merged.get("tags") or []
                    genre_rows = [
                        (
                            entity_id,
                            locale,
                            library_id,
                            entity[1] if entity else entity_type,
                            normalize_search_text(genre),
                            str(genre).strip(),
                        )
                        for genre in genres
                        if isinstance(genre, str) and genre.strip()
                    ]
                with self.db.transaction() as cursor:
                    if has_projection:
                        current = cursor.execute(
                            "SELECT payload FROM catalog_item_projection WHERE entity_id=? AND locale=?",
                            (entity_id, locale),
                        ).fetchone()
                        current_text = current[0] if current else None
                        if current_text != previous_text:
                            continue
                    cursor.execute(
                        "DELETE FROM catalog_search WHERE entity_id=? AND locale=?",
                        (entity_id, locale),
                    )
                    if merged.get("title"):
                        cursor.execute(
                            "INSERT INTO catalog_search(entity_id,library_id,locale,title) VALUES(?,?,?,?)",
                            (entity_id, library_id, locale, str(merged["title"])),
                        )
                    if merged.get("originalTitle"):
                        cursor.execute(
                            "DELETE FROM catalog_search WHERE entity_id=? AND locale='original'",
                            (entity_id,),
                        )
                        cursor.execute(
                            "INSERT INTO catalog_search(entity_id,library_id,locale,title) VALUES(?,?,?,?)",
                            (
                                entity_id,
                                library_id,
                                "original",
                                str(merged["originalTitle"]),
                            ),
                        )
                    if has_projection:
                        cursor.execute(
                            "INSERT INTO catalog_item_projection(entity_id,locale,library_id,parent_id,entity_type,payload,title_sort,rating_sort,release_sort,runtime_sort,updated_at,generation) VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,1) "
                            "ON CONFLICT(entity_id,locale) DO UPDATE SET payload=excluded.payload,title_sort=excluded.title_sort,rating_sort=excluded.rating_sort,release_sort=excluded.release_sort,runtime_sort=excluded.runtime_sort,updated_at=excluded.updated_at",
                            (
                                entity_id,
                                locale,
                                library_id,
                                entity[0] if entity else None,
                                entity[1] if entity else entity_type,
                                payload_text,
                                title_sort,
                                rating_sort,
                                release_sort,
                                runtime_sort,
                            ),
                        )
                        cursor.execute(
                            "DELETE FROM catalog_search_grams WHERE entity_id=? AND locale=?",
                            (entity_id, locale),
                        )
                        cursor.execute(
                            "DELETE FROM catalog_search_grams WHERE entity_id=? AND locale='original'",
                            (entity_id,),
                        )
                        cursor.executemany(
                            "INSERT OR IGNORE INTO catalog_search_grams(gram,entity_id,locale,library_id,parent_id) VALUES(?,?,?,?,NULL)",
                            [row[:4] for row in gram_rows],
                        )
                        if has_root_grams:
                            cursor.execute(
                                "DELETE FROM catalog_root_search_grams WHERE entity_id=? AND locale=?",
                                (entity_id, locale),
                            )
                            cursor.execute(
                                "DELETE FROM catalog_root_search_grams WHERE entity_id=? AND locale='original'",
                                (entity_id,),
                            )
                            cursor.executemany(
                                "INSERT OR IGNORE INTO catalog_root_search_grams(gram,entity_id,locale,library_id,title_sort) VALUES(?,?,?,?,?)",
                                gram_rows,
                            )
                        if has_genres:
                            cursor.execute(
                                "DELETE FROM catalog_item_genres WHERE entity_id=? AND locale=?",
                                (entity_id, locale),
                            )
                            if {"library_id", "entity_type"} <= genre_columns:
                                cursor.executemany(
                                    "INSERT OR IGNORE INTO catalog_item_genres(entity_id,locale,library_id,entity_type,genre_key,genre_name) VALUES(?,?,?,?,?,?)",
                                    genre_rows,
                                )
                            else:
                                cursor.executemany(
                                    "INSERT OR IGNORE INTO catalog_item_genres(entity_id,locale,genre_key,genre_name) VALUES(?,?,?,?)",
                                    [
                                        (row[0], row[1], row[4], row[5])
                                        for row in genre_rows
                                    ],
                                )
                        if has_artwork_selection:
                            if artwork_removals:
                                cursor.executemany(
                                    "DELETE FROM catalog_artwork_selection WHERE entity_id=? AND locale=? AND provider=? AND image_type=?",
                                    [
                                        (entity_id, locale, provider, image_type)
                                        for image_type in set(artwork_removals)
                                    ],
                                )
                            cursor.executemany(
                                "INSERT INTO catalog_artwork_selection(entity_id,locale,image_type,provider,local_path,blur_hash,version,updated_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP) "
                                "ON CONFLICT(entity_id,locale,image_type) DO UPDATE SET provider=excluded.provider,local_path=excluded.local_path,blur_hash=excluded.blur_hash,version=excluded.version,updated_at=excluded.updated_at",
                                artwork_rows,
                            )
                break


def reproject_entity_artwork(
    db, entity_id: str, locales: Iterable[str] | None = None
) -> int:
    return MetadataSearchProjection(db).reproject_entity_artwork(entity_id, locales)


class MetadataReadService:
    """Resolve metadata consistently for public and administrator callers."""

    def __init__(self, db=None, cache: MetadataCache | None = None):
        if db is None:
            from app.config import Config

            db = Config().database
        self.db = db
        self.cache = cache or MetadataCache()
        self._metadata_image_columns: set[str] | None = None
        self._blur_hashes: dict[tuple[str, str, str, str, str], str | None] = {}
        self._payloads: dict[
            tuple[str, tuple[tuple[str, str], ...]], dict[tuple[str, str], dict]
        ] = {}
        self._public_resolutions: dict[
            tuple[str, str, tuple[tuple[str, str], ...], str, bool], dict
        ] = {}

    @staticmethod
    def providers(entity_type: str) -> list[str]:
        return list(PROVIDER_PRIORITIES.get(entity_type, []))

    def payloads(
        self, entity_type: str, provider_ids: Iterable[dict]
    ) -> dict[tuple[str, str], dict]:
        provider_ids = list(provider_ids)
        identities = tuple(
            sorted(
                (str(identity.get("provider")), str(identity.get("id")))
                for identity in provider_ids
                if identity.get("provider") and identity.get("id")
            )
        )
        cache_key = (entity_type, identities)
        cached = self._payloads.get(cache_key)
        if cached is not None:
            return cached
        payloads: dict[tuple[str, str], dict] = {}
        for identity in provider_ids:
            provider = identity.get("provider")
            provider_id = identity.get("id")
            if not provider or not provider_id:
                continue
            rows = self.db.execute(
                "SELECT locale,payload FROM metadata_cache WHERE provider=? AND entity_type=? AND provider_id=?",
                (provider, entity_type, provider_id),
            )
            for locale, encoded in rows:
                try:
                    value = json.loads(encoded)
                except (TypeError, json.JSONDecodeError):
                    continue
                if value.get("_imageLanguageSchema") == IMAGE_LANGUAGE_SCHEMA:
                    payloads.setdefault((provider, locale), value)
        self._payloads[cache_key] = payloads
        return payloads

    def resolve_raw(
        self, entity_type: str, provider_ids: Iterable[dict], requested: str
    ) -> dict:
        provider_ids = list(provider_ids)
        payloads = self.payloads(entity_type, provider_ids)
        available = {locale for _, locale in payloads}
        original = next(
            (
                _canonical_metadata_language(value.get("originalLanguage"))
                for value in payloads.values()
                if value.get("originalLanguage")
            ),
            None,
        )
        configured = MetadataLanguageSettings().get()
        tiers = fallback_tiers(
            requested,
            original,
            media=False,
            include_english=any(language_family(value) == "en" for value in configured),
        )
        providers = self.providers(entity_type)
        result: dict = {}

        for key in TEXT_FIELDS:
            for tier in tiers:
                found = False
                for locale in locale_variants(tier, available):
                    for provider in providers:
                        value = payloads.get((provider, locale), {}).get(key)
                        if usable_text(key, value):
                            result[key] = value
                            found = True
                            break
                    if found:
                        break
                if found:
                    break

        if isinstance(result.get("people"), list):
            result["people"] = [
                {
                    key: value
                    for key, value in person.items()
                    if key in {"name", "role", "department", "order"}
                }
                for person in result["people"]
                if isinstance(person, dict)
            ]

        for key in FACT_FIELDS:
            for provider in providers:
                value = next(
                    (
                        payloads[(provider, locale)].get(key)
                        for tier in tiers
                        for locale in locale_variants(tier, available)
                        if (provider, locale) in payloads
                        and usable_text(key, payloads[(provider, locale)].get(key))
                    ),
                    None,
                )
                if nonempty(value):
                    result[key] = (
                        _canonical_metadata_language(value)
                        if key == "originalLanguage"
                        else value
                    )
                    break

        locale_order: list[str] = []
        for tier in tiers:
            for locale in locale_variants(tier, available):
                if locale not in locale_order:
                    locale_order.append(locale)
        locale_order.extend(
            sorted(locale for locale in available if locale not in locale_order)
        )
        images = []
        for provider in providers:
            for locale in locale_order:
                payload = payloads.get((provider, locale))
                if not payload:
                    continue
                images.extend(
                    image
                    for image in payload.get("images", []) or []
                    if isinstance(image, dict)
                    and image.get("type") in ARTWORK_CATEGORY_SET
                )
        result["images"] = images
        result["trailers"] = self._localized_trailers(
            payloads, providers, requested, original
        )
        return result

    def resolve_public(
        self,
        entity_id: str,
        entity_type: str,
        provider_ids: Iterable[dict],
        requested: str,
    ) -> dict:
        provider_ids = list(provider_ids)
        identities = tuple(
            sorted(
                (str(identity.get("provider")), str(identity.get("id")))
                for identity in provider_ids
                if identity.get("provider") and identity.get("id")
            )
        )
        prefer_no_language_for_backdrop = (
            MetadataLanguageSettings().prefer_no_language_for_backdrop()
        )
        cache_key = (
            entity_id,
            entity_type,
            identities,
            requested,
            prefer_no_language_for_backdrop,
        )
        cached = self._public_resolutions.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)
        raw = self.resolve_raw(entity_type, provider_ids, requested)
        original = raw.get("originalLanguage")
        providers = self.providers(entity_type)
        selected = {}
        for image_type in sorted(ARTWORK_CATEGORY_SET):
            choice = self.ready_artwork(
                entity_type,
                provider_ids,
                raw.get("images", []),
                requested,
                image_type,
                original,
                providers,
                entity_id=entity_id,
                prefer_no_language_for_backdrop=prefer_no_language_for_backdrop,
            )
            if choice:
                image = {
                    "url": f"/api/catalog/items/{entity_id}/images/{image_type}?language={requested}",
                    "language": choice.get("language"),
                    "width": choice.get("width") or 0,
                    "height": choice.get("height") or 0,
                }
                blur_hash = (
                    choice.get("blurHash")
                    if choice.get("provider") == "screen_extractor"
                    else self._image_blur_hash(
                        choice.get("provider"),
                        entity_type,
                        next(
                            (
                                identity.get("id")
                                for identity in provider_ids
                                if identity.get("provider") == choice.get("provider")
                            ),
                            None,
                        ),
                        image_type,
                        choice.get("url"),
                    )
                )
                if blur_hash and image_type != "Logo":
                    image["blurHash"] = blur_hash
                selected[image_type] = image
        raw["images"] = selected
        resolved = {
            "itemId": entity_id,
            "requestedLanguage": requested,
            "originalLanguage": original,
            "metadata": raw,
        }
        self._public_resolutions[cache_key] = copy.deepcopy(resolved)
        return resolved

    def ready_artwork(
        self,
        entity_type: str,
        provider_ids: Iterable[dict],
        images: Iterable[dict],
        requested: str,
        image_type: str,
        original: str | None,
        providers: list[str] | None = None,
        entity_id: str | None = None,
        prefer_no_language_for_backdrop: bool | None = None,
    ) -> dict | None:
        """Select the first provider-ordered candidate with a ready file."""
        if prefer_no_language_for_backdrop is None:
            prefer_no_language_for_backdrop = (
                MetadataLanguageSettings().prefer_no_language_for_backdrop()
            )
        identities = {
            str(identity.get("provider")): str(identity.get("id"))
            for identity in provider_ids
            if identity.get("provider") and identity.get("id")
        }
        provider_order = providers or self.providers(entity_type)
        columns = self._metadata_image_columns
        if columns is None:
            columns = self._metadata_image_columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(metadata_images)")
            }
        has_metadata_images = "metadata_images" in {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for candidate in rank_artwork_candidates(
            images,
            requested,
            image_type,
            original,
            provider_order,
            include_english=any(
                language_family(value) == "en"
                for value in MetadataLanguageSettings().get()
            ),
            prefer_no_language_for_backdrop=prefer_no_language_for_backdrop,
        ):
            provider = str(candidate.get("provider") or "")
            provider_id = identities.get(provider)
            url = candidate.get("url")
            if not provider_id or not isinstance(url, str):
                continue
            path_sql = ",local_path" if "local_path" in columns else ""
            if not has_metadata_images:
                break
            rows = self.db.execute(
                "SELECT 1" + path_sql + " FROM metadata_images WHERE provider=? "
                "AND entity_type=? AND provider_id=? AND image_type=? "
                "AND image_url=? "
                + ("AND local_path IS NOT NULL " if "local_path" in columns else "")
                + "LIMIT 1",
                (provider, entity_type, provider_id, image_type, url),
            )
            if rows and ("local_path" not in columns or _ready_file(rows[0][1])):
                return candidate
        if (
            entity_id
            and image_type == "Primary"
            and entity_type in {"movie", "episode"}
        ):
            has_screen_assets = bool(
                self.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='screen_extractor_assets'"
                )
            )
            if has_screen_assets:
                from app.screen_extractor import ready_artwork

                generated = ready_artwork(self.db, entity_id, entity_type)
                if generated:
                    return generated
        return None

    def _image_blur_hash(
        self,
        provider: object,
        entity_type: str,
        provider_id: object,
        image_type: str,
        image_url: object,
    ) -> str | None:
        if self._metadata_image_columns is None:
            self._metadata_image_columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(metadata_images)")
            }
        columns = self._metadata_image_columns
        if "blur_hash" not in columns or not all(
            isinstance(value, str) and value
            for value in (provider, provider_id, image_url)
        ):
            return None
        key = (provider, entity_type, provider_id, image_type, image_url)
        if key in self._blur_hashes:
            return self._blur_hashes[key]
        rows = self.db.execute(
            "SELECT blur_hash FROM metadata_images WHERE provider=? AND entity_type=? AND provider_id=? AND image_type=? AND image_url=? AND blur_hash IS NOT NULL LIMIT 1",
            (provider, entity_type, provider_id, image_type, image_url),
        )
        value = rows[0][0] if rows and rows[0][0] else None
        self._blur_hashes[key] = value
        return value

    @staticmethod
    def _localized_trailers(
        payloads: dict[tuple[str, str], dict],
        providers: list[str],
        requested: str,
        original: str | None,
    ) -> list[dict]:
        configured = MetadataLanguageSettings().get()
        tiers = fallback_tiers(
            requested,
            original,
            media=True,
            include_english=any(language_family(value) == "en" for value in configured),
        )
        provider_rank = {value: index for index, value in enumerate(providers)}
        candidates = []
        for (provider, locale), payload in payloads.items():
            if provider not in provider_rank:
                continue
            for trailer in payload.get("trailers", []) or []:
                if not isinstance(trailer, dict) or not trailer.get("url"):
                    continue
                trailer_language = trailer.get("language") or ""
                rank = 99
                for index, tier in enumerate(tiers):
                    trailer_tag = str(trailer_language).lower().replace("_", "-")
                    tier_tag = str(tier).lower().replace("_", "-")
                    if trailer_tag == tier_tag:
                        rank = index * 2
                        break
                    if (
                        trailer_tag
                        and tier_tag
                        and language_family(trailer_tag) == language_family(tier_tag)
                    ):
                        rank = index * 2 + 1
                        break
                    if rank < 99:
                        break
                if rank < 99:
                    clean = {
                        key: trailer.get(key)
                        for key in (
                            "url",
                            "site",
                            "key",
                            "name",
                            "type",
                            "official",
                            "language",
                        )
                        if trailer.get(key) is not None
                    }
                    candidates.append((rank, provider_rank[provider], clean))
        if not candidates:
            return []
        best_rank = min(value[0] for value in candidates)
        result = []
        seen_urls = set()
        for _, _, trailer in sorted(
            (value for value in candidates if value[0] == best_rank),
            key=lambda value: (value[1], value[2].get("url", "")),
        ):
            url = trailer.get("url")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            result.append(trailer)
        return result


class MetadataIngestService:
    """Own configured-locale enumeration for scans and refresh jobs."""

    def __init__(
        self,
        metadata_service=None,
        settings: MetadataLanguageSettings | None = None,
        image_ingest=None,
        credit_ingest=None,
        background_assets: bool = True,
    ):
        if metadata_service is None:
            from app.providers import MetadataService

            metadata_service = MetadataService()
        self.metadata_service = metadata_service
        self.settings = settings or MetadataLanguageSettings()
        self._locales = self.settings.get()
        if image_ingest is None:
            cache = getattr(metadata_service, "cache", None)
            if cache is not None and getattr(cache, "db", None) is not None:
                image_ingest = MetadataImageIngestService(cache)
        self.image_ingest = image_ingest
        self.background_assets = background_assets
        if credit_ingest is None:
            cache = getattr(metadata_service, "cache", None)
            if cache is not None and getattr(cache, "db", None) is not None:
                credit_ingest = PersonCreditIngestService(cache)
        self.credit_ingest = credit_ingest

    def locales(self) -> list[str]:
        return list(self._locales)

    def ingest(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        *,
        force: bool = False,
        force_assets: bool | None = None,
        replace_metadata: bool = False,
        should_terminate=None,
    ) -> list[dict]:
        if provider not in {"tmdb", "tvdb", "musicbrainz"}:
            return []
        should_terminate = should_terminate or (lambda: False)
        locales = []
        for locale in self.locales():
            if should_terminate():
                break
            locales.append(locale)
        return list(
            self.ingest_locales(
                provider,
                entity_type,
                provider_id,
                locales,
                force=force,
                force_assets=force_assets,
                replace_metadata=replace_metadata,
            ).values()
        )

    def ingest_locales(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locales: list[str] | None = None,
        *,
        force: bool = False,
        force_assets: bool | None = None,
        replace_metadata: bool = False,
    ) -> dict[str, dict]:
        locales = list(dict.fromkeys(locales or self.locales()))
        unsupported = [locale for locale in locales if locale not in self._locales]
        if unsupported:
            raise ValueError(f"Metadata language is not configured: {unsupported[0]}")
        if force_assets is None:
            force_assets = force
        complete_batch = set(locales) == set(self._locales)
        with metadata_fetch_activity():
            if hasattr(self.metadata_service, "fetch_locales"):
                values = self.metadata_service.fetch_locales(
                    provider,
                    entity_type,
                    provider_id,
                    locales,
                    force=force,
                )
            else:
                values = {
                    locale: self.metadata_service.fetch(
                        provider,
                        entity_type,
                        provider_id,
                        locale,
                        force=force,
                    )
                    for locale in locales
                }
        if len(locales) == 1:
            locale = locales[0]
            return {
                locale: self.ingest_document(
                    provider,
                    entity_type,
                    provider_id,
                    locale,
                    values[locale],
                    force_assets=force_assets,
                    replace_metadata=replace_metadata,
                    complete_batch=complete_batch,
                )
            }

        cache = getattr(self.metadata_service, "cache", None)
        db = getattr(cache, "db", None)
        if db is not None:
            for locale in locales:
                MetadataSearchProjection(db).project(
                    provider,
                    entity_type,
                    provider_id,
                    locale,
                    values[locale],
                    replace_metadata=replace_metadata,
                )

        def materialize_assets() -> None:
            if self.image_ingest is not None:
                batch_ingest = getattr(self.image_ingest, "ingest_documents", None)
                if batch_ingest is not None:
                    batch_ingest(
                        provider,
                        entity_type,
                        provider_id,
                        values,
                        force=force_assets,
                        complete_batch=complete_batch,
                    )
                else:
                    for locale in locales:
                        self.image_ingest.ingest(
                            provider,
                            entity_type,
                            provider_id,
                            locale,
                            values[locale],
                            force=force_assets,
                            complete_batch=complete_batch,
                        )
            if self.credit_ingest is not None:
                for locale in locales:
                    self.credit_ingest.ingest(
                        provider,
                        entity_type,
                        provider_id,
                        locale,
                        values[locale],
                        force_images=force_assets,
                    )

        if self.image_ingest is not None or self.credit_ingest is not None:
            digest = hashlib.sha256(
                json.dumps(values, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            key = (
                provider,
                entity_type,
                provider_id,
                tuple(locales),
                digest,
                int(force_assets),
            )
            if self.background_assets:
                asset_executor.submit(key, materialize_assets)
            else:
                asset_executor.submit_wait(key, materialize_assets)
        return values

    def ingest_locale(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        *,
        force: bool = False,
        force_assets: bool | None = None,
        replace_metadata: bool = False,
    ) -> dict:
        if locale not in self.locales():
            raise ValueError(f"Metadata language is not configured: {locale}")
        return self.ingest_locales(
            provider,
            entity_type,
            provider_id,
            [locale],
            force=force,
            force_assets=force_assets,
            replace_metadata=replace_metadata,
        )[locale]

    def ingest_document(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        normalized: dict,
        *,
        force_assets: bool = False,
        replace_metadata: bool = False,
        complete_batch: bool | None = None,
    ) -> dict:
        """Materialize a normalized document, including documents cached by aggregation."""
        if locale not in self.locales():
            raise ValueError(f"Metadata language is not configured: {locale}")
        if complete_batch is None:
            complete_batch = len(self._locales) == 1
        cache = getattr(self.metadata_service, "cache", None)
        db = getattr(cache, "db", None)
        if db is not None:
            MetadataSearchProjection(db).project(
                provider,
                entity_type,
                provider_id,
                locale,
                normalized,
                replace_metadata=replace_metadata,
            )
        if self.image_ingest is not None or self.credit_ingest is not None:

            def materialize_assets() -> None:
                # Cache hits also run this path so rows created before eager
                # asset ingestion are repaired without blocking metadata.
                if self.image_ingest is not None:
                    self.image_ingest.ingest(
                        provider,
                        entity_type,
                        provider_id,
                        locale,
                        normalized,
                        force=force_assets,
                        complete_batch=complete_batch,
                    )
                if self.credit_ingest is not None:
                    self.credit_ingest.ingest(
                        provider,
                        entity_type,
                        provider_id,
                        locale,
                        normalized,
                        force_images=force_assets,
                    )

            digest = hashlib.sha256(
                json.dumps(normalized, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            key = (
                provider,
                entity_type,
                provider_id,
                locale,
                digest,
                int(force_assets),
            )
            if self.background_assets:
                asset_executor.submit(key, materialize_assets)
            else:
                asset_executor.submit_wait(key, materialize_assets)
        return normalized


class MetadataImageIngestService:
    """Materialize provider-ordered artwork winners during scan/refresh."""

    MAX_IMAGE_BYTES = 20 * 1024 * 1024
    _http_local = threading.local()
    _file_locks = tuple(threading.Lock() for _ in range(64))

    def __init__(
        self,
        cache,
        image_root: str | Path | None = None,
        downloader=None,
        encoder=None,
        hasher=None,
    ):
        self.cache = cache
        self.db = cache.db
        if image_root is None:
            db_file = getattr(self.db, "db_file", None)
            if not db_file or db_file == ":memory:":
                image_root = None
            else:
                image_root = Path(db_file).parent / "metadata-cache" / "images"
        self.image_root = Path(image_root) if image_root is not None else None
        self.downloader = downloader
        self.encoder = encoder or encode_webp_bytes
        self.hasher = hasher or blurhash_for_image

    @staticmethod
    def _source_suffix(url: str) -> str:
        suffix = Path(urlparse(url).path).suffix.lower()
        return (
            suffix
            if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".svg", ".svgz"}
            else ".image"
        )

    def _target(self, url: str) -> Path | None:
        if self.image_root is None:
            return None
        name = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.image_root / f"{name}.webp"

    @classmethod
    def _file_lock(cls, target: Path):
        return cls._file_locks[hash(str(target)) % len(cls._file_locks)]

    @staticmethod
    def _validate_provider_url(value: str) -> None:
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("provider image URL is not an HTTP(S) URL")
        allowlist = {
            host.strip().lower()
            for host in os.getenv(
                "METADATA_IMAGE_HOST_ALLOWLIST",
                "image.tmdb.org,media.themoviedb.org,artworks.thetvdb.com",
            ).split(",")
            if host.strip()
        }
        hostname = parsed.hostname.casefold().rstrip(".")
        if not any(
            hostname == allowed or hostname.endswith("." + allowed)
            for allowed in allowlist
        ):
            raise ValueError("provider image host is not allowlisted")
        try:
            addresses = {
                info[4][0]
                for info in socket.getaddrinfo(
                    hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except OSError as error:
            raise ValueError("provider image host could not be resolved") from error
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                raise ValueError("provider image host resolves to a private address")

    def _download(self, url: str, target: Path) -> None:
        if self.downloader is not None:
            content = self.downloader(url)
        else:
            import httpx

            configured_timeout = float(
                os.getenv("METADATA_IMAGE_TIMEOUT_SECONDS", "20")
            )
            timeout = max(3.0, min(60.0, configured_timeout))
            client = getattr(self._http_local, "client", None)
            if client is None:
                client = self._http_local.client = httpx.Client(
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                    headers={
                        "Accept": "image/*",
                        "User-Agent": "ZenStream/metadata",
                    },
                    limits=httpx.Limits(
                        max_connections=16, max_keepalive_connections=8
                    ),
                )
            current = url
            content = bytearray()
            for _ in range(6):
                self._validate_provider_url(current)
                with client.stream("GET", current, timeout=timeout) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("provider redirect has no location")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(
                        ";", 1
                    )[0]
                    if content_type and not content_type.startswith("image/"):
                        raise ValueError(
                            f"provider returned non-image content type {content_type}"
                        )
                    for chunk in response.iter_bytes(64 * 1024):
                        content.extend(chunk)
                        if len(content) > self.MAX_IMAGE_BYTES:
                            raise ValueError("provider image exceeds the 20 MiB limit")
                    break
            else:
                raise ValueError("provider image redirect limit exceeded")
        if not content:
            raise ValueError("provider returned an empty image")
        if len(content) > self.MAX_IMAGE_BYTES:
            raise ValueError("provider image exceeds the 20 MiB limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.encoder(content, target, self._source_suffix(url))
        if not target.is_file() or not target.stat().st_size:
            raise RuntimeError("WebP encoder did not produce an image.")

    def _persist(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        image: dict,
        target: Path,
        blur_hash: str | None,
    ) -> None:
        record = (
            provider,
            entity_type,
            provider_id,
            image.get("language"),
            image.get("type"),
            image.get("url"),
            blur_hash,
            str(target),
        )
        if hasattr(self.cache, "put_images"):
            self.cache.put_images([record])
        else:
            self.cache.put_image(*record)

    def _blur_hash(self, url: str, target: Path, image_type: str) -> str | None:
        columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(metadata_images)")
        }
        existing = (
            self.db.execute(
                "SELECT blur_hash FROM metadata_images WHERE image_url=? "
                "AND local_path=? AND blur_hash IS NOT NULL LIMIT 1",
                (url, str(target)),
            )
            if "metadata_images"
            in {
                row[0]
                for row in self.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            and "blur_hash" in columns
            else []
        )
        blur_hash = existing[0][0] if existing else None
        if blur_hash is None:
            try:
                blur_hash = self.hasher(target)
            except Exception as error:
                logger.warning(
                    "metadata image BlurHash encoding failed type=%s url=%s error=%s",
                    image_type,
                    url,
                    error,
                )
        return blur_hash

    def _has_ready_category(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        image_type: str,
        locale: str,
    ) -> bool:
        """Whether this provider identity still has a usable cached image."""
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "metadata_images" not in tables:
            return False
        if "catalog_artwork_selection" in tables:
            selection_columns = {
                row[1]
                for row in self.db.execute(
                    "PRAGMA table_info(catalog_artwork_selection)"
                )
            }
            if {"entity_id", "locale", "image_type", "local_path"}.issubset(
                selection_columns
            ):
                selected = self.db.execute(
                    "SELECT s.local_path FROM catalog_artwork_selection s "
                    "JOIN entity_provider_ids p ON p.entity_id=s.entity_id "
                    "WHERE p.provider=? AND p.provider_id=? AND s.locale=? "
                    "AND s.image_type=? AND s.local_path IS NOT NULL LIMIT 8",
                    (provider, provider_id, locale, image_type),
                )
                if any(_ready_file(row[0]) for row in selected):
                    return True
        columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(metadata_images)")
        }
        path_clause = " AND local_path IS NOT NULL" if "local_path" in columns else ""
        rows = self.db.execute(
            "SELECT local_path FROM metadata_images WHERE provider=? AND entity_type=? "
            "AND provider_id=? AND image_type=?" + path_clause + " LIMIT 32",
            (provider, entity_type, provider_id, image_type),
        )
        if "local_path" not in columns:
            return bool(rows)
        return any(_ready_file(row[0]) for row in rows)

    def _prune_replaced(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        documents: dict[str, dict],
        outcomes: dict[str, str],
    ) -> None:
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "metadata_images" not in tables:
            return
        prefer_no_language_for_backdrop = (
            MetadataLanguageSettings().prefer_no_language_for_backdrop()
        )
        for image_type in ARTWORK_CATEGORIES:
            winners: set[str] = set()
            complete = True
            for locale, document in documents.items():
                images = (
                    document.get("images", []) if isinstance(document, dict) else []
                )
                candidates = rank_artwork_candidates(
                    images,
                    locale,
                    image_type,
                    document.get("originalLanguage"),
                    [provider],
                    include_english=any(
                        language_family(value) == "en"
                        for value in MetadataLanguageSettings().get()
                    ),
                    prefer_no_language_for_backdrop=prefer_no_language_for_backdrop,
                )
                choice = next(
                    (
                        candidate
                        for candidate in candidates[:2]
                        if outcomes.get(candidate.get("url")) == "ready"
                    ),
                    None,
                )
                if not choice or not choice.get("url"):
                    if candidates:
                        complete = False
                    continue
                url = str(choice["url"])
                winners.add(url)
            if not winners or not complete:
                continue
            rows = self.db.execute(
                "SELECT DISTINCT local_path FROM metadata_images WHERE provider=? "
                "AND entity_type=? AND provider_id=? AND image_type=? "
                "AND image_url NOT IN ("
                + ",".join("?" for _ in winners)
                + ") AND local_path IS NOT NULL",
                [provider, entity_type, provider_id, image_type, *sorted(winners)],
            )
            self.db.execute(
                "DELETE FROM metadata_images WHERE provider=? AND entity_type=? "
                "AND provider_id=? AND image_type=? AND image_url NOT IN ("
                + ",".join("?" for _ in winners)
                + ")",
                [provider, entity_type, provider_id, image_type, *sorted(winners)],
            )
            for (raw_path,) in rows:
                if not raw_path:
                    continue
                path = Path(str(raw_path))
                try:
                    path.resolve().relative_to(self.image_root.resolve())
                except (OSError, ValueError):
                    continue
                with self._file_lock(path):
                    still_referenced = self.db.execute(
                        "SELECT 1 FROM metadata_images WHERE local_path=? LIMIT 1",
                        (str(path),),
                    )
                    if not still_referenced and "catalog_artwork_selection" in tables:
                        still_referenced = self.db.execute(
                            "SELECT 1 FROM catalog_artwork_selection WHERE local_path=? LIMIT 1",
                            (str(path),),
                        )
                    if not still_referenced:
                        path.unlink(missing_ok=True)

    def ingest_documents(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        documents: dict[str, dict],
        *,
        force: bool = False,
        complete_batch: bool = False,
    ) -> dict[str, int]:
        """Materialize one provider winner per locale/category.

        The first provider-ordered candidate is attempted.  A single next
        candidate may be attempted after a failure, while the preferred
        candidate remains eligible for a later repair run.
        """
        if self.image_root is None:
            return {"ready": 0, "failed": 0, "skipped": 0}
        ready = failed = skipped = 0
        outcomes: dict[str, str] = {}
        preserved: dict[str, set[str]] = {}
        prefer_no_language_for_backdrop = (
            MetadataLanguageSettings().prefer_no_language_for_backdrop()
        )
        for locale, document in documents.items():
            images = document.get("images", []) if isinstance(document, dict) else []
            if not isinstance(images, list):
                continue
            for image_type in ARTWORK_CATEGORIES:
                candidates = rank_artwork_candidates(
                    images,
                    locale,
                    image_type,
                    document.get("originalLanguage"),
                    [provider],
                    include_english=any(
                        language_family(value) == "en"
                        for value in MetadataLanguageSettings().get()
                    ),
                    prefer_no_language_for_backdrop=prefer_no_language_for_backdrop,
                )
                if not candidates:
                    continue
                had_ready = self._has_ready_category(
                    provider, entity_type, provider_id, image_type, locale
                )
                for candidate in candidates[:2]:
                    url = candidate.get("url")
                    if not isinstance(url, str):
                        continue
                    parsed = urlparse(url)
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                        continue
                    outcome = outcomes.get(url)
                    if outcome == "failed":
                        if candidate is candidates[0] and had_ready:
                            preserved.setdefault(locale, set()).add(image_type)
                        continue
                    target = self._target(url)
                    if target is None:
                        continue
                    if (
                        outcome == "ready"
                        and target.is_file()
                        and target.stat().st_size > 0
                    ):
                        skipped += 1
                        self._persist(
                            provider,
                            entity_type,
                            provider_id,
                            candidate,
                            target,
                            self._blur_hash(
                                url, target, str(candidate.get("type") or image_type)
                            ),
                        )
                        outcomes[url] = "ready"
                        break
                    try:
                        with self._file_lock(target):
                            exists = target.is_file() and target.stat().st_size > 0
                            if not force and exists:
                                skipped += 1
                            else:
                                self._download(url, target)
                                ready += 1
                            outcomes[url] = "ready"
                        blur_hash = self._blur_hash(
                            url, target, str(candidate.get("type") or image_type)
                        )
                        self._persist(
                            provider,
                            entity_type,
                            provider_id,
                            candidate,
                            target,
                            blur_hash,
                        )
                        outcomes[url] = "ready"
                        break
                    except Exception as error:
                        outcomes[url] = "failed"
                        failed += 1
                        if candidate is candidates[0] and had_ready:
                            preserved.setdefault(locale, set()).add(image_type)
                        logger.warning(
                            "metadata image ingest failed provider=%s entity_type=%s provider_id=%s locale=%s type=%s url=%s error=%s",
                            provider,
                            entity_type,
                            provider_id,
                            locale,
                            image_type,
                            url,
                            error,
                        )
                # A failed preferred candidate may have a ready fallback, but
                # never fan out through the full provider candidate list.

        projection = MetadataSearchProjection(self.db)
        for locale, document in documents.items():
            projection.project(
                provider,
                entity_type,
                provider_id,
                locale,
                document,
                preserve_artwork=preserved.get(locale),
            )
        # Pruning is safe only when the caller supplied the complete
        # configured-locale document batch.  A single-locale replay from a
        # multi-locale configuration must remain non-destructive, otherwise
        # another locale's ready artwork could be mistaken for an obsolete
        # alternate.
        if complete_batch:
            self._prune_replaced(
                provider, entity_type, provider_id, documents, outcomes
            )
        return {"ready": ready, "failed": failed, "skipped": skipped}

    def ingest(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        document: dict,
        *,
        force: bool = False,
        complete_batch: bool = False,
    ) -> dict[str, int]:
        return self.ingest_documents(
            provider,
            entity_type,
            provider_id,
            {locale: document},
            force=force,
            complete_batch=complete_batch,
        )


class PersonCreditIngestService:
    """Materialize primary-provider video credits and their cached portraits."""

    def __init__(self, cache, image_root: str | Path | None = None):
        self.cache = cache
        self.db = cache.db
        db_file = getattr(self.db, "db_file", None)
        self.image_root = (
            Path(image_root)
            if image_root is not None
            else Path(db_file).parent / "people-cache"
            if db_file and db_file != ":memory:"
            else None
        )
        self.images = MetadataImageIngestService(cache, image_root=self.image_root)

    def _tables_ready(self) -> bool:
        names = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return {"people", "person_localizations", "entity_person_credits"} <= names

    @staticmethod
    def _records(document: dict) -> list[tuple[str, int, dict]]:
        credits = document.get("credits") if isinstance(document, dict) else None
        if not isinstance(credits, dict):
            return []
        values = []
        for credit_type in ("cast", "crew"):
            source = credits.get(credit_type)
            if not isinstance(source, list):
                continue
            for index, record in enumerate(source):
                if isinstance(record, dict) and str(record.get("name") or "").strip():
                    values.append((credit_type, index, record))
        return values

    def _portrait(self, person_id: str, image_url: object, force: bool = False):
        if not isinstance(image_url, str) or not image_url:
            return None
        parsed = urlparse(image_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or self.image_root is None
        ):
            return None
        target = self.images._target(image_url)
        if target is None:
            return None
        try:
            current = self.db.execute(
                "SELECT image_url,local_path,image_blur_hash FROM people WHERE id=?",
                (person_id,),
            )
            if (
                not force
                and current
                and current[0][0] == image_url
                and current[0][1] == str(target)
                and current[0][2]
                and target.is_file()
                and target.stat().st_size
            ):
                return None
            with self.images._file_lock(target):
                if force or not target.is_file() or not target.stat().st_size:
                    self.images._download(image_url, target)
            blur_hash = (
                current[0][2]
                if not force and current and current[0][0] == image_url
                else None
            )
            try:
                if blur_hash is None:
                    blur_hash = self.images.hasher(target)
            except Exception as error:
                logger.warning(
                    "person portrait BlurHash encoding failed url=%s error=%s",
                    image_url,
                    error,
                )
            return (image_url, str(target), blur_hash, iso_now(), person_id)
        except Exception as error:
            logger.warning(
                "person portrait ingest failed person_id=%s url=%s error=%s",
                person_id,
                image_url,
                error,
            )
            return None

    @staticmethod
    def _person_id(cursor, provider: str, identity: str, name: str, locale: str) -> str:
        rows = cursor.execute(
            "SELECT id FROM people WHERE provider=? AND provider_person_id=?",
            (provider, identity),
        ).fetchall()
        person_id = rows[0][0] if rows else str(uuid.uuid4())
        now = iso_now()
        if rows:
            cursor.execute(
                "UPDATE people SET updated_at=? WHERE id=?", (now, person_id)
            )
        else:
            cursor.execute(
                "INSERT INTO people(id,provider,provider_person_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                (person_id, provider, identity, now, now),
            )
        cursor.execute(
            "INSERT INTO person_localizations(person_id,locale,name,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(person_id,locale) DO UPDATE SET name=excluded.name,updated_at=excluded.updated_at",
            (person_id, locale, name, now),
        )
        return person_id

    def ingest(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        document: dict,
        *,
        force_images: bool = False,
    ) -> None:
        if provider not in {"tmdb", "tvdb"} or entity_type not in {
            "movie",
            "series",
            "season",
            "episode",
        }:
            return
        if not self._tables_ready():
            return
        normalized_records = []
        for credit_type, fallback_order, record in self._records(document):
            name = str(record.get("name") or "").strip()
            source_id = str(record.get("id") or "").strip()
            role = str(record.get("role") or "").strip() or None
            department = str(record.get("department") or "").strip() or None
            order = record.get("order", fallback_order)
            try:
                order = int(order)
            except (TypeError, ValueError):
                order = fallback_order
            normalized_records.append(
                (
                    credit_type,
                    fallback_order,
                    name,
                    source_id,
                    role,
                    department,
                    order,
                    record.get("imageUrl"),
                )
            )
        entity_ids = [
            row[0]
            for row in self.db.execute(
                "SELECT p.entity_id FROM entity_provider_ids p JOIN library_entities e ON e.id=p.entity_id "
                "WHERE p.provider=? AND p.provider_id=? AND p.is_primary=1 AND e.entity_type=?",
                (provider, provider_id, entity_type),
            )
        ]
        prepared_by_entity = {}
        for entity_id in entity_ids:
            prepared_by_entity[entity_id] = [
                (
                    str(uuid.uuid4()),
                    credit_type,
                    name,
                    source_id
                    or "credit:"
                    + hashlib.sha256(
                        f"{entity_id}|{locale}|{credit_type}|{fallback_order}|{name}|{role or ''}".encode()
                    ).hexdigest(),
                    role,
                    department,
                    order,
                    image_url,
                )
                for (
                    credit_type,
                    fallback_order,
                    name,
                    source_id,
                    role,
                    department,
                    order,
                    image_url,
                ) in normalized_records
            ]
        portraits = []
        for entity_id in entity_ids:
            with self.db.transaction() as cursor:
                credits = []
                for (
                    credit_id,
                    credit_type,
                    name,
                    identity,
                    role,
                    department,
                    order,
                    image_url,
                ) in prepared_by_entity[entity_id]:
                    person_id = self._person_id(
                        cursor, provider, identity, name, locale
                    )
                    portraits.append((person_id, image_url))
                    credits.append(
                        (
                            credit_id,
                            entity_id,
                            person_id,
                            provider,
                            locale,
                            credit_type,
                            role,
                            department,
                            order,
                        )
                    )
                cursor.execute(
                    "DELETE FROM entity_person_credits WHERE entity_id=? AND provider=? AND locale=?",
                    (entity_id, provider, locale),
                )
                cursor.executemany(
                    "INSERT INTO entity_person_credits(id,entity_id,person_id,provider,locale,credit_type,role,department,credit_order) VALUES(?,?,?,?,?,?,?,?,?)",
                    credits,
                )
        updates = [
            update
            for person_id, image_url in portraits
            if (update := self._portrait(person_id, image_url, force=force_images))
            is not None
        ]
        if updates:
            with self.db.transaction() as cursor:
                cursor.executemany(
                    "UPDATE people SET image_url=?,local_path=?,image_blur_hash=?,updated_at=? WHERE id=?",
                    updates,
                )
