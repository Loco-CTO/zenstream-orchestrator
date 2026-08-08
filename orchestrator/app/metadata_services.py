from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import hashlib
import os
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


from app.metadata_domain import (
    ARTWORK_CATEGORY_SET,
    choose_artwork,
    fallback_tiers,
    locale_variants,
    nonempty,
)
from app.models.metadata import (
    IMAGE_LANGUAGE_SCHEMA,
    MetadataCache,
    MetadataLanguageSettings,
    iso_now,
)
from app.logging_config import get_logger
from app.images import blurhash_for_image, encode_webp_bytes
from app.worker_config import configured_worker_limit


logger = get_logger("metadata")


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

    def submit(self, key: tuple, work) -> str:
        with self._lock:
            current = self._pending.get(key)
            if current is not None and not current.done():
                return self._states.get(key, "pending")
            self._states[key] = "pending"

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
                    self._pending.pop(key, None)

            future.add_done_callback(finished)
            return "pending"

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
            return self._states.get(key)


asset_executor = MetadataAssetExecutor()


def _canonical_metadata_language(value: object) -> str | None:
    raw = str(value or "").strip().replace("_", "-")
    if not raw:
        return None
    parts = raw.split("-", 1)
    base = parts[0].lower()
    base = {
        "deu": "de",
        "eng": "en",
        "fra": "fr",
        "ita": "it",
        "jpn": "ja",
        "kor": "ko",
        "por": "pt",
        "rus": "ru",
        "spa": "es",
        "zho": "zh",
    }.get(base, base)
    return base if len(parts) == 1 else f"{base}-{parts[1].upper()}"


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


class MetadataSearchProjection:
    """Project cached titles into the transactional catalog search index."""

    def __init__(self, db):
        self.db = db

    def project(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        payload: dict,
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
            "SELECT e.id,e.library_id FROM entity_provider_ids p JOIN library_entities e ON e.id=p.entity_id WHERE p.provider=? AND p.provider_id=? AND e.entity_type=?",
            (provider, provider_id, entity_type),
        )
        has_projection = "catalog_item_projection" in tables
        has_genres = "catalog_item_genres" in tables
        if has_projection:
            from app.catalog_read_model import normalize_search_text

        for entity_id, library_id in entities:
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
                    for field in (
                        "title",
                        "originalTitle",
                        "genres",
                        "tags",
                        "date",
                        "releaseDate",
                        "runtimeMinutes",
                        "communityRating",
                    ):
                        if field in payload:
                            merged[field] = payload[field]
                    payload_text = json.dumps(merged, ensure_ascii=False)
                    title_sort = normalize_search_text(merged.get("title") or "")
                    rating_sort = float(merged.get("communityRating") or 0)
                    release_sort = str(
                        merged.get("date") or merged.get("releaseDate") or ""
                    )
                    runtime_sort = float(merged.get("runtimeMinutes") or 0)
                    searchable = normalize_search_text(
                        f"{merged.get('title') or ''} {payload.get('originalTitle') or ''}"
                    )
                    grams = {
                        searchable[index : index + size]
                        for size in (1, 2)
                        for index in range(max(0, len(searchable) - size + 1))
                        if searchable[index : index + size]
                    }
                    gram_rows = [
                        (
                            gram,
                            entity_id,
                            locale,
                            library_id,
                            entity[0] if entity else None,
                        )
                        for gram in grams
                    ]
                    genres = merged.get("genres") or merged.get("tags") or []
                    genre_rows = [
                        (
                            entity_id,
                            locale,
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
                    if payload.get("title"):
                        cursor.execute(
                            "INSERT INTO catalog_search(entity_id,library_id,locale,title) VALUES(?,?,?,?)",
                            (entity_id, library_id, locale, str(payload["title"])),
                        )
                    if payload.get("originalTitle"):
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
                                str(payload["originalTitle"]),
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
                        cursor.executemany(
                            "INSERT OR IGNORE INTO catalog_search_grams(gram,entity_id,locale,library_id,parent_id) VALUES(?,?,?,?,?)",
                            gram_rows,
                        )
                        if has_genres:
                            cursor.execute(
                                "DELETE FROM catalog_item_genres WHERE entity_id=? AND locale=?",
                                (entity_id, locale),
                            )
                            cursor.executemany(
                                "INSERT OR IGNORE INTO catalog_item_genres(entity_id,locale,genre_key,genre_name) VALUES(?,?,?,?)",
                                genre_rows,
                            )
                break


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
            tuple[str, str, tuple[tuple[str, str], ...], str], dict
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
                "SELECT locale,payload FROM metadata_cache WHERE provider=? AND entity_type=? AND provider_id=? ORDER BY fetched_at DESC",
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
            requested, original, media=False, include_english="en" in configured
        )
        providers = self.providers(entity_type)
        result: dict = {}

        for key in TEXT_FIELDS:
            for tier in tiers:
                found = False
                for locale in locale_variants(tier, available):
                    for provider in providers:
                        value = payloads.get((provider, locale), {}).get(key)
                        if nonempty(value):
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
                        and nonempty(payloads[(provider, locale)].get(key))
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

        images = [
            image
            for provider in providers
            for (payload_provider, _), payload in payloads.items()
            if payload_provider == provider
            for image in payload.get("images", []) or []
            if isinstance(image, dict) and image.get("type") in ARTWORK_CATEGORY_SET
        ]
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
        cache_key = (entity_id, entity_type, identities, requested)
        cached = self._public_resolutions.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)
        raw = self.resolve_raw(entity_type, provider_ids, requested)
        original = raw.get("originalLanguage")
        providers = self.providers(entity_type)
        selected = {}
        for image_type in sorted(ARTWORK_CATEGORY_SET):
            choice = choose_artwork(
                raw.get("images", []), requested, image_type, original, providers
            )
            if choice:
                image = {
                    "url": f"/api/catalog/items/{entity_id}/images/{image_type}?language={requested}",
                    "language": choice.get("language"),
                    "width": choice.get("width") or 0,
                    "height": choice.get("height") or 0,
                }
                blur_hash = self._image_blur_hash(
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
            "SELECT blur_hash FROM metadata_images WHERE provider=? AND entity_type=? AND provider_id=? AND image_type=? AND image_url=? AND blur_hash IS NOT NULL ORDER BY fetched_at DESC LIMIT 1",
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
            requested, original, media=False, include_english="en" in configured
        )
        provider_rank = {value: index for index, value in enumerate(providers)}
        candidates = []
        available = {locale for _, locale in payloads}
        for (provider, locale), payload in payloads.items():
            if provider not in provider_rank:
                continue
            for trailer in payload.get("trailers", []) or []:
                if not isinstance(trailer, dict) or not trailer.get("url"):
                    continue
                trailer_language = trailer.get("language") or locale
                rank = 99
                for index, tier in enumerate(tiers):
                    for variant in locale_variants(
                        tier, available | {str(trailer_language)}
                    ):
                        if str(trailer_language).lower() == str(variant).lower():
                            rank = index * 2
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
        for _, _, trailer in sorted(
            (value for value in candidates if value[0] == best_rank),
            key=lambda value: (value[1], value[2].get("url", "")),
        ):
            if trailer not in result:
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
                provider, entity_type, provider_id, locales, force=force
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
    ) -> dict[str, dict]:
        locales = list(dict.fromkeys(locales or self.locales()))
        unsupported = [locale for locale in locales if locale not in self._locales]
        if unsupported:
            raise ValueError(
                f"Metadata language is not configured: {unsupported[0]}"
            )
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
        return {
            locale: self.ingest_document(
                provider, entity_type, provider_id, locale, values[locale]
            )
            for locale in locales
        }

    def ingest_locale(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        *,
        force: bool = False,
    ) -> dict:
        if locale not in self.locales():
            raise ValueError(f"Metadata language is not configured: {locale}")
        return self.ingest_locales(
            provider, entity_type, provider_id, [locale], force=force
        )[locale]

    def ingest_document(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        normalized: dict,
    ) -> dict:
        """Materialize a normalized document, including documents cached by aggregation."""
        if locale not in self.locales():
            raise ValueError(f"Metadata language is not configured: {locale}")
        cache = getattr(self.metadata_service, "cache", None)
        db = getattr(cache, "db", None)
        if db is not None:
            MetadataSearchProjection(db).project(
                provider, entity_type, provider_id, locale, normalized
            )
        if self.image_ingest is not None or self.credit_ingest is not None:

            def materialize_assets() -> None:
                # Cache hits also run this path so rows created before eager
                # asset ingestion are repaired without blocking metadata.
                if self.image_ingest is not None:
                    self.image_ingest.ingest(
                        provider, entity_type, provider_id, locale, normalized
                    )
                if self.credit_ingest is not None:
                    self.credit_ingest.ingest(
                        provider, entity_type, provider_id, locale, normalized
                    )

            if self.background_assets:
                digest = hashlib.sha256(
                    json.dumps(normalized, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
                asset_executor.submit(
                    (provider, entity_type, provider_id, locale, digest),
                    materialize_assets,
                )
            else:
                materialize_assets()
        return normalized


class MetadataImageIngestService:
    """Download every normalized artwork item during scan/refresh ingestion."""

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
            if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif"}
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
                    follow_redirects=True,
                    headers={
                        "Accept": "image/*",
                        "User-Agent": "ZenStream/metadata",
                    },
                    limits=httpx.Limits(
                        max_connections=16, max_keepalive_connections=8
                    ),
                )
            response = client.get(url, timeout=timeout)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            if content_type and not content_type.startswith("image/"):
                raise ValueError(
                    f"provider returned non-image content type {content_type}"
                )
            content = response.content
        if not content:
            raise ValueError("provider returned an empty image")
        if len(content) > self.MAX_IMAGE_BYTES:
            raise ValueError("provider image exceeds the 20 MiB limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.encoder(content, target, self._source_suffix(url))
        if not target.is_file() or not target.stat().st_size:
            raise RuntimeError("WebP encoder did not produce an image.")

    def ingest(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        document: dict,
    ) -> dict[str, int]:
        """Persist local files and image rows for all artwork in a document.

        A failed image does not fail the metadata document. The next scan or
        refresh retries it because only successfully downloaded files are
        recorded as ready.
        """
        if self.image_root is None:
            return {"ready": 0, "failed": 0, "skipped": 0}
        ready = failed = skipped = 0
        records = []
        images = document.get("images", []) if isinstance(document, dict) else []
        for image in images if isinstance(images, list) else []:
            if not isinstance(image, dict):
                continue
            image_type = image.get("type")
            url = image.get("url")
            if image_type not in ARTWORK_CATEGORY_SET or not isinstance(url, str):
                continue
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            target = self._target(url)
            if target is None:
                continue
            try:
                with self._file_lock(target):
                    if target.is_file() and target.stat().st_size > 0:
                        skipped += 1
                    else:
                        self._download(url, target)
                        ready += 1
                existing = (
                    self.db.execute(
                        "SELECT blur_hash FROM metadata_images WHERE image_url=? AND local_path=? AND blur_hash IS NOT NULL LIMIT 1",
                        (url, str(target)),
                    )
                    if hasattr(self.cache, "put_images")
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
                records.append(
                    (
                        provider,
                        entity_type,
                        provider_id,
                        image.get("language"),
                        image_type,
                        url,
                        blur_hash,
                        str(target),
                    )
                )
            except Exception as error:
                failed += 1
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
        if records:
            if hasattr(self.cache, "put_images"):
                self.cache.put_images(records)
            else:
                for record in records:
                    self.cache.put_image(*record)
        return {"ready": ready, "failed": failed, "skipped": skipped}


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

    def _portrait(self, person_id: str, image_url: object):
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
                current
                and current[0][0] == image_url
                and current[0][1] == str(target)
                and current[0][2]
                and target.is_file()
                and target.stat().st_size
            ):
                return None
            with self.images._file_lock(target):
                if not target.is_file() or not target.stat().st_size:
                    self.images._download(image_url, target)
            blur_hash = current[0][2] if current and current[0][0] == image_url else None
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

    def _person_id(
        self, cursor, provider: str, identity: str, name: str, locale: str
    ) -> str:
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
                        f"{entity_id}|{locale}|{credit_type}|{fallback_order}|{name}|{role or ''}".encode(
                            "utf-8"
                        )
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
        with self.db.transaction() as cursor:
            for entity_id in entity_ids:
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
            if (update := self._portrait(person_id, image_url)) is not None
        ]
        if updates:
            with self.db.transaction() as cursor:
                cursor.executemany(
                    "UPDATE people SET image_url=?,local_path=?,image_blur_hash=?,updated_at=? WHERE id=?",
                    updates,
                )
