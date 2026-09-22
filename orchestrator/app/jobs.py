from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Config
from app.foreground import active_requests
from app.intro_outro import IntroOutroDetector
from app.library import (
    PRIMARY_METADATA_IDENTITIES,
    CatalogWorkCoordinator,
    JobTerminated,
    LibraryScanner,
    LibraryStore,
)
from app.library import runtime as library_runtime
from app.library_cleanup import cleanup_orphans
from app.logging_config import get_logger
from app.metadata_domain import choose_artwork, language_family
from app.metadata_refresh import MetadataRefreshJob
from app.metadata_services import (
    FACT_FIELDS,
    MUSICBRAINZ_NEUTRAL_ENTITY_TYPES,
    TEXT_FIELDS,
    MetadataIngestService,
    metadata_task_results,
    repair_music_track_contexts,
)
from app.models.metadata import MetadataLanguageSettings
from app.progress import (
    WholeJobProgress,
    format_progress_message,
    resolve_progress_item,
)
from app.providers import ProviderError, ProviderNotFoundError
from app.trickplay import TrickplayExtractor
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

logger = get_logger("jobs")
VIDEO_ENTITY_TYPES = {"movie", "series", "season", "episode"}
ARTWORK_TYPES = {"Primary", "Backdrop", "Logo", "Banner"}
ANALYSIS_KINDS = {"trickplay_extract", "intro_outro_detect"}
METADATA_MISSING_MAX_ATTEMPTS = 5
METADATA_MISSING_RETRY_BASE_SECONDS = 60 * 60
METADATA_MISSING_RETRY_MAX_SECONDS = 7 * 24 * 60 * 60
METADATA_IDENTITY_PROVIDER = "__identity__"
METADATA_JOB_KINDS = {"metadata_missing", "metadata_upgrade", "metadata_refresh"}
# Catalog-mutating metadata jobs need the same exclusive window as orphan
# cleanup so their read-model snapshots cannot overlap inventory admission.
CATALOG_EXCLUSIVE_KINDS = METADATA_JOB_KINDS | {"metadata_cleanup"}
JOB_DISPATCH_BACKOFF_INITIAL = 0.25
JOB_DISPATCH_BACKOFF_MAX = 5.0
METADATA_UPGRADE_VERSION = 1
METADATA_UPGRADE_STATE_COLUMNS = {
    "provider",
    "entity_type",
    "provider_id",
    "locale",
    "upgrade_version",
    "document_digest",
    "completed_at",
}


class AnalysisMaintenanceTimeout(TimeoutError):
    """An analysis worker did not acknowledge cleanup termination in time."""


def _english_configured() -> bool:
    return any(
        language_family(value) == "en" for value in MetadataLanguageSettings().get()
    )


def _prefer_no_language_for_backdrop() -> bool:
    return MetadataLanguageSettings().prefer_no_language_for_backdrop()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def _local_next(time_text: str, weekday: int | None = None) -> str:
    """Return the next server-local calendar occurrence as a UTC ISO instant."""
    try:
        hour, minute = (int(part) for part in time_text.split(":", 1))
    except (TypeError, ValueError):
        raise ValueError("time must be HH:mm")
    if hour not in range(24) or minute not in range(60):
        raise ValueError("time must be HH:mm")
    local_now = datetime.now().astimezone()
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is not None:
        if weekday not in range(7):
            raise ValueError("weekday must be between 0 and 6")
        candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
        if candidate <= local_now:
            candidate += timedelta(days=7)
    elif candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).isoformat()


def _usable_metadata_value(value) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _metadata_retry_at(attempts: int) -> str:
    delay = min(
        METADATA_MISSING_RETRY_MAX_SECONDS,
        METADATA_MISSING_RETRY_BASE_SECONDS * (2 ** max(0, min(attempts - 1, 16))),
    )
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


def _metadata_recovery_state_table(db) -> bool:
    try:
        return bool(
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_missing_state'"
            )
        )
    except Exception:
        return False


def _metadata_recovery_state(
    db, provider: str, entity_type: str, provider_id: str, locale: str = ""
):
    if not _metadata_recovery_state_table(db):
        return None
    rows = db.execute(
        "SELECT state,attempts,next_attempt_at FROM metadata_missing_state "
        "WHERE provider=? AND entity_type=? AND provider_id=? AND locale=?",
        (provider, entity_type, provider_id, locale),
    )
    return rows[0] if rows else None


def _metadata_recovery_due(state, current: str | None = None) -> bool:
    if not state:
        return True
    status, attempts, next_attempt_at = state
    if (
        status not in {"queued", "retry"}
        or int(attempts or 0) >= METADATA_MISSING_MAX_ATTEMPTS
    ):
        return False
    return next_attempt_at is None or str(next_attempt_at) <= (current or now())


def _record_metadata_recovery_state(
    db,
    provider: str,
    entity_type: str,
    provider_id: str,
    *,
    locale: str = "",
    error: str | None,
    source_job_id: str | None,
    permanent: bool = False,
) -> None:
    if not _metadata_recovery_state_table(db):
        return
    previous = _metadata_recovery_state(db, provider, entity_type, provider_id, locale)
    attempts = int(previous[1] or 0) + 1 if previous else 1
    terminal = permanent or attempts >= METADATA_MISSING_MAX_ATTEMPTS
    status = "failed" if terminal else "retry"
    next_attempt_at = None if terminal else _metadata_retry_at(attempts)
    timestamp = now()
    with db.transaction() as cursor:
        cursor.execute(
            "INSERT INTO metadata_missing_state(provider,entity_type,provider_id,locale,state,attempts,next_attempt_at,source_job_id,error,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(provider,entity_type,provider_id,locale) DO UPDATE SET "
            "state=excluded.state,attempts=excluded.attempts,next_attempt_at=excluded.next_attempt_at,source_job_id=excluded.source_job_id,error=excluded.error,updated_at=excluded.updated_at",
            (
                provider,
                entity_type,
                provider_id,
                locale,
                status,
                attempts,
                next_attempt_at,
                source_job_id,
                error,
                timestamp,
                timestamp,
            ),
        )


def _complete_metadata_recovery_state(
    db,
    provider: str,
    entity_type: str,
    provider_id: str,
    *,
    locale: str = "",
    source_job_id: str | None,
) -> None:
    if not _metadata_recovery_state_table(db):
        return
    previous = _metadata_recovery_state(db, provider, entity_type, provider_id, locale)
    attempts = int(previous[1] or 0) if previous else 0
    timestamp = now()
    with db.transaction() as cursor:
        cursor.execute(
            "INSERT INTO metadata_missing_state(provider,entity_type,provider_id,locale,state,attempts,next_attempt_at,source_job_id,error,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,NULL,?,NULL,?,?) "
            "ON CONFLICT(provider,entity_type,provider_id,locale) DO UPDATE SET "
            "state='completed',attempts=excluded.attempts,next_attempt_at=NULL,source_job_id=excluded.source_job_id,error=NULL,updated_at=excluded.updated_at",
            (
                provider,
                entity_type,
                provider_id,
                locale,
                "completed",
                attempts,
                source_job_id,
                timestamp,
                timestamp,
            ),
        )


def _clear_metadata_recovery_state(
    db, provider: str, entity_type: str, provider_id: str, locale: str = ""
) -> None:
    if not _metadata_recovery_state_table(db):
        return
    db.execute(
        "DELETE FROM metadata_missing_state WHERE provider=? AND entity_type=? AND provider_id=? AND locale=?",
        (provider, entity_type, provider_id, locale),
    )


def _metadata_failure_is_retryable(failure: dict) -> bool:
    if failure.get("retryable") is False or failure.get("permanent"):
        return False
    missing = set(failure.get("missing") or ())
    # TVDB legitimately has season records with no localized title.  The
    # scanner's synthesized ``Season N`` label is valid local metadata, so a
    # missing provider title is terminal after the one explicit recovery pass.
    if (
        failure.get("kind") == "incomplete"
        and failure.get("provider") == "tvdb"
        and failure.get("entityType") == "season"
        and missing
        and missing <= {"metadata:title"}
    ):
        return False
    return True


def _ready_cache_path(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        path = Path(value)
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _metadata_upgrade_digest(document: dict | None) -> str | None:
    """Hash provider data without cache-only bookkeeping fields."""
    if not isinstance(document, dict):
        return None
    comparable = {
        key: value for key, value in document.items() if not str(key).startswith("_")
    }
    return hashlib.sha256(
        json.dumps(
            comparable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _metadata_upgrade_state_columns(db) -> set[str]:
    try:
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='metadata_upgrade_state'"
        )
        if not tables:
            return set()
        return {
            row[1] for row in db.execute("PRAGMA table_info(metadata_upgrade_state)")
        }
    except Exception:
        return set()


def _load_metadata_upgrade_state(
    db,
) -> dict[tuple[str, str, str, str], tuple[int, str]]:
    if not METADATA_UPGRADE_STATE_COLUMNS <= _metadata_upgrade_state_columns(db):
        return {}
    try:
        rows = db.execute(
            "SELECT provider,entity_type,provider_id,locale,upgrade_version,document_digest "
            "FROM metadata_upgrade_state"
        )
    except Exception:
        return {}
    state = {}
    for provider, entity_type, provider_id, locale, version, digest in rows:
        if not digest:
            continue
        try:
            state[(str(provider), str(entity_type), str(provider_id), str(locale))] = (
                int(version),
                str(digest),
            )
        except (TypeError, ValueError):
            continue
    return state


def _persist_metadata_upgrade_state(db, updates) -> int:
    """Persist completed upgrade markers in one bounded transaction."""
    updates = list(dict.fromkeys(updates))
    if (
        not updates
        or not METADATA_UPGRADE_STATE_COLUMNS <= _metadata_upgrade_state_columns(db)
    ):
        return 0
    completed_at = now()
    with db.transaction() as cursor:
        cursor.executemany(
            "INSERT INTO metadata_upgrade_state(provider,entity_type,provider_id,locale,upgrade_version,document_digest,completed_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(provider,entity_type,provider_id,locale) DO UPDATE SET "
            "upgrade_version=excluded.upgrade_version,document_digest=excluded.document_digest,completed_at=excluded.completed_at",
            [
                (
                    provider,
                    entity_type,
                    str(provider_id),
                    locale,
                    METADATA_UPGRADE_VERSION,
                    digest,
                    completed_at,
                )
                for provider, entity_type, provider_id, locale, digest in updates
            ],
        )
    return len(updates)


def _metadata_upgrade_needed(
    before: dict, fresh: dict, locale: str, provider: str
) -> bool:
    """Return whether fresh non-empty provider data can improve existing data."""
    if not isinstance(before, dict) or not isinstance(fresh, dict):
        return False
    if (
        provider == "lastfm"
        and _usable_metadata_value(fresh.get("providers"))
        and before.get("providers") != fresh.get("providers")
    ):
        return True
    for field in TEXT_FIELDS | FACT_FIELDS:
        previous = before.get(field)
        current = fresh.get(field)
        if (
            _usable_metadata_value(previous)
            and _usable_metadata_value(current)
            and previous != current
        ):
            return True

    prefer_no_language_for_backdrop = _prefer_no_language_for_backdrop()
    for image_type in ARTWORK_TYPES:
        previous = choose_artwork(
            before.get("images", []),
            locale,
            image_type,
            before.get("originalLanguage"),
            [provider],
            include_english=_english_configured(),
            prefer_no_language_for_backdrop=prefer_no_language_for_backdrop,
        )
        current = choose_artwork(
            fresh.get("images", []),
            locale,
            image_type,
            fresh.get("originalLanguage"),
            [provider],
            include_english=_english_configured(),
            prefer_no_language_for_backdrop=prefer_no_language_for_backdrop,
        )
        if (
            previous
            and current
            and _usable_metadata_value(previous.get("url"))
            and _usable_metadata_value(current.get("url"))
            and previous.get("url") != current.get("url")
        ):
            return True

    previous_credits = before.get("credits")
    current_credits = fresh.get("credits")
    if isinstance(previous_credits, dict) and isinstance(current_credits, dict):
        for credit_type in ("cast", "crew"):
            previous_values = previous_credits.get(credit_type)
            current_values = current_credits.get(credit_type)
            if (
                isinstance(previous_values, list)
                and previous_values
                and isinstance(current_values, list)
                and current_values
                and previous_values != current_values
            ):
                return True
    return False


def _fetch_upgrade_documents(
    ingest: MetadataIngestService,
    provider: str,
    entity_type: str,
    provider_id: str,
    locales: list[str],
) -> dict[str, dict]:
    """Fetch and cache fresh documents without projecting unchanged upgrades."""
    service = ingest.metadata_service
    neutral = (
        provider == "musicbrainz" and entity_type in MUSICBRAINZ_NEUTRAL_ENTITY_TYPES
    )
    fetch_method = getattr(service, "fetch_locales", None)
    provider_locales = [""] if neutral else locales
    if fetch_method is not None:
        fetch_kwargs = {
            "force": True,
            "project": False,
            "batch_cache_writes": True,
        }
        while True:
            try:
                values = fetch_method(
                    provider,
                    entity_type,
                    provider_id,
                    provider_locales if neutral else locales,
                    **fetch_kwargs,
                )
                break
            except TypeError as error:
                message = str(error)
                unsupported = next(
                    (
                        key
                        for key in ("batch_cache_writes", "project")
                        if key in message
                    ),
                    None,
                )
                if unsupported is None:
                    raise
                fetch_kwargs.pop(unsupported)

    else:
        values = {
            locale: service.fetch(
                provider,
                entity_type,
                provider_id,
                locale,
                force=True,
            )
            for locale in (provider_locales if neutral else locales)
        }
    if neutral:
        normalized = values.get("") or next(iter(values.values()), None)
        if not isinstance(normalized, dict):
            return {}
        return {
            locale: dict(normalized)
            for locale in (locales if locales != [""] else ingest.locales())
        }
    return values


def _metadata_catalog_entity_type(provider: str, identifier_type: str) -> str:
    if provider == "musicbrainz":
        return {"recording": "track"}.get(identifier_type, identifier_type)
    return identifier_type


def _metadata_identity_type(provider: str, entity_type: str) -> str:
    if provider == "musicbrainz" and entity_type == "track":
        return "recording"
    return entity_type


def _metadata_document_gaps(
    db,
    provider: str,
    entity_type: str,
    provider_id: str,
    locale: str,
    document: dict,
) -> tuple[set[str], list[tuple[str, str]]]:
    gaps: set[str] = set()
    linked = db.execute(
        "SELECT ep.entity_id,e.library_id,ep.is_primary FROM entity_provider_ids ep "
        "JOIN library_entities e ON e.id=ep.entity_id "
        "WHERE ep.provider=? AND ep.identifier_type=? AND ep.provider_id=?",
        (provider, _metadata_identity_type(provider, entity_type), provider_id),
    )
    entity_libraries = [(row[0], row[1]) for row in linked]
    if not entity_libraries:
        return {"identity:orphaned"}, []

    # Provider identity fields describe the source document, not the selected
    # catalog projection.  A local document or a higher-priority provider is
    # allowed to own those fields, so comparing them here creates a permanent
    # false gap for otherwise valid music and video records.
    projected_fields = (TEXT_FIELDS | FACT_FIELDS) - {
        "provider",
        "providerId",
        "ids",
    }
    prefer_no_language_for_backdrop = _prefer_no_language_for_backdrop()
    source_images = set()
    for image_type in ARTWORK_TYPES:
        expected = choose_artwork(
            document.get("images", []),
            locale,
            image_type,
            document.get("originalLanguage"),
            [provider],
            include_english=_english_configured(),
            prefer_no_language_for_backdrop=prefer_no_language_for_backdrop,
        )
        if expected and expected.get("url"):
            source_images.add((image_type, str(expected["url"])))
    image_columns = {row[1] for row in db.execute("PRAGMA table_info(metadata_images)")}
    blur_hash_column = ",blur_hash" if "blur_hash" in image_columns else ""
    image_rows = db.execute(
        "SELECT image_type,image_url,local_path" + blur_hash_column + " "
        "FROM metadata_images WHERE provider=? AND entity_type=? AND provider_id=?",
        (provider, entity_type, provider_id),
    )
    ready_images = {
        (str(image_type), str(image_url))
        for image_type, image_url, local_path, *_rest in image_rows
        if _ready_cache_path(local_path)
    }
    ready_hashes = {
        (str(row[0]), str(row[1])): str(row[3]).strip()
        for row in image_rows
        if len(row) > 3 and _ready_cache_path(row[2]) and row[3]
    }
    for image_type, image_url in source_images - ready_images:
        gaps.add(f"artwork:{image_type}")

    expected_credit_records = []
    credits = document.get("credits")
    if isinstance(credits, dict):
        for credit_type in ("cast", "crew"):
            values = credits.get(credit_type)
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, dict) and str(value.get("name") or "").strip():
                    expected_credit_records.append((credit_type, value))

    for entity_id, _library_id, is_primary in linked:
        if not is_primary:
            continue
        projection_rows = db.execute(
            "SELECT payload FROM catalog_item_projection WHERE entity_id=? AND locale=?",
            (entity_id, locale),
        )
        projection = {}
        if projection_rows:
            try:
                projection = json.loads(projection_rows[0][0] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                projection = {}
        if not isinstance(projection, dict):
            projection = {}
        if not projection_rows:
            gaps.add("projection")
        # A season can have its provider identity and episode children while
        # the season details request was interrupted (or returned a partial
        # response).  Do not treat the synthesized catalog label ("Season N")
        # as a provider title: the missing-metadata job must retry the
        # provider document so a localized season name can be materialized.
        if (
            provider == "tvdb"
            and entity_type == "season"
            and not _usable_metadata_value(document.get("title"))
        ):
            gaps.add("metadata:title")
        if provider == "lastfm":
            source_namespace = (document.get("providers") or {}).get("lastfm")
            projected_namespaces = projection.get("providers") or {}
            if (
                isinstance(source_namespace, dict)
                and source_namespace
                and projected_namespaces.get("lastfm") != source_namespace
            ):
                gaps.add("metadata:providers")
        for field in projected_fields:
            source_value = document.get(field)
            if not _usable_metadata_value(source_value):
                continue
            if provider == "lastfm":
                if field in {"ids", "provider", "providerId"}:
                    continue
                if field == "tags":
                    projected_tags = projection.get("tags")
                    projected_tags = (
                        projected_tags if isinstance(projected_tags, list) else []
                    )
                    projected_keys = {
                        str(value.get("name") if isinstance(value, dict) else value)
                        .strip()
                        .casefold()
                        for value in projected_tags
                    }
                    if any(
                        str(value).strip().casefold() not in projected_keys
                        for value in source_value
                        if str(value).strip()
                    ):
                        gaps.add("metadata:tags")
                elif not _usable_metadata_value(projection.get(field)):
                    gaps.add(f"metadata:{field}")
                continue
            if field == "trailers":
                # Trailer projection is deliberately localized and merged
                # across provider priorities.  A different provider's
                # trailer is a valid materialization; only an absent
                # projection is actionable.
                if not _usable_metadata_value(projection.get(field)):
                    gaps.add("metadata:trailers")
                continue
            if projection.get(field) != source_value:
                gaps.add(f"metadata:{field}")
        projected_images = projection.get("images")
        if not isinstance(projected_images, dict):
            projected_images = {}
        for image_type in ARTWORK_TYPES:
            expected = choose_artwork(
                document.get("images", []),
                locale,
                image_type,
                document.get("originalLanguage"),
                [provider],
                include_english=_english_configured(),
                prefer_no_language_for_backdrop=prefer_no_language_for_backdrop,
            )
            if expected and image_type not in projected_images:
                gaps.add(f"projection-artwork:{image_type}")
            elif (
                expected
                and image_type != "Logo"
                and image_type in projected_images
                and ready_hashes.get((image_type, str(expected.get("url"))))
                and not str(
                    (
                        projected_images.get(image_type)
                        if isinstance(projected_images.get(image_type), dict)
                        else {}
                    ).get("blurHash")
                    or ""
                ).strip()
            ):
                gaps.add(f"projection-artwork-blurhash:{image_type}")

        if (
            is_primary
            and provider in {"tmdb", "tvdb"}
            and entity_type in VIDEO_ENTITY_TYPES
            and expected_credit_records
        ):
            actual_credit_count = db.execute(
                "SELECT COUNT(*) FROM entity_person_credits WHERE entity_id=? AND provider=? AND locale=?",
                (entity_id, provider, locale),
            )[0][0]
            if int(actual_credit_count) != len(expected_credit_records):
                gaps.add("credits")

    if expected_credit_records and any(bool(row[2]) for row in linked):
        expected_portraits = {
            str(record.get("id")): record.get("imageUrl")
            for _credit_type, record in expected_credit_records
            if str(record.get("id") or "").strip()
            and isinstance(record.get("imageUrl"), str)
            and record.get("imageUrl")
        }
        people_by_id = {}
        person_ids = sorted(expected_portraits)
        for offset in range(0, len(person_ids), 400):
            batch = person_ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            people_by_id.update(
                {
                    person_id: (image_url, local_path)
                    for person_id, image_url, local_path in db.execute(
                        f"SELECT provider_person_id,image_url,local_path FROM people "
                        f"WHERE provider=? AND provider_person_id IN ({placeholders})",
                        (provider, *batch),
                    )
                }
            )
        for person_id, image_url in expected_portraits.items():
            person = people_by_id.get(person_id)
            if (
                person is None
                or person[0] != image_url
                or not _ready_cache_path(person[1])
            ):
                gaps.add("portrait")
                break
    return gaps, entity_libraries


def _repair_missing_tv_child_identities(
    db,
    metadata_service,
    *,
    run_id: str | None = None,
    should_terminate=None,
    persist_state: bool = True,
) -> int:
    """Restore child provider IDs left behind by an interrupted TV scan.

    A scan can persist the series identity before the process is restarted,
    while the season/episode identity pass is still pending.  The missing
    metadata job must repair that durable gap before selecting provider
    documents; otherwise those children are invisible to the job forever.
    """
    entity_columns = {
        row[1] for row in db.execute("PRAGMA table_info(library_entities)")
    }
    if not {"parent_id", "season_number", "episode_number"} <= entity_columns:
        return 0

    child_identity_rows = db.execute(
        "SELECT child.id,child.entity_type,child.season_number,child.episode_number,"
        "CASE WHEN child.entity_type='season' THEN child.parent_id "
        "ELSE season.parent_id END "
        "FROM library_entities child "
        "LEFT JOIN library_entities season ON season.id=child.parent_id "
        "WHERE child.entity_type='season' AND child.parent_id IN "
        "(SELECT id FROM library_entities WHERE entity_type='series') "
        "OR child.entity_type='episode' AND season.entity_type='season' "
        "AND season.parent_id IN "
        "(SELECT id FROM library_entities WHERE entity_type='series') "
        "ORDER BY child.parent_id,child.entity_type,child.season_number,child.episode_number"
    )
    if not child_identity_rows:
        return 0

    series_rows = db.execute(
        "SELECT e.id,p.provider,p.provider_id "
        "FROM library_entities e JOIN entity_provider_ids p ON p.entity_id=e.id "
        "WHERE e.entity_type='series' AND p.identifier_type='series' "
        "AND p.provider IN ('tmdb','tvdb') ORDER BY e.id,p.provider"
    )
    children_by_series: dict[str, list[tuple]] = {}
    for row in child_identity_rows:
        child_id, entity_type, season_number, episode_number, series_id = row
        children_by_series.setdefault(series_id, []).append(
            (child_id, entity_type, season_number, episode_number)
        )

    existing = set(
        db.execute(
            "SELECT entity_id,provider,identifier_type FROM entity_provider_ids "
            "WHERE provider IN ('tmdb','tvdb')"
        )
    )
    should_terminate = should_terminate or (lambda: False)
    state_available = persist_state and _metadata_recovery_state_table(db)
    source_job_id = run_id or "metadata_missing"
    repaired = 0
    for series_id, provider, series_provider_id in series_rows:
        if should_terminate():
            raise JobTerminated()
        children = children_by_series.get(series_id, [])
        if not children:
            continue
        missing = [
            child
            for child in children
            if (child[0], provider, child[1]) not in existing
        ]
        if not missing:
            if provider == "tvdb" and state_available:
                _clear_metadata_recovery_state(
                    db, "tvdb", "series_children", str(series_provider_id)
                )
            continue

        provider_ids: dict[tuple[int, int | None], str] = {}
        if provider == "tmdb":
            for _child_id, entity_type, season_number, episode_number in missing:
                if season_number is None:
                    continue
                key = (int(season_number), None)
                provider_ids[key] = f"{series_provider_id}:{season_number}"
                if entity_type == "episode" and episode_number is not None:
                    provider_ids[(int(season_number), int(episode_number))] = (
                        f"{series_provider_id}:{season_number}:{episode_number}"
                    )
        else:
            discover = getattr(metadata_service, "series_child_ids", None)
            if not callable(discover):
                continue
            if state_available and not _metadata_recovery_due(
                _metadata_recovery_state(
                    db, "tvdb", "series_children", str(series_provider_id)
                )
            ):
                continue
            try:
                hierarchy = discover("tvdb", str(series_provider_id)) or {}
            except ProviderNotFoundError as error:
                _record_metadata_recovery_state(
                    db,
                    "tvdb",
                    "series_children",
                    str(series_provider_id),
                    error=f"{type(error).__name__}: {error}",
                    source_job_id=source_job_id,
                    permanent=True,
                )
                logger.info(
                    "missing metadata child identity is permanently unresolved series_id=%s provider_id=%s",
                    series_id,
                    series_provider_id,
                )
                continue
            except Exception as error:
                _record_metadata_recovery_state(
                    db,
                    "tvdb",
                    "series_children",
                    str(series_provider_id),
                    error=f"{type(error).__name__}: {error}",
                    source_job_id=source_job_id,
                )
                logger.warning(
                    "missing metadata child identity discovery failed series_id=%s provider_id=%s: %s",
                    series_id,
                    series_provider_id,
                    error,
                )
                continue
            for value in hierarchy.get("seasons", []) or []:
                if value.get("seasonNumber") is not None and value.get("providerId"):
                    provider_ids[(int(value["seasonNumber"]), None)] = str(
                        value["providerId"]
                    )
            for value in hierarchy.get("episodes", []) or []:
                if (
                    value.get("seasonNumber") is not None
                    and value.get("episodeNumber") is not None
                    and value.get("providerId")
                ):
                    provider_ids[
                        (int(value["seasonNumber"]), int(value["episodeNumber"]))
                    ] = str(value["providerId"])

        for child_id, entity_type, season_number, episode_number in missing:
            if season_number is None:
                continue
            key = (
                (int(season_number), int(episode_number))
                if entity_type == "episode" and episode_number is not None
                else (int(season_number), None)
            )
            provider_id = provider_ids.get(key)
            if not provider_id:
                continue
            db.execute(
                "INSERT OR IGNORE INTO entity_provider_ids "
                "(entity_id,provider,identifier_type,provider_id,is_primary) "
                "VALUES(?,?,?,?,?)",
                (child_id, provider, entity_type, provider_id, int(provider == "tvdb")),
            )
            existing.add((child_id, provider, entity_type))
            repaired += 1
            if "match_status" in entity_columns:
                db.execute(
                    "UPDATE library_entities SET match_status='matched',"
                    "match_confidence=1.0,match_method='parent_resolution',updated_at=? "
                    "WHERE id=?",
                    (now(), child_id),
                )
        if provider == "tvdb" and state_available:
            remaining = [
                child
                for child in missing
                if (
                    child[0],
                    "tvdb",
                    child[1],
                )
                not in existing
            ]
            if remaining:
                _record_metadata_recovery_state(
                    db,
                    "tvdb",
                    "series_children",
                    str(series_provider_id),
                    error=(
                        "Provider hierarchy did not contain all indexed TV child identities"
                    ),
                    source_job_id=source_job_id,
                )
            else:
                _clear_metadata_recovery_state(
                    db, "tvdb", "series_children", str(series_provider_id)
                )
    if repaired:
        logger.info("repaired missing TV child provider identities count=%s", repaired)
    return repaired


class JobStore:
    def __init__(self):
        self.db = Config().database
        self._progress: dict[str, WholeJobProgress] = {}

    def begin_progress(self, run_id: str, kind: str) -> None:
        if not hasattr(self, "_progress"):
            self._progress = {}
        self._progress[run_id] = WholeJobProgress(kind)

    def end_progress(self, run_id: str) -> None:
        getattr(self, "_progress", {}).pop(run_id, None)

    def _progress_columns(self, table: str) -> list[str]:
        try:
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        except Exception:
            columns = set()
        return [
            f"{name}" if name in columns else f"NULL AS {name}"
            for name in (
                "progress_phase",
                "progress_label",
                "progress_stage_current",
                "progress_stage_total",
                "progress_stage_unit",
                "progress_current_item",
            )
        ]

    @staticmethod
    def _progress_detail(row: tuple, offset: int) -> dict | None:
        values = row[offset : offset + 6]
        if not any(value is not None for value in values):
            return None
        value = {
            "phase": values[0] or "processing",
            "label": values[1] or "Working",
            "current": values[2],
            "total": values[3],
            "unit": values[4],
            "item": values[5],
        }
        return value

    @staticmethod
    def _scan_stats(value) -> dict | None:
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _definition(row) -> dict:
        # Keep the mapper tolerant of pre-trigger test databases while the
        # production schema is migrated to trigger-owned scheduling.
        if len(row) >= 15:
            interval_minutes, enabled, config, next_run, last_run = row[5:10]
            offset = 0
        else:
            interval_minutes, enabled = None, None
            config, next_run, last_run = row[5:8]
            offset = -2
        try:
            config = json.loads(config or "{}")
        except json.JSONDecodeError:
            config = {}
        return {
            "id": row[0],
            "key": row[1],
            "name": row[2],
            "description": row[3],
            "kind": row[4],
            "intervalMinutes": interval_minutes,
            "enabled": True if enabled is None else bool(enabled),
            "config": config,
            "nextRunAt": next_run,
            "lastRunAt": last_run,
            "lastRunId": row[10 + offset],
            "lastState": row[11 + offset],
            "lastMessage": row[12 + offset],
            "createdAt": row[13 + offset],
            "updatedAt": row[14 + offset],
        }

    def _definition_select(self) -> str:
        columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(job_definitions)")
        }
        legacy = {"interval_minutes", "enabled"}.issubset(columns)
        if legacy:
            return "id,job_key,name,description,kind,interval_minutes,enabled,config,next_run_at,last_run_at,last_run_id,last_state,last_message,created_at,updated_at"
        return "id,job_key,name,description,kind,config,next_run_at,last_run_at,last_run_id,last_state,last_message,created_at,updated_at"

    def _definition_columns(self, executor=None) -> set[str]:
        executor = executor or self.db
        try:
            return {
                row[1] for row in executor.execute("PRAGMA table_info(job_definitions)")
            }
        except Exception:
            return set()

    def _mark_definition_queued(
        self, definition_id: str, run_id: str, timestamp: str, executor=None
    ) -> None:
        """Keep the definition pointer aligned with a newly queued run."""
        executor = executor or self.db
        columns = self._definition_columns(executor)
        values = {
            "last_state": "queued",
            "last_message": "Queued",
            "updated_at": timestamp,
        }
        if "last_run_id" in columns:
            values["last_run_id"] = run_id
        if "last_run_at" in columns:
            values["last_run_at"] = timestamp
        fields = [key for key in values if key in columns]
        if not fields:
            return
        executor.execute(
            "UPDATE job_definitions SET "
            + ",".join(f"{key}=?" for key in fields)
            + " WHERE id=?",
            [values[key] for key in fields] + [definition_id],
        )

    def _update_definition_from_run(self, row: tuple) -> None:
        (
            definition_id,
            state,
            message,
            error,
            created_at,
            started_at,
            _finished_at,
            run_id,
        ) = row
        columns = self._definition_columns()
        values = {
            "last_state": state,
            "last_message": message or error,
            "updated_at": now(),
        }
        if "last_run_id" in columns:
            values["last_run_id"] = run_id
        if "last_run_at" in columns:
            # last_run_at is the run's admission/start instant, matching the
            # trigger-owned timestamp semantics; terminal duration is exposed
            # by the run's finished_at field instead.
            values["last_run_at"] = started_at or created_at
        fields = [key for key in values if key in columns]
        if not fields:
            return
        self.db.execute(
            "UPDATE job_definitions SET "
            + ",".join(f"{key}=?" for key in fields)
            + " WHERE id=?",
            [values[key] for key in fields] + [definition_id],
        )

    def _sync_definition_from_run(self, run_id: str) -> None:
        rows = self.db.execute(
            "SELECT definition_id,state,message,error,created_at,started_at,finished_at,id "
            "FROM job_runs WHERE id=?",
            (run_id,),
        )
        if rows:
            self._update_definition_from_run(rows[0])

    def _with_triggers(self, definition: dict) -> dict:
        try:
            rows = self.db.execute(
                "SELECT id,trigger_type,interval_seconds,time_of_day,weekday,next_run_at,options "
                "FROM job_schedule_triggers WHERE definition_id=? ORDER BY created_at",
                (definition["id"],),
            )
        except Exception:
            try:
                rows = self.db.execute(
                    "SELECT id,trigger_type,interval_seconds,time_of_day,weekday,next_run_at,NULL FROM job_schedule_triggers WHERE definition_id=? ORDER BY created_at",
                    (definition["id"],),
                )
            except Exception:
                rows = []
        triggers = []
        for row in rows:
            item = {"id": row[0], "type": row[1]}
            if row[1] == "interval":
                item["intervalSeconds"] = row[2]
            elif row[1] == "daily":
                item["time"] = row[3]
            elif row[1] == "weekly":
                item["weekday"] = row[4]
                item["time"] = row[3]
            item["nextRunAt"] = row[5]
            try:
                item["options"] = json.loads(row[6] or "{}")
            except (IndexError, TypeError, json.JSONDecodeError):
                item["options"] = {}
            triggers.append(item)
        definition["triggers"] = triggers
        definition["optionDefinitions"] = self.option_definitions(definition["kind"])
        return definition

    @staticmethod
    def option_definitions(kind: str) -> list[dict]:
        if kind == "metadata_refresh":
            return [
                {
                    "key": "refreshAll",
                    "label": "Refresh all indexed media metadata",
                    "type": "boolean",
                    "default": False,
                    "manualOnly": True,
                    "description": "Ignore sparse rules and refresh every indexed movie, series, season, episode, album, and track.",
                },
                {
                    "key": "preserveCachedAssets",
                    "label": "Preserve cached assets",
                    "type": "boolean",
                    "default": False,
                    "description": "Reuse valid cached artwork and portraits instead of forcing a refresh.",
                },
            ]
        return []

    @classmethod
    def validate_options(
        cls, kind: str, options: dict | None, *, allow_manual: bool = True
    ) -> dict:
        values = options or {}
        if not isinstance(values, dict):
            raise ValueError("options must be an object")
        definitions = {item["key"]: item for item in cls.option_definitions(kind)}
        if not allow_manual:
            manual = {
                item["key"] for item in definitions.values() if item.get("manualOnly")
            }
            supplied_manual = set(values).intersection(manual)
            if supplied_manual:
                raise ValueError(
                    f"{sorted(supplied_manual)[0]} is only available for manual runs"
                )
        unknown = set(values) - set(definitions)
        if unknown:
            raise ValueError(f"Unsupported task option: {sorted(unknown)[0]}")
        result = {}
        for key, definition in definitions.items():
            value = values.get(key, definition.get("default"))
            if definition["type"] == "boolean" and not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
            result[key] = value
        return result

    @staticmethod
    def _run(row) -> dict:
        value = {
            "id": row[0],
            "definitionId": row[1],
            "libraryId": row[2],
            "kind": row[3],
            "state": row[4],
            "progressCurrent": row[5],
            "progressTotal": row[6],
            "message": row[7],
            "error": row[8],
            "errorDetails": row[9],
            "createdAt": row[10],
            "startedAt": row[11],
            "finishedAt": row[12],
            "threadName": row[13],
        }
        value["progressDetail"] = JobStore._progress_detail(row, 14)
        if len(row) >= 23:
            value["scanStats"] = JobStore._scan_stats(row[20])
            option_offset = 21
        else:
            value["scanStats"] = None
            option_offset = 20
        if len(row) > option_offset:
            value["sourceTriggerId"] = row[option_offset]
            if len(row) > option_offset + 1:
                try:
                    value["options"] = json.loads(row[option_offset + 1] or "{}")
                except (TypeError, json.JSONDecodeError):
                    value["options"] = {}
        return value

    def definitions(self) -> list[dict]:
        rows = self.db.execute(
            f"SELECT {self._definition_select()} FROM job_definitions ORDER BY name COLLATE NOCASE"
        )
        return [self._with_triggers(self._definition(row)) for row in rows]

    def definition(self, definition_id: str) -> dict | None:
        rows = self.db.execute(
            f"SELECT {self._definition_select()} FROM job_definitions WHERE id=?",
            (definition_id,),
        )
        return self._with_triggers(self._definition(rows[0])) if rows else None

    def by_key(self, key: str) -> dict | None:
        rows = self.db.execute(
            f"SELECT {self._definition_select()} FROM job_definitions WHERE job_key=?",
            (key,),
        )
        return self._with_triggers(self._definition(rows[0])) if rows else None

    def ensure(
        self,
        key: str,
        name: str,
        description: str,
        kind: str,
        interval: int = 1440,
        config: dict | None = None,
        enabled: bool = True,
    ) -> dict:
        existing = self.by_key(key)
        if existing:
            return existing
        timestamp = now()
        definition_id = new_id()
        columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(job_definitions)")
        }
        if {"interval_minutes", "enabled"}.issubset(columns):
            self.db.execute(
                "INSERT INTO job_definitions(id,job_key,name,description,kind,interval_minutes,enabled,config,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    definition_id,
                    key,
                    name,
                    description,
                    kind,
                    max(5, min(43200, int(interval or 1440))),
                    int(enabled),
                    json.dumps(config or {}, ensure_ascii=False),
                    None,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            self.db.execute(
                "INSERT INTO job_definitions(id,job_key,name,description,kind,config,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    definition_id,
                    key,
                    name,
                    description,
                    kind,
                    json.dumps(config or {}, ensure_ascii=False),
                    None,
                    timestamp,
                    timestamp,
                ),
            )
        if enabled:
            self._replace_triggers(
                definition_id,
                [
                    {
                        "type": "interval",
                        "intervalSeconds": max(1, int(interval or 1440) * 60),
                    }
                ],
            )
        return self.definition(definition_id)  # type: ignore[return-value]

    def _sync_default_definition_text(
        self,
        key: str,
        name: str,
        description: str,
        previous_name: str,
        previous_description: str,
    ) -> None:
        """Update labels from an older built-in definition without clobbering edits."""
        rows = self.db.execute(
            "SELECT id,name,description FROM job_definitions WHERE job_key=?",
            (key,),
        )
        if not rows:
            return
        definition_id, current_name, current_description = rows[0]
        next_name = name if current_name == previous_name else current_name
        next_description = (
            description
            if current_description == previous_description
            else current_description
        )
        if next_name == current_name and next_description == current_description:
            return
        self.db.execute(
            "UPDATE job_definitions SET name=?,description=?,updated_at=? WHERE id=?",
            (next_name, next_description, now(), definition_id),
        )

    def ensure_defaults(self) -> None:
        definition = self.ensure(
            "metadata_missing",
            "Find missing media metadata",
            "Fetch missing provider metadata, artwork, and credits for indexed movie, TV, album, and track records.",
            "metadata_missing",
            1440,
            {"locales": ["en"], "batchSize": 50},
        )
        self._sync_default_definition_text(
            "metadata_missing",
            "Find missing media metadata",
            "Fetch missing provider metadata, artwork, and credits for indexed movie, TV, album, and track records.",
            "Find missing metadata",
            "Fetch missing provider metadata, artwork, and credits for indexed IDs.",
        )
        if definition["lastRunAt"] is None:
            self.db.execute(
                "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
                (now(), now(), definition["id"]),
            )
        upgrade = self.ensure(
            "metadata_upgrade",
            "Find metadata upgrades",
            "Refetch provider metadata and repair existing metadata for indexed movie, TV, album, and track records.",
            "metadata_upgrade",
            10080,
            {"locales": ["en"], "batchSize": 50},
        )
        self._sync_default_definition_text(
            "metadata_upgrade",
            "Find metadata upgrades",
            "Refetch provider metadata and repair existing metadata for indexed movie, TV, album, and track records.",
            "Find metadata upgrade",
            "Refetch provider metadata and repair existing metadata that can be improved.",
        )
        if upgrade["lastRunAt"] is None:
            self.db.execute(
                "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
                (now(), now(), upgrade["id"]),
            )
        music_repair = self.ensure(
            "music_catalog_repair",
            "Repair music catalog identities",
            "Run the deterministic tag-first music inventory and repair release-context metadata without changing source files or user state.",
            "music_catalog_repair",
            1440,
            {"repairVersion": MusicCatalogRepairJob.REPAIR_VERSION},
            enabled=False,
        )
        repair_version = int(
            (music_repair.get("config") or {}).get("repairVersion") or 0
        )
        if (
            music_repair["lastRunAt"] is None
            and repair_version < MusicCatalogRepairJob.REPAIR_VERSION
        ):
            self.db.execute(
                "UPDATE job_definitions SET config=?,next_run_at=?,updated_at=? WHERE id=?",
                (
                    json.dumps(
                        {"repairVersion": MusicCatalogRepairJob.REPAIR_VERSION},
                        ensure_ascii=False,
                    ),
                    now(),
                    now(),
                    music_repair["id"],
                ),
            )
        elif music_repair["lastRunAt"] is None:
            self.db.execute(
                "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
                (now(), now(), music_repair["id"]),
            )
        self.ensure(
            "metadata_refresh",
            "Refresh media metadata",
            "Refresh indexed movie, TV, album, and track metadata and artwork using the configured sparse rules.",
            "metadata_refresh",
            43200,
            {},
            enabled=False,
        )
        self._sync_default_definition_text(
            "metadata_refresh",
            "Refresh media metadata",
            "Refresh indexed movie, TV, album, and track metadata and artwork using the configured sparse rules.",
            "Refresh metadata",
            "Refresh indexed metadata and artwork using the configured sparse rules.",
        )
        cleanup = self.ensure(
            "metadata_cleanup",
            "Clean orphaned library data",
            "Remove deleted-library inventory, metadata, and cached artwork leftovers.",
            "metadata_cleanup",
            10080,
            {},
        )
        if cleanup["lastRunAt"] is None:
            self.db.execute(
                "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
                (now(), now(), cleanup["id"]),
            )
        trickplay = self.ensure(
            "trickplay_extract",
            "Extract trickplay sheets",
            "Generate cached sprite sheets for indexed video sources.",
            "trickplay_extract",
            60,
            {},
        )
        if trickplay["lastRunAt"] is None:
            self.db.execute(
                "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
                (now(), now(), trickplay["id"]),
            )
        intro_outro = self.ensure(
            "intro_outro_detect",
            "Detect intros and outros",
            "Compare cached audio fingerprints for unscanned TV episodes.",
            "intro_outro_detect",
            60,
            {},
        )
        if intro_outro["lastRunAt"] is None:
            self.db.execute(
                "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
                (now(), now(), intro_outro["id"]),
            )
        bazarr_sync = self.ensure(
            "bazarr_sync",
            "Sync Bazarr mappings",
            "Refresh cached Bazarr series and episode mappings for indexed TV media.",
            "bazarr_sync",
            1440,
            {},
        )
        if not bazarr_sync.get("triggers") or (
            len(bazarr_sync["triggers"]) == 1
            and bazarr_sync["triggers"][0].get("type") == "interval"
            and bazarr_sync["triggers"][0].get("intervalSeconds") == 86400
        ):
            self._replace_triggers(
                bazarr_sync["id"], [{"type": "daily", "time": "02:00"}]
            )
        calendar_sync = self.ensure(
            "calendar_sync",
            "Sync calendar data",
            "Fetch the rolling Sonarr and Radarr calendar window.",
            "calendar_sync",
            1440,
            {},
        )
        if not calendar_sync.get("triggers") or (
            len(calendar_sync["triggers"]) == 1
            and calendar_sync["triggers"][0].get("type") == "interval"
            and calendar_sync["triggers"][0].get("intervalSeconds") == 86400
        ):
            self._replace_triggers(
                calendar_sync["id"], [{"type": "daily", "time": "03:00"}]
            )
        future_metadata = self.ensure(
            "calendar_future_metadata",
            "Refetch future calendar metadata",
            "Refetch isolated TMDB and TVDB metadata for upcoming calendar events.",
            "calendar_future_metadata",
            1440,
            {},
        )
        if not future_metadata.get("triggers") or (
            len(future_metadata["triggers"]) == 1
            and future_metadata["triggers"][0].get("type") == "interval"
            and future_metadata["triggers"][0].get("intervalSeconds") == 86400
        ):
            self._replace_triggers(
                future_metadata["id"], [{"type": "daily", "time": "04:00"}]
            )

    def ensure_library(self, library: dict) -> dict:
        description = "Index the library without moving or renaming files."
        definition = self.ensure(
            f"library_scan:{library['id']}",
            f"Scan {library['name']}",
            description,
            "library_scan",
            library.get("scanIntervalMinutes") or 1440,
            {"libraryId": library["id"]},
            library.get("watchEnabled", True),
        )
        # Repair older definitions whose config was lost by the former row mapper,
        # while preserving task-level interval and enabled settings.
        self.db.execute(
            "UPDATE job_definitions SET name=?,description=?,config=?,updated_at=? WHERE id=?",
            (
                f"Scan {library['name']}",
                description,
                json.dumps({"libraryId": library["id"]}),
                now(),
                definition["id"],
            ),
        )
        return self.definition(definition["id"])  # type: ignore[return-value]

    @staticmethod
    def _library_definition_key_owner(job_key: str) -> str | None:
        for prefix in ("library_scan:", "library_delta_verify:"):
            if job_key.startswith(prefix):
                return job_key.removeprefix(prefix) or None
        return None

    @classmethod
    def _library_definition_owner(cls, job_key: str, config: str | None) -> str | None:
        try:
            library_id = json.loads(config or "{}").get("libraryId")
        except (AttributeError, json.JSONDecodeError):
            library_id = None
        if library_id:
            return str(library_id)
        return cls._library_definition_key_owner(job_key)

    def _delete_definitions(self, definition_ids: list[str]) -> None:
        definition_ids = list(dict.fromkeys(definition_ids))
        if not definition_ids:
            return
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        with self.db.transaction() as cursor:
            for definition_id in definition_ids:
                if "job_schedule_triggers" in tables:
                    cursor.execute(
                        "DELETE FROM job_schedule_triggers WHERE definition_id=?",
                        (definition_id,),
                    )
                if "job_runs" in tables:
                    cursor.execute(
                        "DELETE FROM job_runs WHERE definition_id=?", (definition_id,)
                    )
                cursor.execute(
                    "DELETE FROM job_definitions WHERE id=?", (definition_id,)
                )

    def remove_library_definitions(self, library_id: str) -> None:
        rows = self.db.execute(
            "SELECT id,job_key,config FROM job_definitions WHERE kind='library_scan'"
        )
        self._delete_definitions(
            [
                row[0]
                for row in rows
                if library_id
                in {
                    self._library_definition_owner(row[1], row[2]),
                    self._library_definition_key_owner(row[1]),
                }
            ]
        )

    def reconcile_library_definitions(self, libraries: list[dict]) -> None:
        libraries_by_id = {str(library["id"]): library for library in libraries}
        definitions_by_library: dict[str, list[tuple[str, str]]] = {}
        orphan_ids = []
        for definition_id, job_key, config in self.db.execute(
            "SELECT id,job_key,config FROM job_definitions WHERE kind='library_scan'"
        ):
            library_id = self._library_definition_owner(job_key, config)
            key_library_id = self._library_definition_key_owner(job_key)
            if library_id not in libraries_by_id and key_library_id in libraries_by_id:
                library_id = key_library_id
            if library_id not in libraries_by_id:
                orphan_ids.append(definition_id)
                continue
            definitions_by_library.setdefault(library_id, []).append(
                (definition_id, job_key)
            )
        self._delete_definitions(orphan_ids)

        for library_id, definitions in definitions_by_library.items():
            canonical_key = f"library_scan:{library_id}"
            keeper = next(
                (
                    definition
                    for definition in definitions
                    if definition[1] == canonical_key
                ),
                definitions[0],
            )
            self._delete_definitions(
                [
                    definition_id
                    for definition_id, _key in definitions
                    if definition_id != keeper[0]
                ]
            )
            if keeper[1] != canonical_key:
                self.db.execute(
                    "UPDATE job_definitions SET job_key=?,updated_at=? WHERE id=?",
                    (canonical_key, now(), keeper[0]),
                )

        for library in libraries_by_id.values():
            self.ensure_library(library)

    @staticmethod
    def _validate_trigger(trigger: dict) -> dict:
        trigger_type = str(trigger.get("type", "")).strip().lower()
        if trigger_type == "interval":
            seconds = int(trigger.get("intervalSeconds", 0))
            if not 1 <= seconds <= 2_592_000:
                raise ValueError("intervalSeconds must be between 1 and 2592000")
            return {"type": "interval", "intervalSeconds": seconds}
        if trigger_type == "daily":
            value = str(trigger.get("time", ""))
            _local_next(value)
            return {"type": "daily", "time": value}
        if trigger_type == "weekly":
            value = str(trigger.get("time", ""))
            weekday = int(trigger.get("weekday", -1))
            _local_next(value, weekday)
            return {"type": "weekly", "weekday": weekday, "time": value}
        if trigger_type == "startup":
            return {"type": "startup"}
        raise ValueError("Unsupported schedule trigger")

    @staticmethod
    def _next_for_trigger(trigger: dict, base: datetime | None = None) -> str | None:
        if trigger["type"] == "startup":
            return None
        if trigger["type"] == "interval":
            return (
                (base or datetime.now(timezone.utc))
                + timedelta(seconds=trigger["intervalSeconds"])
            ).isoformat()
        if trigger["type"] == "daily":
            return _local_next(trigger["time"])
        return _local_next(trigger["time"], trigger["weekday"])

    def _replace_triggers(self, definition_id: str, triggers: list[dict]) -> None:
        definition = self.definition(definition_id)
        if not definition:
            raise KeyError("Job definition not found")
        validated = [
            {
                **self._validate_trigger(trigger),
                "id": str(trigger.get("id") or new_id()),
                "options": self.validate_options(
                    definition["kind"], trigger.get("options"), allow_manual=False
                ),
            }
            for trigger in triggers
        ]
        timestamp = now()
        self.db.execute(
            "DELETE FROM job_schedule_triggers WHERE definition_id=?", (definition_id,)
        )
        for trigger in validated:
            try:
                self.db.execute(
                    "INSERT INTO job_schedule_triggers(id,definition_id,trigger_type,interval_seconds,time_of_day,weekday,next_run_at,options,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        trigger["id"],
                        definition_id,
                        trigger["type"],
                        trigger.get("intervalSeconds"),
                        trigger.get("time"),
                        trigger.get("weekday"),
                        self._next_for_trigger(trigger),
                        json.dumps(trigger["options"], ensure_ascii=False),
                        timestamp,
                        timestamp,
                    ),
                )
            except Exception:
                self.db.execute(
                    "INSERT INTO job_schedule_triggers(id,definition_id,trigger_type,interval_seconds,time_of_day,weekday,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        trigger["id"],
                        definition_id,
                        trigger["type"],
                        trigger.get("intervalSeconds"),
                        trigger.get("time"),
                        trigger.get("weekday"),
                        self._next_for_trigger(trigger),
                        timestamp,
                        timestamp,
                    ),
                )

    def add_trigger(self, definition_id: str, trigger: dict) -> dict:
        definition = self.definition(definition_id)
        if not definition:
            raise KeyError("Job definition not found")
        validated = self._validate_trigger(trigger)
        validated["options"] = self.validate_options(
            definition["kind"], trigger.get("options"), allow_manual=False
        )
        trigger_id = str(trigger.get("id") or new_id())
        timestamp = now()
        try:
            self.db.execute(
                "INSERT INTO job_schedule_triggers(id,definition_id,trigger_type,interval_seconds,time_of_day,weekday,next_run_at,options,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    trigger_id,
                    definition_id,
                    validated["type"],
                    validated.get("intervalSeconds"),
                    validated.get("time"),
                    validated.get("weekday"),
                    self._next_for_trigger(validated),
                    json.dumps(validated["options"], ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
        except Exception:
            self.db.execute(
                "INSERT INTO job_schedule_triggers(id,definition_id,trigger_type,interval_seconds,time_of_day,weekday,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    trigger_id,
                    definition_id,
                    validated["type"],
                    validated.get("intervalSeconds"),
                    validated.get("time"),
                    validated.get("weekday"),
                    self._next_for_trigger(validated),
                    timestamp,
                    timestamp,
                ),
            )
        self.db.execute(
            "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
            (self._earliest_next(definition_id), timestamp, definition_id),
        )
        return self.definition(definition_id)  # type: ignore[return-value]

    def remove_trigger(self, definition_id: str, trigger_id: str) -> dict:
        definition = self.definition(definition_id)
        if not definition:
            raise KeyError("Job definition not found")
        exists = self.db.execute(
            "SELECT 1 FROM job_schedule_triggers WHERE id=? AND definition_id=?",
            (trigger_id, definition_id),
        )
        if not exists:
            raise KeyError("Trigger not found")
        self.db.execute(
            "DELETE FROM job_schedule_triggers WHERE id=? AND definition_id=?",
            (trigger_id, definition_id),
        )
        self.db.execute(
            "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
            (self._earliest_next(definition_id), now(), definition_id),
        )
        return self.definition(definition_id)  # type: ignore[return-value]

    def _earliest_next(self, definition_id: str) -> str | None:
        try:
            rows = self.db.execute(
                "SELECT next_run_at FROM job_schedule_triggers WHERE definition_id=? "
                "AND next_run_at IS NOT NULL ORDER BY next_run_at LIMIT 1",
                (definition_id,),
            )
        except Exception:
            return None
        return rows[0][0] if rows else None

    def update_definition(self, definition_id: str, values: dict) -> dict:
        definition = self.definition(definition_id)
        if not definition:
            raise KeyError("Job definition not found")
        if "intervalMinutes" in values or "enabled" in values:
            raise ValueError("intervalMinutes and enabled are trigger properties")
        name = str(values.get("name", definition["name"])).strip() or definition["name"]
        config = values.get("config", definition["config"])
        self.db.execute(
            "UPDATE job_definitions SET name=?,config=?,next_run_at=?,updated_at=? WHERE id=?",
            (
                name,
                json.dumps(config or {}, ensure_ascii=False),
                self._earliest_next(definition_id),
                now(),
                definition_id,
            ),
        )
        return self.definition(definition_id)  # type: ignore[return-value]

    def runs(self, definition_id: str | None = None, limit: int = 100) -> list[dict]:
        detail = ",".join(self._progress_columns("job_runs"))
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(job_runs)")}
        scan_stats = "scan_stats" if "scan_stats" in columns else "NULL AS scan_stats"
        snapshot = (
            ",source_trigger_id,options"
            if {"source_trigger_id", "options"}.issubset(columns)
            else ",NULL,NULL"
        )
        if definition_id:
            rows = self.db.execute(
                f"SELECT id,definition_id,library_id,kind,state,progress_current,progress_total,message,error,error_details,created_at,started_at,finished_at,thread_name,{detail},{scan_stats}{snapshot} FROM job_runs WHERE definition_id=? ORDER BY created_at DESC LIMIT ?",
                (definition_id, limit),
            )
        else:
            rows = self.db.execute(
                f"SELECT id,definition_id,library_id,kind,state,progress_current,progress_total,message,error,error_details,created_at,started_at,finished_at,thread_name,{detail},{scan_stats}{snapshot} FROM job_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [self._run(row) for row in rows]

    def library_runs(
        self,
        library_id: str,
        limit: int = 10,
        kinds: set[str] | None = None,
    ) -> list[dict]:
        query = (
            "SELECT id,library_id,kind,state,progress_current,progress_total,message,error,error_details,created_at,started_at,finished_at,"
            + ",".join(self._progress_columns("library_jobs"))
            + ","
            + (
                "scan_stats"
                if "scan_stats"
                in {
                    row[1] for row in self.db.execute("PRAGMA table_info(library_jobs)")
                }
                else "NULL AS scan_stats"
            )
            + ",NULL,NULL "
            "FROM library_jobs WHERE library_id=?"
        )
        params: list[object] = [library_id]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            query += f" AND kind IN ({placeholders})"
            params.extend(sorted(kinds))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.db.execute(query, params)
        return [
            {
                "id": row[0],
                "definitionId": None,
                "libraryId": row[1],
                "kind": row[2],
                "state": row[3],
                "progressCurrent": row[4],
                "progressTotal": row[5],
                "message": row[6],
                "error": row[7],
                "errorDetails": row[8],
                "createdAt": row[9],
                "startedAt": row[10],
                "finishedAt": row[11],
                "threadName": None,
                "progressDetail": self._progress_detail(row, 12),
                "scanStats": self._scan_stats(row[18]),
            }
            for row in rows
        ]

    def cleanup_history(
        self, retention_days: int = 30, batch_size: int = 500
    ) -> dict[str, int]:
        """Delete old terminal history in bounded transactions.

        Definitions and active/terminating work are durable control state and
        are intentionally preserved.  Only completed history is retention
        managed, and each batch keeps the SQLite writer hold short.
        """
        days = max(1, int(retention_days))
        batch = max(1, min(5000, int(batch_size)))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        removed = {"job_runs": 0, "library_jobs": 0}
        for table in removed:
            try:
                columns = {
                    row[1] for row in self.db.execute(f"PRAGMA table_info({table})")
                }
            except Exception:
                columns = set()
            if not {"id", "state", "finished_at"}.issubset(columns):
                continue
            while True:
                with self.db.transaction() as cursor:
                    cursor.execute(
                        f"DELETE FROM {table} WHERE id IN ("
                        f"SELECT id FROM {table} WHERE state IN ('completed','failed','terminated','cancelled') "
                        "AND finished_at IS NOT NULL AND finished_at<? ORDER BY finished_at LIMIT ?)",
                        (cutoff, batch),
                    )
                    count = max(0, cursor.rowcount)
                removed[table] += count
                if count < batch:
                    break
        return removed

    def create_run(self, definition: dict) -> dict:
        run_id = new_id()
        library_id = (definition.get("config") or {}).get("libraryId")
        timestamp = now()
        self.db.execute(
            "INSERT INTO job_runs(id,definition_id,library_id,kind,created_at) VALUES(?,?,?,?,?)",
            (run_id, definition["id"], library_id, definition["kind"], timestamp),
        )
        self._mark_definition_queued(definition["id"], run_id, timestamp)
        return self.runs(definition["id"], 1)[0]

    def create_or_get_active_run(
        self,
        definition: dict,
        options: dict | None = None,
        source_trigger_id: str | None = None,
    ) -> tuple[dict, bool]:
        """Atomically keep at most one queued/running run for a task definition."""
        timestamp = now()
        with self.db.transaction() as cursor:
            cursor.execute(
                "SELECT id FROM job_runs WHERE definition_id=? AND state IN ('queued','running','terminating') ORDER BY created_at DESC LIMIT 1",
                (definition["id"],),
            )
            existing = cursor.fetchone()
            if existing:
                run_id = existing[0]
                created = False
            else:
                run_id = new_id()
                library_id = (definition.get("config") or {}).get("libraryId")
                values = self.validate_options(
                    definition["kind"],
                    options,
                    allow_manual=source_trigger_id is None,
                )
                columns = {
                    row[1] for row in self.db.execute("PRAGMA table_info(job_runs)")
                }
                if {"source_trigger_id", "options"}.issubset(columns):
                    cursor.execute(
                        "INSERT INTO job_runs(id,definition_id,library_id,kind,source_trigger_id,options,created_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            run_id,
                            definition["id"],
                            library_id,
                            definition["kind"],
                            source_trigger_id,
                            json.dumps(values, ensure_ascii=False),
                            timestamp,
                        ),
                    )
                else:
                    cursor.execute(
                        "INSERT INTO job_runs(id,definition_id,library_id,kind,created_at) VALUES(?,?,?,?,?)",
                        (
                            run_id,
                            definition["id"],
                            library_id,
                            definition["kind"],
                            timestamp,
                        ),
                    )
                self._mark_definition_queued(
                    definition["id"], run_id, timestamp, executor=cursor
                )
                created = True
        runs = [run for run in self.runs(definition["id"], 100) if run["id"] == run_id]
        return runs[0], created

    def queued_or_running(self, definition_id: str) -> bool:
        return bool(
            self.db.execute(
                "SELECT 1 FROM job_runs WHERE definition_id=? AND state IN ('queued','running','terminating') LIMIT 1",
                (definition_id,),
            )
        )

    def due_triggers(self) -> list[dict]:
        current = now()
        try:
            rows = self.db.execute(
                "SELECT t.id,t.definition_id,t.trigger_type,t.interval_seconds,t.time_of_day,t.weekday,t.next_run_at,t.options FROM job_schedule_triggers t WHERE t.next_run_at IS NOT NULL AND t.next_run_at<=? ORDER BY t.next_run_at,t.created_at",
                (current,),
            )
        except Exception:
            rows = self.db.execute(
                "SELECT t.id,t.definition_id,t.trigger_type,t.interval_seconds,t.time_of_day,t.weekday,t.next_run_at,NULL FROM job_schedule_triggers t WHERE t.next_run_at IS NOT NULL AND t.next_run_at<=? ORDER BY t.next_run_at,t.created_at",
                (current,),
            )
        values = []
        for row in rows:
            definition = self.definition(row[1])
            if not definition:
                continue
            trigger = {"id": row[0], "type": row[2], "nextRunAt": row[6]}
            if row[2] == "interval":
                trigger["intervalSeconds"] = row[3]
            elif row[2] == "daily":
                trigger["time"] = row[4]
            elif row[2] == "weekly":
                trigger["weekday"], trigger["time"] = row[5], row[4]
            try:
                trigger["options"] = json.loads(row[7] or "{}")
            except (TypeError, json.JSONDecodeError):
                trigger["options"] = {}
            values.append({"definition": definition, "trigger": trigger})
        return values

    def mark_trigger_scheduled(
        self,
        definition_id: str,
        trigger: dict,
        run_id: str | None,
        message: str = "Queued",
    ) -> None:
        current = now()
        next_trigger = self._next_for_trigger(trigger, datetime.now(timezone.utc))
        self.db.execute(
            "UPDATE job_schedule_triggers SET next_run_at=?,updated_at=? WHERE id=? AND definition_id=?",
            (next_trigger, current, trigger["id"], definition_id),
        )
        self.db.execute(
            "UPDATE job_definitions SET next_run_at=?,last_run_at=?,last_run_id=?,last_state='queued',last_message=?,updated_at=? WHERE id=?",
            (
                self._earliest_next(definition_id),
                current,
                run_id,
                message,
                current,
                definition_id,
            ),
        )

    def update_run(self, run_id: str, **values) -> None:
        tracker = getattr(self, "_progress", {}).get(run_id)
        if tracker is not None:
            values = tracker.apply(values)
        allowed = {
            "state",
            "progress_current",
            "progress_total",
            "message",
            "error",
            "error_details",
            "started_at",
            "finished_at",
            "thread_name",
            "progress_phase",
            "progress_label",
            "progress_stage_current",
            "progress_stage_total",
            "progress_stage_unit",
            "progress_current_item",
            "scan_stats",
        }
        try:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(job_runs)")}
            allowed = {key for key in allowed if key in columns}
        except Exception:
            pass
        updates = [(key, value) for key, value in values.items() if key in allowed]
        if updates:
            fields = ",".join(f"{key}=?" for key, _ in updates)
            self.db.execute(
                f"UPDATE job_runs SET {fields} WHERE id=?",
                [value for _, value in updates] + [run_id],
            )
        row = self.db.execute(
            "SELECT definition_id,state,message,error,created_at,started_at,finished_at,id "
            "FROM job_runs WHERE id=?",
            (run_id,),
        )
        if row:
            self._update_definition_from_run(row[0])


class MetadataMissingJob:
    def __init__(
        self,
        store: JobStore,
        library_runtime=None,
        catalog_work_coordinator: CatalogWorkCoordinator | None = None,
    ):
        self.store = store
        self.db = store.db
        self.library_runtime = (
            library_runtime
            if library_runtime is not None
            else globals().get("library_runtime")
        )
        self.catalog_work_coordinator = catalog_work_coordinator

    @contextmanager
    def _suspend_metadata_catalog_lock(self):
        """Allow providerless recovery to run its nested library reconcile."""
        coordinator = self.catalog_work_coordinator
        if coordinator is None:
            yield
            return
        with coordinator.suspend_metadata():
            yield

    def _missing_primary_entity_rows(self) -> list[tuple]:
        """Find non-manual entities invisible to provider-ID worklists."""
        entity_types = sorted(PRIMARY_METADATA_IDENTITIES)
        entity_placeholders = ",".join("?" for _ in entity_types)
        identity_clauses = []
        identity_params: list[str] = []
        for entity_type, (provider, identifier_type) in sorted(
            PRIMARY_METADATA_IDENTITIES.items()
        ):
            identity_clauses.append(
                "(e.entity_type=? AND p.provider=? AND p.identifier_type=?)"
            )
            identity_params.extend((entity_type, provider, identifier_type))
        try:
            entity_columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(library_entities)")
            }
        except Exception:
            entity_columns = set()
        manual_filter = (
            "AND COALESCE(e.match_method,'')<>'manual'"
            if "match_method" in entity_columns
            else ""
        )
        relative_path = (
            "e.relative_path" if "relative_path" in entity_columns else "NULL"
        )
        parent_id = "e.parent_id" if "parent_id" in entity_columns else "NULL"
        try:
            rows = self.db.execute(
                "SELECT e.id,e.library_id,COALESCE(l.name,e.library_id),"
                f"e.entity_type,{relative_path} AS relative_path,{parent_id} AS parent_id "
                "FROM library_entities e JOIN libraries l ON l.id=e.library_id "
                f"WHERE l.type<>'collection' {manual_filter} "
                f"AND e.entity_type IN ({entity_placeholders}) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM entity_provider_ids p "
                "WHERE p.entity_id=e.id "
                "AND p.provider_id IS NOT NULL AND TRIM(p.provider_id)<>'' "
                f"AND ({' OR '.join(identity_clauses)})"
                ") ORDER BY e.library_id",
                [*entity_types, *identity_params],
            )
        except Exception:
            # Minimal/legacy test databases and an in-progress first install
            # may not have the library inventory tables yet. The normal
            # provider-ID worklist remains usable in that case.
            logger.debug(
                "providerless metadata recovery discovery unavailable",
                exc_info=True,
            )
            return []
        return [
            (
                str(entity_id),
                str(library_id),
                str(name or library_id),
                str(entity_type),
                relative_path,
                parent_id,
            )
            for entity_id, library_id, name, entity_type, relative_path, parent_id in rows
        ]

    def _missing_primary_library_rows(self) -> list[tuple[str, str]]:
        """Find libraries containing entities invisible to provider-ID worklists."""
        libraries = {(row[1], row[2]) for row in self._missing_primary_entity_rows()}
        return sorted(libraries)

    @staticmethod
    def _target_root(relative_path) -> str | None:
        normalized = str(relative_path or "").replace("\\", "/").strip("/")
        if not normalized:
            return None
        root = normalized.split("/", 1)[0].strip()
        if not root or root in {".", ".."} or ":" in root:
            return None
        return root

    def _entity_target_root(self, row: tuple, entity_columns: set[str]) -> str | None:
        target = self._target_root(row[4])
        if target or not {"parent_id", "relative_path"} <= entity_columns:
            return target
        current = row[5]
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(str(current))
            parent_rows = self.db.execute(
                "SELECT parent_id,relative_path FROM library_entities WHERE id=?",
                (current,),
            )
            if not parent_rows:
                break
            parent_id, relative_path = parent_rows[0]
            target = self._target_root(relative_path)
            if target:
                return target
            current = parent_id
        return None

    def _select_metadata_items(
        self,
        rows: list[tuple],
        locales: list[str],
        *,
        operation: str,
        force: bool,
        has_enrichment_queue: bool,
    ) -> tuple[list[tuple], dict[tuple, list[str]] | None]:
        """Select only new, due, or explicitly queued repair documents."""
        if operation != "metadata_missing" or force:
            return list(rows), None
        if not _metadata_recovery_state_table(self.db):
            # Keep first-install and old fixture databases compatible. The
            # migration enables the durable sparse worklist in production.
            return list(rows), None

        state_rows = {
            (str(provider), str(entity_type), str(provider_id), str(locale or "")): (
                str(state),
                int(attempts or 0),
                next_attempt_at,
            )
            for provider, entity_type, provider_id, locale, state, attempts, next_attempt_at in self.db.execute(
                "SELECT provider,entity_type,provider_id,locale,state,attempts,next_attempt_at "
                "FROM metadata_missing_state"
            )
        }
        queued_keys: set[tuple[str, str, str, str]] = set()
        if has_enrichment_queue:
            queue_rows = self.db.execute(
                "SELECT DISTINCT ep.provider,ep.identifier_type,ep.provider_id,q.locale "
                "FROM enrichment_queue q JOIN entity_provider_ids ep ON ep.entity_id=q.entity_id "
                "WHERE q.kind='metadata' AND q.state IN ('queued','retry') "
                "AND q.attempts < ? AND (q.next_attempt_at IS NULL OR q.next_attempt_at<=?)",
                (METADATA_MISSING_MAX_ATTEMPTS, now()),
            )
            for provider, identifier_type, provider_id, locale in queue_rows:
                entity_type = _metadata_catalog_entity_type(provider, identifier_type)
                normalized_locale = str(locale or "")
                if (
                    provider == "musicbrainz"
                    and entity_type in MUSICBRAINZ_NEUTRAL_ENTITY_TYPES
                ):
                    normalized_locale = ""
                queued_keys.add(
                    (str(provider), entity_type, str(provider_id), normalized_locale)
                )

        selected: list[tuple] = []
        locales_by_item: dict[tuple, list[str]] = {}
        for item in rows:
            provider, identifier_type, provider_id = item
            entity_type = _metadata_catalog_entity_type(provider, identifier_type)
            neutral = (
                provider == "musicbrainz"
                and entity_type in MUSICBRAINZ_NEUTRAL_ENTITY_TYPES
            )
            item_locales = [""] if neutral else list(locales)
            due_locales = []
            for locale in item_locales:
                key = (provider, entity_type, str(provider_id), locale)
                state = state_rows.get(key)
                queued = key in queued_keys
                if state is None:
                    # Existing rows are bootstrapped once. Successful work is
                    # marked completed below, so later runs stay sparse.
                    due_locales.append(locale)
                elif (
                    state[0] in {"queued", "retry"}
                    and _metadata_recovery_due(state)
                    or state[0] == "completed"
                    and queued
                ):
                    due_locales.append(locale)
            if due_locales:
                selected.append(item)
                locales_by_item[item] = due_locales
        return selected, locales_by_item

    def _recover_missing_primary_entities(
        self,
        run_id: str,
        should_terminate,
    ) -> tuple[list[dict], list[dict]]:
        """Recover providerless entities through bounded targeted reconciles."""
        missing_rows = self._missing_primary_entity_rows()
        if not missing_rows:
            return [], []
        runtime = self.library_runtime
        if runtime is None:
            for row in missing_rows:
                _record_metadata_recovery_state(
                    self.db,
                    METADATA_IDENTITY_PROVIDER,
                    row[3],
                    row[0],
                    error="Library runtime is unavailable",
                    source_job_id=run_id,
                )
            return (
                [
                    {
                        "kind": "identity_recovery",
                        "error": "Library runtime is unavailable",
                    }
                ],
                [],
            )
        state_available = _metadata_recovery_state_table(self.db)
        state_rows = {}
        if state_available:
            state_rows = {
                (
                    str(provider),
                    str(entity_type),
                    str(provider_id),
                    str(locale or ""),
                ): (
                    str(state),
                    int(attempts or 0),
                    next_attempt_at,
                )
                for provider, entity_type, provider_id, locale, state, attempts, next_attempt_at in self.db.execute(
                    "SELECT provider,entity_type,provider_id,locale,state,attempts,next_attempt_at "
                    "FROM metadata_missing_state WHERE provider=?",
                    (METADATA_IDENTITY_PROVIDER,),
                )
            }
            missing_rows = [
                row
                for row in missing_rows
                if _metadata_recovery_due(
                    state_rows.get(
                        (
                            METADATA_IDENTITY_PROVIDER,
                            row[3],
                            row[0],
                            "",
                        )
                    )
                )
            ]
        if not missing_rows:
            return [], []
        thread = getattr(runtime, "thread", None)
        if not thread or not getattr(thread, "is_alive", lambda: False)():
            runtime.start()
        failures: list[dict] = []
        incomplete: list[dict] = []
        entity_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(library_entities)")
        }
        rows_by_library: dict[str, list[tuple]] = {}
        names_by_library: dict[str, str] = {}
        for row in missing_rows:
            rows_by_library.setdefault(row[1], []).append(row)
            names_by_library[row[1]] = row[2]
        libraries = sorted(rows_by_library)
        for index, library_id in enumerate(libraries, start=1):
            name = names_by_library[library_id]
            if should_terminate():
                raise JobTerminated()
            runtime.suppress_library_notifications(library_id, True)
            library_rows = rows_by_library[library_id]
            targets = {
                target
                for target in (
                    self._entity_target_root(row, entity_columns)
                    for row in library_rows
                )
                if target
            }
            error_text = None
            try:
                if not targets:
                    error_text = "Providerless entities have no targetable library path"
                    raise RuntimeError(error_text)
                with self._suspend_metadata_catalog_lock():
                    job = runtime.enqueue(
                        library_id,
                        "reconcile",
                        targets=targets,
                        force_metadata=True,
                    )
                    if not job:
                        error_text = "Library reconcile could not be queued"
                        failures.append(
                            {
                                "kind": "identity_recovery",
                                "libraryId": library_id,
                                "name": name,
                                "error": error_text,
                            }
                        )
                        for row in library_rows:
                            _record_metadata_recovery_state(
                                self.db,
                                METADATA_IDENTITY_PROVIDER,
                                row[3],
                                row[0],
                                error=error_text,
                                source_job_id=run_id,
                            )
                        continue
                    was_active = job.get("state") in {
                        "running",
                        "terminating",
                    }
                    result = runtime.wait_for_job(
                        job["id"],
                        should_terminate=should_terminate,
                    )
                    if should_terminate():
                        raise JobTerminated()
                    if not result or result.get("state") not in {
                        "completed",
                        "completed_with_warnings",
                    }:
                        error_text = (result.get("error") if result else None) or (
                            f"Library reconcile ended in {result.get('state')}"
                            if result
                            else "Library reconcile result was not found"
                        )
                        failures.append(
                            {
                                "kind": "identity_recovery",
                                "libraryId": library_id,
                                "name": name,
                                "jobId": job.get("id"),
                                "state": result.get("state") if result else "missing",
                                "error": error_text,
                            }
                        )
                remaining_ids = {
                    row[0]
                    for row in self._missing_primary_entity_rows()
                    if row[1] == library_id
                }
                # An already-running reconcile may have taken its force flag
                # before this recovery request arrived. Allow one targeted
                # follow-up, never another library-wide pass or an unbounded
                # feedback loop.
                if not error_text and was_active and remaining_ids:
                    with self._suspend_metadata_catalog_lock():
                        retry_job = runtime.enqueue(
                            library_id,
                            "reconcile",
                            targets=targets,
                            force_metadata=True,
                        )
                        if retry_job:
                            retry_result = runtime.wait_for_job(
                                retry_job["id"],
                                should_terminate=should_terminate,
                            )
                            if should_terminate():
                                raise JobTerminated()
                            if not retry_result or retry_result.get("state") not in {
                                "completed",
                                "completed_with_warnings",
                            }:
                                error_text = (
                                    retry_result.get("error") if retry_result else None
                                ) or (
                                    f"Targeted recovery reconcile ended in {retry_result.get('state')}"
                                    if retry_result
                                    else "Targeted recovery reconcile result was not found"
                                )
                                failures.append(
                                    {
                                        "kind": "identity_recovery",
                                        "libraryId": library_id,
                                        "name": name,
                                        "jobId": retry_job.get("id"),
                                        "state": (
                                            retry_result.get("state")
                                            if retry_result
                                            else "missing"
                                        ),
                                        "error": (error_text),
                                    }
                                )
                            else:
                                remaining_ids = {
                                    row[0]
                                    for row in self._missing_primary_entity_rows()
                                    if row[1] == library_id
                                }
                if error_text:
                    for row in library_rows:
                        _record_metadata_recovery_state(
                            self.db,
                            METADATA_IDENTITY_PROVIDER,
                            row[3],
                            row[0],
                            error=error_text,
                            source_job_id=run_id,
                        )
                else:
                    for row in library_rows:
                        if row[0] in remaining_ids:
                            _record_metadata_recovery_state(
                                self.db,
                                METADATA_IDENTITY_PROVIDER,
                                row[3],
                                row[0],
                                error=(
                                    "Required provider identity remains missing "
                                    "after targeted recovery"
                                ),
                                source_job_id=run_id,
                            )
                        else:
                            _clear_metadata_recovery_state(
                                self.db,
                                METADATA_IDENTITY_PROVIDER,
                                row[3],
                                row[0],
                            )
                if remaining_ids:
                    incomplete.append(
                        {
                            "kind": "identity_recovery",
                            "libraryId": library_id,
                            "name": name,
                            "error": "Required provider identities remain missing after targeted recovery",
                        }
                    )
            except JobTerminated:
                raise
            except Exception as error:
                error_text = f"{type(error).__name__}: {error}"
                for row in library_rows:
                    _record_metadata_recovery_state(
                        self.db,
                        METADATA_IDENTITY_PROVIDER,
                        row[3],
                        row[0],
                        error=error_text,
                        source_job_id=run_id,
                    )
                failures.append(
                    {
                        "kind": "identity_recovery",
                        "libraryId": library_id,
                        "name": name,
                        "error": error_text,
                    }
                )
                logger.exception(
                    "providerless metadata recovery failed library_id=%s",
                    library_id,
                )
            finally:
                runtime.suppress_library_notifications(library_id, False)
            self.store.update_run(
                run_id,
                progress_phase="identity_recovery",
                progress_label="Recovering missing provider identities",
                message=(
                    f"Recovered provider identities for {name} · "
                    f"{index}/{len(libraries)} libraries · {len(targets)} roots"
                ),
            )
        return failures, incomplete

    def run(
        self,
        run_id: str,
        definition: dict,
        should_terminate=None,
        force: bool = False,
        force_assets: bool | None = None,
        operation: str | None = None,
    ) -> None:
        should_terminate = should_terminate or (lambda: False)
        operation = operation or ("metadata_refresh" if force else "metadata_missing")
        is_upgrade = operation == "metadata_upgrade"
        job_started = time.monotonic()
        metrics_before = (
            self.db.metrics() if callable(getattr(self.db, "metrics", None)) else {}
        )
        ingest = MetadataIngestService(background_assets=False)
        locales = ingest.locales()
        _repair_missing_tv_child_identities(
            self.db,
            ingest.metadata_service,
            run_id=run_id,
            should_terminate=should_terminate,
            persist_state=operation == "metadata_missing",
        )
        upgrade_state_columns = (
            _metadata_upgrade_state_columns(self.db) if is_upgrade else set()
        )
        upgrade_state_enabled = METADATA_UPGRADE_STATE_COLUMNS <= upgrade_state_columns
        upgrade_state = (
            _load_metadata_upgrade_state(self.db) if upgrade_state_enabled else {}
        )
        _repair_missing_tv_child_identities(self.db, ingest.metadata_service)
        if operation == "metadata_missing":
            identity_failures, identity_incomplete = (
                self._recover_missing_primary_entities(run_id, should_terminate)
            )
        else:
            identity_failures, identity_incomplete = [], []
        config = definition.get("config") or {}
        batch_size = max(1, min(500, int(config.get("batchSize") or 50)))
        has_enrichment_queue = bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrichment_queue'"
            )
        )
        # Discover optional TMDB series identities from the authoritative TVDB
        # documents before taking the work-list snapshot.  This lets a missing
        # or forced refresh ingest newly discovered TMDB documents in the same
        # run. An upgrade is deliberately limited to existing cached documents
        # and must not fetch every TVDB series before fetching it again below.
        tv_series_rows = (
            []
            if is_upgrade
            else self.db.execute(
                "SELECT e.id,p.provider_id FROM library_entities e "
                "JOIN entity_provider_ids p ON p.entity_id=e.id "
                "WHERE e.entity_type='series' AND p.provider='tvdb' "
                "AND p.identifier_type='series' ORDER BY e.id"
            )
        )
        for entity_id, tvdb_id in tv_series_rows:
            try:
                if is_upgrade:
                    documents = _fetch_upgrade_documents(
                        ingest,
                        "tvdb",
                        "series",
                        str(tvdb_id),
                        locales,
                    )
                elif force:
                    documents = ingest.ingest_locales(
                        "tvdb",
                        "series",
                        str(tvdb_id),
                        locales,
                        force=True,
                        force_assets=force_assets,
                    )
                else:
                    documents = {
                        locale: ingest.metadata_service.cache.get(
                            "tvdb", "series", str(tvdb_id), locale
                        )
                        for locale in locales
                    }
            except (ProviderError, ValueError, OSError) as error:
                logger.warning(
                    "TVDB series secondary-ID discovery failed entity_id=%s provider_id=%s: %s",
                    entity_id,
                    tvdb_id,
                    error,
                )
                continue
            linked_ids = {
                str(value.get("id"))
                for document in documents.values()
                if isinstance(document, dict)
                for value in document.get("ids", []) or []
                if value.get("provider") == "tmdb" and value.get("id")
            }
            for tmdb_id in sorted(linked_ids):
                self.db.execute(
                    "INSERT OR IGNORE INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES(?,?,?,?,0)",
                    (entity_id, "tmdb", "series", tmdb_id),
                )
        rows = self.db.execute(
            "SELECT DISTINCT p.provider,p.identifier_type,p.provider_id "
            "FROM entity_provider_ids p JOIN library_entities e ON e.id=p.entity_id "
            "WHERE p.provider IN ('tmdb','tvdb','musicbrainz','lastfm') "
            # MusicBrainz release-group, release-track, and work IDs are
            # supporting identities attached to an admitted release/track;
            # they are not catalog metadata documents. Treating them as
            # entity types here makes the repair job issue redundant or
            # malformed requests for every configured locale.
            "AND NOT (p.provider='musicbrainz' AND p.identifier_type IN "
            "('release_group','release_track','work')) "
            "ORDER BY p.provider,e.entity_type,p.provider_id"
        )
        items = list(rows)
        try:
            lastfm_configured = bool(ingest.metadata_service.credentials.get("lastfm"))
        except (AttributeError, ValueError, RuntimeError):
            lastfm_configured = False
        if not lastfm_configured:
            # Clearing the optional key disables future network work while
            # deliberately retaining Last.fm identities, cached documents,
            # artwork, and the projected enrichment already on the catalog.
            items = [item for item in items if item[0] != "lastfm"]
        items, locales_by_item = self._select_metadata_items(
            items,
            locales,
            operation=operation,
            force=force,
            has_enrichment_queue=has_enrichment_queue,
        )
        total = 0
        for item in items:
            provider, identifier_type, _provider_id = item
            entity_type = _metadata_catalog_entity_type(provider, identifier_type)
            if (
                provider == "musicbrainz"
                and entity_type in MUSICBRAINZ_NEUTRAL_ENTITY_TYPES
            ):
                total += 1
            else:
                total += len((locales_by_item or {}).get(item, locales))
        has_screen_assets = bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='screen_extractor_assets'"
            )
        )
        extractor_rows = (
            self.db.execute(
                "SELECT id,entity_type FROM library_entities "
                "WHERE entity_type IN ('movie','episode') ORDER BY id"
            )
            if has_screen_assets
            else []
        )
        extractor_total = len(extractor_rows)
        self.store.update_run(
            run_id,
            state="running",
            started_at=now(),
            thread_name=threading.current_thread().name,
            progress_total=total + extractor_total,
            progress_phase="discovery",
            progress_label=(
                "Discovering metadata upgrades"
                if is_upgrade
                else "Discovering metadata work"
            ),
            progress_stage_current=0,
            progress_stage_total=total + extractor_total,
            progress_stage_unit="documents",
            message=format_progress_message(
                (
                    "Discovering metadata upgrades"
                    if is_upgrade
                    else "Discovering metadata work"
                ),
                current=0,
                total=total + extractor_total,
                unit="documents",
            ),
        )

        def queue_failures(
            provider: str,
            entity_type: str,
            provider_id: str,
            item_failures: list[dict],
        ) -> None:
            if not item_failures:
                return
            timestamp = now()
            failures_by_locale = {
                locale: [
                    failure
                    for failure in item_failures
                    if failure.get("locale") == locale
                ]
                for locale in {
                    str(failure.get("locale") or "") for failure in item_failures
                }
            }
            for locale, locale_failures in failures_by_locale.items():
                retryable = any(
                    _metadata_failure_is_retryable(failure)
                    for failure in locale_failures
                )
                _record_metadata_recovery_state(
                    self.db,
                    provider,
                    entity_type,
                    provider_id,
                    locale=locale,
                    error=json.dumps(locale_failures, ensure_ascii=False),
                    source_job_id=run_id,
                    permanent=not retryable,
                )
            if not has_enrichment_queue:
                return
            entity_rows = self.db.execute(
                "SELECT ep.entity_id,e.library_id FROM entity_provider_ids ep "
                "JOIN library_entities e ON e.id=ep.entity_id "
                "WHERE ep.provider=? AND ep.identifier_type=? AND ep.provider_id=?",
                (provider, _metadata_identity_type(provider, entity_type), provider_id),
            )
            with self.db.transaction() as cursor:
                for entity_id, library_id in entity_rows:
                    for locale, locale_failures in failures_by_locale.items():
                        existing = cursor.execute(
                            "SELECT attempts FROM enrichment_queue "
                            "WHERE entity_id=? AND kind='metadata' AND locale=?",
                            (entity_id, locale),
                        ).fetchone()
                        attempts = int(existing[0] or 0) + 1 if existing else 1
                        retryable = any(
                            _metadata_failure_is_retryable(failure)
                            for failure in locale_failures
                        )
                        terminal = (
                            not retryable or attempts >= METADATA_MISSING_MAX_ATTEMPTS
                        )
                        queue_state = "failed" if terminal else "retry"
                        next_attempt_at = (
                            None if terminal else _metadata_retry_at(attempts)
                        )
                        cursor.execute(
                            "INSERT INTO enrichment_queue(id,entity_id,library_id,kind,locale,priority,state,attempts,next_attempt_at,lease_owner,lease_expires_at,source_job_id,error,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(entity_id,kind,locale) DO UPDATE SET state=excluded.state,priority=MAX(enrichment_queue.priority,excluded.priority),attempts=excluded.attempts,next_attempt_at=excluded.next_attempt_at,lease_owner=NULL,lease_expires_at=NULL,source_job_id=excluded.source_job_id,error=excluded.error,updated_at=excluded.updated_at",
                            (
                                str(uuid.uuid4()),
                                entity_id,
                                library_id,
                                "metadata",
                                locale,
                                10,
                                queue_state,
                                attempts,
                                next_attempt_at,
                                None,
                                None,
                                run_id,
                                json.dumps(locale_failures, ensure_ascii=False),
                                timestamp,
                                timestamp,
                            ),
                        )

        def complete_repair(
            entity_ids: set[str],
            locale: str,
            pending: set[tuple[str, str]] | None = None,
        ) -> None:
            if not entity_ids or not has_enrichment_queue:
                return
            if is_upgrade and pending is not None:
                pending.update((entity_id, locale) for entity_id in entity_ids)
                return
            placeholders = ",".join("?" for _ in entity_ids)
            self.db.execute(
                f"UPDATE enrichment_queue SET state='completed',next_attempt_at=NULL,lease_owner=NULL,lease_expires_at=NULL,error=NULL,updated_at=? "
                f"WHERE kind='metadata' AND locale=? AND entity_id IN ({placeholders})",
                (now(), locale, *sorted(entity_ids)),
            )

        def root_ids(entity_ids: set[str]) -> set[str]:
            roots = set()
            reader = getattr(self.db, "read_execute", self.db.execute)
            for entity_id in entity_ids:
                current = entity_id
                seen_ancestors = set()
                while current and current not in seen_ancestors:
                    seen_ancestors.add(current)
                    try:
                        rows = reader(
                            "SELECT parent_id FROM library_entities WHERE id=?",
                            (current,),
                        )
                    except Exception:
                        # Older/fixture schemas do not carry hierarchy; publish
                        # the linked entity itself in that case.
                        roots.add(current)
                        break
                    parent = rows[0][0] if rows else None
                    if not parent:
                        roots.add(current)
                        break
                    current = parent
            return roots

        def process_upgrade_item(item):
            started = time.monotonic()
            provider, identifier_type, provider_id = item
            entity_type = _metadata_catalog_entity_type(provider, identifier_type)
            neutral = (
                provider == "musicbrainz"
                and entity_type in MUSICBRAINZ_NEUTRAL_ENTITY_TYPES
            )
            provider_locales = [""] if neutral else locales
            cache = ingest.metadata_service.cache
            item_failures = []
            documents: dict[str, dict] = {}
            cached_documents: dict[str, dict] = {}
            pre_gaps: dict[str, set[str]] = {}
            pre_linked: dict[str, list[tuple[str, str]]] = {}
            fetch_locales = []
            failed_locales: set[str] = set()
            changed_locales: set[str] = set()
            repair_locales: set[str] = set()
            state_skipped_locales: set[str] = set()
            missing_locales: set[str] = set()
            state_updates = []
            pending_completions: set[tuple[str, str]] = set()
            publish_ids: set[str] = set()
            upgraded_documents = 0
            changed_documents = 0
            materialized_documents = 0
            skipped_documents = 0
            missing_documents = 0
            incomplete_documents = 0
            failed_documents = 0
            projection_calls = 0
            provider_requests = 0
            provider_elapsed_ms = 0.0

            def clean(document):
                if not isinstance(document, dict):
                    return None
                value = dict(document)
                value.pop("_stale", None)
                return value

            def has_current_state(locale: str, document: dict | None) -> bool:
                digest = _metadata_upgrade_digest(document)
                return bool(
                    upgrade_state_enabled
                    and digest
                    and upgrade_state.get(
                        (provider, entity_type, str(provider_id), locale)
                    )
                    == (METADATA_UPGRADE_VERSION, digest)
                )

            for locale in provider_locales:
                cached = clean(
                    cache.get(provider, entity_type, str(provider_id), locale)
                )
                if cached is None:
                    missing_locales.add(locale)
                    missing_documents += 1
                    item_failures.append(
                        {
                            "kind": "incomplete",
                            "provider": provider,
                            "entityType": entity_type,
                            "providerId": provider_id,
                            "locale": locale,
                            "error": (
                                "Metadata cache document is missing; "
                                "metadata_missing will populate it"
                            ),
                        }
                    )
                    continue
                cached_documents[locale] = cached
                if has_current_state(locale, cached):
                    if neutral:
                        documents = {
                            display_locale: dict(cached) for display_locale in locales
                        }
                    else:
                        documents[locale] = cached
                    state_skipped_locales.add(locale)
                    continue
                if neutral:
                    documents = {
                        display_locale: dict(cached) for display_locale in locales
                    }
                else:
                    documents[locale] = cached
                fetch_locales.append(locale)

            if neutral and missing_documents:
                # A neutral provider identity is one document even though it
                # is rendered into every configured catalog locale.
                missing_documents = 1
                state_skipped_locales.clear()
                fetch_locales.clear()
            elif not neutral:
                missing_documents = min(missing_documents, len(locales))
                incomplete_documents = missing_documents

            if fetch_locales:
                provider_started = time.monotonic()
                provider_requests = 1
                try:
                    fetched = _fetch_upgrade_documents(
                        ingest,
                        provider,
                        entity_type,
                        provider_id,
                        fetch_locales,
                    )
                    if neutral:
                        normalized = clean(
                            fetched.get(locales[0]) if locales else fetched.get("")
                        )
                        if normalized is not None:
                            documents = {
                                display_locale: dict(normalized)
                                for display_locale in locales
                            }
                    else:
                        documents.update(
                            {
                                locale: clean(document)
                                for locale, document in fetched.items()
                            }
                        )
                    if neutral:
                        previous = cached_documents.get("")
                        if previous is not None and any(
                            _metadata_upgrade_needed(
                                previous,
                                documents.get(locale),
                                locale,
                                provider,
                            )
                            for locale in locales
                        ):
                            changed_documents = 1
                            changed_locales.update(locales)
                    else:
                        for locale in fetch_locales:
                            fresh = documents.get(locale)
                            if _metadata_upgrade_needed(
                                cached_documents.get(locale),
                                fresh,
                                locale,
                                provider,
                            ):
                                changed_documents += 1
                                changed_locales.add(locale)
                except (ProviderError, ValueError, OSError) as error:
                    failed_locales.update(fetch_locales)
                    failed_documents = len(set(fetch_locales))
                    item_failures.extend(
                        {
                            "kind": "error",
                            "provider": provider,
                            "entityType": entity_type,
                            "providerId": provider_id,
                            "locale": locale,
                            "error": f"{type(error).__name__}: {error}",
                        }
                        for locale in fetch_locales
                    )
                    logger.exception(
                        "metadata upgrade failed provider=%s entity_type=%s provider_id=%s locales=%s",
                        provider,
                        entity_type,
                        provider_id,
                        fetch_locales,
                    )
                finally:
                    provider_elapsed_ms = (
                        max(0.0, time.monotonic() - provider_started) * 1000.0
                    )

            if neutral:
                neutral_locale = ""
                if neutral_locale in failed_locales:
                    incomplete_documents = 0
                elif missing_documents:
                    incomplete_documents = 1
                else:
                    document = documents.get(locales[0]) if locales else None
                    if isinstance(document, dict):
                        for locale in locales:
                            gaps, linked = _metadata_document_gaps(
                                self.db,
                                provider,
                                entity_type,
                                provider_id,
                                locale,
                                document,
                            )
                            pre_gaps[locale] = gaps
                            pre_linked[locale] = linked
                            if gaps:
                                repair_locales.add(locale)
                        needs_materialization = bool(changed_locales or repair_locales)
                        if needs_materialization:
                            ingest.ingest_document(
                                provider,
                                entity_type,
                                provider_id,
                                neutral_locale,
                                document,
                                force_assets=False,
                                reproject_assets=False,
                            )
                            materialized_documents = 1
                            projection_calls = len(locales)
                        final_gaps = set()
                        final_linked: list[tuple[str, str]] = []
                        for locale in locales:
                            if materialized_documents:
                                gaps, linked = _metadata_document_gaps(
                                    self.db,
                                    provider,
                                    entity_type,
                                    provider_id,
                                    locale,
                                    document,
                                )
                            else:
                                gaps = pre_gaps.get(locale, set())
                                linked = pre_linked.get(locale, [])
                            final_gaps.update(gaps)
                            final_linked.extend(linked)
                        linked_ids = {
                            entity_id for entity_id, _library_id in final_linked
                        }
                        if materialized_documents:
                            pending_completions.update(
                                (entity_id, "") for entity_id in linked_ids
                            )
                            publish_ids.update(linked_ids)
                        deferred_projection_gaps = {
                            gap
                            for gap in final_gaps
                            if gap == "projection" or gap.startswith("projection-")
                        }
                        unresolved_gaps = final_gaps - (
                            deferred_projection_gaps
                            if materialized_documents and linked_ids
                            else set()
                        )
                        if unresolved_gaps:
                            incomplete_documents = 1
                            item_failures.append(
                                {
                                    "kind": "incomplete",
                                    "provider": provider,
                                    "entityType": entity_type,
                                    "providerId": provider_id,
                                    "locale": neutral_locale,
                                    "missing": sorted(unresolved_gaps),
                                    "error": "Metadata materialization remains incomplete",
                                }
                            )
                        else:
                            if changed_documents and not unresolved_gaps:
                                upgraded_documents = 1
                            if upgrade_state_enabled and not (
                                neutral_locale in state_skipped_locales
                                and not materialized_documents
                            ):
                                state_updates.append(
                                    (
                                        provider,
                                        entity_type,
                                        provider_id,
                                        neutral_locale,
                                        _metadata_upgrade_digest(document),
                                        bool(materialized_documents),
                                    )
                                )
                            if not materialized_documents:
                                skipped_documents = len(state_skipped_locales)
                    else:
                        incomplete_documents = 1
                        item_failures.append(
                            {
                                "kind": "incomplete",
                                "provider": provider,
                                "entityType": entity_type,
                                "providerId": provider_id,
                                "locale": neutral_locale,
                                "error": "Metadata document is still missing after repair",
                            }
                        )
            else:
                for locale in locales:
                    if locale in failed_locales or locale in missing_locales:
                        continue
                    document = documents.get(locale)
                    if not isinstance(document, dict):
                        incomplete_documents += 1
                        item_failures.append(
                            {
                                "kind": "incomplete",
                                "provider": provider,
                                "entityType": entity_type,
                                "providerId": provider_id,
                                "locale": locale,
                                "error": "Metadata document is still missing after repair",
                            }
                        )
                        continue
                    gaps, linked = _metadata_document_gaps(
                        self.db,
                        provider,
                        entity_type,
                        provider_id,
                        locale,
                        document,
                    )
                    needs_materialization = locale in changed_locales or bool(gaps)
                    if needs_materialization:
                        ingest.ingest_document(
                            provider,
                            entity_type,
                            provider_id,
                            locale,
                            document,
                            force_assets=False,
                            reproject_assets=False,
                        )
                        materialized_documents += 1
                        projection_calls += 1
                        gaps, linked = _metadata_document_gaps(
                            self.db,
                            provider,
                            entity_type,
                            provider_id,
                            locale,
                            document,
                        )
                    linked_ids = {entity_id for entity_id, _library_id in linked}
                    if needs_materialization:
                        pending_completions.update(
                            (entity_id, locale) for entity_id in linked_ids
                        )
                        publish_ids.update(linked_ids)
                    deferred_projection_gaps = {
                        gap
                        for gap in gaps
                        if gap == "projection" or gap.startswith("projection-")
                    }
                    unresolved_gaps = gaps - (
                        deferred_projection_gaps
                        if needs_materialization and linked_ids
                        else set()
                    )
                    if unresolved_gaps:
                        incomplete_documents += 1
                        item_failures.append(
                            {
                                "kind": "incomplete",
                                "provider": provider,
                                "entityType": entity_type,
                                "providerId": provider_id,
                                "locale": locale,
                                "missing": sorted(unresolved_gaps),
                                "error": "Metadata materialization remains incomplete",
                            }
                        )
                        continue
                    if locale in changed_locales:
                        upgraded_documents += 1
                    if upgrade_state_enabled and locale not in state_skipped_locales:
                        state_updates.append(
                            (
                                provider,
                                entity_type,
                                provider_id,
                                locale,
                                _metadata_upgrade_digest(document),
                                needs_materialization,
                            )
                        )
                    if locale in state_skipped_locales and not needs_materialization:
                        skipped_documents += 1

            queue_failures(provider, entity_type, provider_id, item_failures)
            roots = root_ids(publish_ids) if materialized_documents else set()
            return {
                "processed": 1 if neutral else len(locales),
                "failures": item_failures,
                "upgraded": upgraded_documents,
                "changed": changed_documents,
                "skipped": skipped_documents,
                "missing": missing_documents,
                "incomplete": incomplete_documents,
                "failed": failed_documents,
                "materialized": materialized_documents,
                "projection_calls": projection_calls,
                "provider_requests": provider_requests,
                "provider_elapsed_ms": provider_elapsed_ms,
                "worker_elapsed_ms": max(0.0, time.monotonic() - started) * 1000.0,
                "roots": roots,
                "state_updates": state_updates,
                "completions": pending_completions,
            }

        def process_item(item):
            if is_upgrade:
                return process_upgrade_item(item)
            provider, identifier_type, provider_id = item
            entity_type = _metadata_catalog_entity_type(provider, identifier_type)
            neutral = (
                provider == "musicbrainz"
                and entity_type in MUSICBRAINZ_NEUTRAL_ENTITY_TYPES
            )
            requested_locales = (
                locales_by_item.get(item) if locales_by_item is not None else None
            )
            provider_locales = [""] if neutral else list(requested_locales or locales)
            output_locales = locales if neutral else provider_locales
            item_failures = []
            fetch_locales = []
            documents: dict[str, dict] = {}
            worked_locales: set[str] = set()
            if neutral:
                provider_locale = ""
                cached = ingest.metadata_service.cache.get(
                    provider, entity_type, provider_id, provider_locale
                )
                if not cached or force:
                    fetch_locales.append(provider_locale)
                else:
                    cached = dict(cached)
                    cached.pop("_stale", None)
                    documents = {locale: dict(cached) for locale in locales}
                    gaps, _linked = _metadata_document_gaps(
                        self.db,
                        provider,
                        entity_type,
                        provider_id,
                        locales[0],
                        cached,
                    )
                    if gaps:
                        fetch_locales.append(provider_locale)
                    else:
                        ingest.ingest_document(
                            provider,
                            entity_type,
                            provider_id,
                            provider_locale,
                            cached,
                        )
                        worked_locales.add(provider_locale)
            else:
                for locale in provider_locales:
                    cached = ingest.metadata_service.cache.get(
                        provider, entity_type, provider_id, locale
                    )
                    if not cached:
                        fetch_locales.append(locale)
                        continue
                    cached = dict(cached)
                    cached.pop("_stale", None)
                    documents[locale] = cached
                    if force:
                        fetch_locales.append(locale)
                        continue
                    gaps, _linked = _metadata_document_gaps(
                        self.db,
                        provider,
                        entity_type,
                        provider_id,
                        locale,
                        cached,
                    )
                    if gaps:
                        # A cache hit is normally replayed locally. A missing
                        # provider title is different: replaying the same
                        # normalized document can never repair it, so request
                        # a fresh localized document instead.
                        if (
                            provider == "tvdb"
                            and entity_type == "season"
                            and not _usable_metadata_value(cached.get("title"))
                        ):
                            fetch_locales.append(locale)
                        else:
                            ingest.ingest_document(
                                provider, entity_type, provider_id, locale, cached
                            )
                            worked_locales.add(locale)
            if fetch_locales:
                try:
                    fetched = ingest.ingest_locales(
                        provider,
                        entity_type,
                        provider_id,
                        fetch_locales,
                        force=force,
                        force_assets=force_assets,
                    )
                    documents.update(fetched)
                    worked_locales.update(fetch_locales)
                except (ProviderError, ValueError, OSError) as error:
                    item_failures.extend(
                        {
                            "kind": "error",
                            "provider": provider,
                            "entityType": entity_type,
                            "providerId": provider_id,
                            "locale": "" if neutral else locale,
                            "retryable": not isinstance(error, ProviderNotFoundError),
                            "error": f"{type(error).__name__}: {error}",
                        }
                        for locale in fetch_locales
                    )
                    logger.exception(
                        "scheduled missing metadata failed provider=%s entity_type=%s provider_id=%s locales=%s",
                        provider,
                        entity_type,
                        provider_id,
                        fetch_locales,
                    )
            failed_locales = {str(failure.get("locale")) for failure in item_failures}
            publish_ids: set[str] = set()
            for locale in output_locales:
                if ("" if neutral else locale) in failed_locales:
                    continue
                document = documents.get(locale)
                if not isinstance(document, dict):
                    item_failures.append(
                        {
                            "kind": "incomplete",
                            "provider": provider,
                            "entityType": entity_type,
                            "providerId": provider_id,
                            "locale": "" if neutral else locale,
                            "error": "Metadata document is still missing after repair",
                        }
                    )
                    continue
                document = dict(document)
                document.pop("_stale", None)
                gaps, linked = _metadata_document_gaps(
                    self.db,
                    provider,
                    entity_type,
                    provider_id,
                    locale,
                    document,
                )
                linked_ids = {entity_id for entity_id, _library_id in linked}
                publish_ids.update(linked_ids)
                if gaps:
                    item_failures.append(
                        {
                            "kind": "incomplete",
                            "provider": provider,
                            "entityType": entity_type,
                            "providerId": provider_id,
                            "locale": "" if neutral else locale,
                            "missing": sorted(gaps),
                            "error": "Metadata materialization remains incomplete",
                        }
                    )
                else:
                    complete_repair(linked_ids, "" if neutral else locale)
                    if operation == "metadata_missing":
                        _complete_metadata_recovery_state(
                            self.db,
                            provider,
                            entity_type,
                            provider_id,
                            locale="" if neutral else locale,
                            source_job_id=run_id,
                        )
            queue_failures(provider, entity_type, provider_id, item_failures)
            if worked_locales and publish_ids:
                from app.catalog_read_model import CatalogReadModel

                # Repairs may be linked to an episode/season.  Publication is
                # rooted at the top-level movie/series/collection so dependent
                # projections are refreshed once per root.
                roots = set()
                reader = getattr(self.db, "read_execute", self.db.execute)
                for entity_id in publish_ids:
                    current = entity_id
                    seen_ancestors = set()
                    while current and current not in seen_ancestors:
                        seen_ancestors.add(current)
                        try:
                            rows = reader(
                                "SELECT parent_id FROM library_entities WHERE id=?",
                                (current,),
                            )
                        except Exception:
                            # Older/fixture schemas do not carry hierarchy;
                            # publish the linked entity itself in that case.
                            roots.add(current)
                            break
                        parent = rows[0][0] if rows else None
                        if not parent:
                            roots.add(current)
                            break
                        current = parent
                CatalogReadModel(self.db).refresh_roots(sorted(roots))
            upgraded_documents = len(worked_locales)
            return (1 if neutral else len(locales)), item_failures, upgraded_documents

        completed = 0
        repaired = 0
        failures = list(identity_failures)
        incomplete_repairs = list(identity_incomplete)
        upgrade_stats = {
            "changed": 0,
            "skipped": 0,
            "missing": 0,
            "incomplete": 0,
            "failed": 0,
            "materialized": 0,
            "projection_calls": 0,
            "provider_requests": 0,
            "provider_elapsed_ms": 0.0,
            "worker_elapsed_ms": 0.0,
            "root_refresh_calls": 0,
            "root_refresh_roots": 0,
            "state_writes": 0,
            "completion_writes": 0,
        }

        def upgrade_scan_stats() -> dict:
            metrics_after = (
                self.db.metrics() if callable(getattr(self.db, "metrics", None)) else {}
            )

            def metric_delta(name: str) -> float:
                try:
                    return max(
                        0.0,
                        float(metrics_after.get(name, 0))
                        - float(metrics_before.get(name, 0)),
                    )
                except (AttributeError, TypeError, ValueError):
                    return 0.0

            changed = upgrade_stats["changed"]
            incomplete = upgrade_stats["incomplete"]
            failed = upgrade_stats["failed"]
            unchanged = max(0, completed - repaired - incomplete - failed)
            return {
                "totalDocuments": total,
                "checkedDocuments": completed,
                "changedDocuments": changed,
                "unchangedDocuments": unchanged,
                "skippedDocuments": upgrade_stats["skipped"],
                "missingDocuments": upgrade_stats["missing"],
                "incompleteDocuments": incomplete,
                "failedDocuments": failed,
                "materializedDocuments": upgrade_stats["materialized"],
                "providerRequests": upgrade_stats["provider_requests"],
                "providerElapsedMs": round(upgrade_stats["provider_elapsed_ms"], 3),
                "workerElapsedMs": round(upgrade_stats["worker_elapsed_ms"], 3),
                "projectionCalls": upgrade_stats["projection_calls"],
                "rootRefreshCalls": upgrade_stats["root_refresh_calls"],
                "rootRefreshRoots": upgrade_stats["root_refresh_roots"],
                "stateWrites": upgrade_stats["state_writes"],
                "completionWrites": upgrade_stats["completion_writes"],
                "writerOperations": int(metric_delta("writer_operations")),
                "commitCount": int(metric_delta("commit_count")),
                "writerWaitMs": round(metric_delta("writer_wait_seconds") * 1000, 3),
                "writerHoldMs": round(metric_delta("writer_hold_seconds") * 1000, 3),
                "wallClockMs": round(
                    max(0.0, time.monotonic() - job_started) * 1000, 3
                ),
            }

        def complete_repairs_batch(pending: set[tuple[str, str]]) -> int:
            if not pending or not has_enrichment_queue:
                return 0
            timestamp = now()
            with self.db.transaction() as cursor:
                cursor.executemany(
                    "UPDATE enrichment_queue SET state='completed',next_attempt_at=NULL,lease_owner=NULL,lease_expires_at=NULL,error=NULL,updated_at=? "
                    "WHERE kind='metadata' AND locale=? AND entity_id=?",
                    [
                        (timestamp, locale, entity_id)
                        for entity_id, locale in sorted(pending)
                    ],
                )
            return len(pending)

        for offset in range(0, len(items), batch_size):
            batch = items[offset : offset + batch_size]
            batch_roots: set[str] = set()
            batch_state_updates = []
            batch_completions: set[tuple[str, str]] = set()
            last_item_label = None
            for item, result, error in metadata_task_results(
                batch, process_item, should_terminate
            ):
                if error is not None:
                    raise error
                if is_upgrade:
                    processed = result["processed"]
                    item_failures = result["failures"]
                    repaired_documents = result["upgraded"]
                    batch_roots.update(result["roots"])
                    batch_state_updates.extend(result["state_updates"])
                    batch_completions.update(result["completions"])
                    for key in (
                        "changed",
                        "skipped",
                        "missing",
                        "incomplete",
                        "failed",
                        "materialized",
                        "projection_calls",
                        "provider_requests",
                    ):
                        upgrade_stats[key] += result[key]
                    for key in ("provider_elapsed_ms", "worker_elapsed_ms"):
                        upgrade_stats[key] += result[key]
                else:
                    processed, item_failures, repaired_documents = result
                failures.extend(
                    failure
                    for failure in item_failures
                    if failure.get("kind") != "incomplete"
                )
                incomplete_repairs.extend(
                    failure
                    for failure in item_failures
                    if failure.get("kind") == "incomplete"
                )
                completed += processed
                repaired += repaired_documents
                provider, entity_type, provider_id = item
                entity_rows = self.db.execute(
                    "SELECT entity_id FROM entity_provider_ids WHERE provider=? AND identifier_type=? AND provider_id=? ORDER BY entity_id LIMIT 1",
                    (provider, entity_type, provider_id),
                )
                item_label = resolve_progress_item(
                    self.db,
                    entity_rows[0][0] if entity_rows else None,
                    f"{entity_type} {provider}:{provider_id}",
                )
                last_item_label = item_label
                if is_upgrade:
                    continue
                self.store.update_run(
                    run_id,
                    progress_current=completed,
                    progress_phase="metadata",
                    progress_label=(
                        "Upgrading metadata" if is_upgrade else "Refreshing metadata"
                    ),
                    progress_stage_current=completed,
                    progress_stage_total=total + extractor_total,
                    progress_stage_unit="documents",
                    progress_current_item=item_label,
                    message=(
                        format_progress_message(
                            (
                                "Upgrading metadata"
                                if is_upgrade
                                else "Refreshing metadata"
                            ),
                            item=item_label,
                            current=completed,
                            total=total + extractor_total,
                            unit="documents",
                        )
                    ),
                )
            if is_upgrade:
                projection_error = None
                if batch_roots:
                    try:
                        from app.catalog_read_model import CatalogReadModel

                        CatalogReadModel(self.db).refresh_roots(sorted(batch_roots))
                        upgrade_stats["root_refresh_calls"] += 1
                        upgrade_stats["root_refresh_roots"] += len(batch_roots)
                    except Exception as error:
                        projection_error = error
                        failures.append(
                            {
                                "kind": "projection",
                                "error": f"{type(error).__name__}: {error}",
                                "roots": sorted(batch_roots),
                            }
                        )
                        upgrade_stats["failed"] += 1
                        logger.exception(
                            "metadata upgrade catalog refresh failed roots=%s",
                            sorted(batch_roots),
                        )
                if projection_error is None:
                    upgrade_stats["completion_writes"] += complete_repairs_batch(
                        batch_completions
                    )
                state_rows = [
                    row[:5]
                    for row in batch_state_updates
                    if projection_error is None or not row[5]
                ]
                upgrade_stats["state_writes"] += _persist_metadata_upgrade_state(
                    self.db, state_rows
                )
                self.store.update_run(
                    run_id,
                    progress_current=completed,
                    progress_phase="metadata",
                    progress_label="Upgrading metadata",
                    progress_stage_current=completed,
                    progress_stage_total=total + extractor_total,
                    progress_stage_unit="documents",
                    progress_current_item=last_item_label,
                    message=format_progress_message(
                        "Upgrading metadata",
                        item=last_item_label,
                        current=completed,
                        total=total + extractor_total,
                        unit="documents",
                    ),
                    scan_stats=json.dumps(upgrade_scan_stats(), ensure_ascii=False),
                )
        # Screen Extractor is a final, language-neutral fallback. Run it only
        # after every real provider identity has been processed so a secondary
        # TMDB/TVDB Primary can displace a generated frame in the same pass.
        extractor_failures = []
        artwork_entity_ids: set[str] = set()
        try:
            from app.metadata_services import reproject_entity_artwork
            from app.screen_extractor import extract_entity

            for extractor_index, (entity_id, entity_type) in enumerate(
                extractor_rows, start=1
            ):
                if should_terminate():
                    raise JobTerminated()
                try:
                    extract_entity(
                        self.db,
                        entity_id,
                        entity_type,
                        force=False,
                        should_terminate=should_terminate,
                    )
                    reproject_entity_artwork(self.db, entity_id, locales)
                    artwork_entity_ids.add(entity_id)
                    self.store.update_run(
                        run_id,
                        progress_current=total + extractor_index,
                        progress_total=total + extractor_total,
                        progress_phase="artwork",
                        progress_label="Extracting fallback artwork",
                        progress_stage_current=total + extractor_index,
                        progress_stage_total=total + extractor_total,
                        progress_stage_unit="documents",
                        progress_current_item=resolve_progress_item(
                            self.db, entity_id, entity_id
                        ),
                        message=(
                            format_progress_message(
                                "Extracting fallback artwork",
                                item=resolve_progress_item(
                                    self.db, entity_id, entity_id
                                ),
                                current=total + extractor_index,
                                total=total + extractor_total,
                                unit="documents",
                            )
                        ),
                    )
                except Exception as error:
                    extractor_failures.append(
                        {
                            "entityId": entity_id,
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
        except JobTerminated:
            raise
        except Exception as error:
            extractor_failures.append({"error": f"{type(error).__name__}: {error}"})
        if extractor_failures:
            incomplete_repairs.extend(
                {"kind": "screen_extractor", **failure}
                for failure in extractor_failures
            )
        try:
            from app.catalog_read_model import CatalogReadModel

            roots = set()
            for entity_id in artwork_entity_ids:
                current = entity_id
                seen = set()
                while current and current not in seen:
                    seen.add(current)
                    rows = self.db.execute(
                        "SELECT parent_id FROM library_entities WHERE id=?",
                        (current,),
                    )
                    parent = rows[0][0] if rows else None
                    if not parent:
                        roots.add(current)
                        break
                    current = parent
            if roots:
                CatalogReadModel(self.db).refresh_roots(sorted(roots))
        except Exception:
            logger.exception("screen extractor catalog refresh failed")
        try:
            # Metadata refreshes can discover new MusicBrainz track credits
            # without traversing the media filesystem. Rebuild the durable
            # artist entities/links from the documents just materialized so
            # those credits are immediately navigable.
            repair_store = LibraryStore.__new__(LibraryStore)
            repair_store.db = self.db
            repair_store._progress = {}
            LibraryScanner(repair_store).repair_music_artist_credits(
                ingest, run_id, should_terminate
            )
        except JobTerminated:
            raise
        except Exception:
            logger.exception("music artist credit repair failed")
        if should_terminate():
            raise JobTerminated()
        final_scan_stats = upgrade_scan_stats() if is_upgrade else None
        if failures:
            if is_upgrade:
                unchanged = final_scan_stats["unchangedDocuments"]
                summary = (
                    f"Checked {completed} metadata documents; upgraded {repaired}; "
                    f"unchanged {unchanged}; "
                    f"{final_scan_stats['incompleteDocuments']} incomplete; "
                    f"{final_scan_stats['failedDocuments']} failed"
                )
            else:
                summary = (
                    f"Checked {completed} metadata documents; repaired {repaired}; "
                    f"{len(failures)} repair errors"
                )
            if incomplete_repairs and not is_upgrade:
                summary += f"; {len(incomplete_repairs)} repairs remain incomplete"
            details = {
                "operation": operation,
                "checked": completed,
                "upgraded": repaired if is_upgrade else 0,
                "unchanged": final_scan_stats["unchangedDocuments"]
                if is_upgrade
                else 0,
                "incomplete": final_scan_stats["incompleteDocuments"]
                if is_upgrade
                else len(incomplete_repairs),
                "failed": final_scan_stats["failedDocuments"]
                if is_upgrade
                else len(failures),
                "failures": failures,
                "incompleteRepairs": incomplete_repairs,
            }
            terminal_fields = {
                "state": "failed",
                "progress_current": completed,
                "progress_total": total + extractor_total,
                "finished_at": now(),
                "message": summary,
                "error": summary,
                "error_details": json.dumps(details),
            }
            if is_upgrade:
                details["scanStats"] = final_scan_stats
                terminal_fields["error_details"] = json.dumps(details)
                terminal_fields["scan_stats"] = json.dumps(
                    final_scan_stats, ensure_ascii=False
                )
            self.store.update_run(
                run_id,
                **terminal_fields,
            )
        else:
            if is_upgrade:
                unchanged = final_scan_stats["unchangedDocuments"]
                summary = (
                    f"Checked {completed} metadata documents; upgraded {repaired}; "
                    f"unchanged {unchanged}; "
                    f"{final_scan_stats['incompleteDocuments']} incomplete; "
                    "0 failed"
                )
            else:
                summary = (
                    f"Checked {completed} metadata documents; repaired {repaired} "
                    "missing or partial documents"
                )
            if incomplete_repairs:
                if not is_upgrade:
                    summary += f"; {len(incomplete_repairs)} repairs remain incomplete"
            details = None
            terminal_fields = {
                "state": "completed",
                "progress_current": completed,
                "progress_total": total + extractor_total,
                "finished_at": now(),
                "message": summary,
            }
            if is_upgrade:
                details = {
                    "operation": operation,
                    "checked": completed,
                    "upgraded": repaired,
                    "unchanged": final_scan_stats["unchangedDocuments"],
                    "incomplete": final_scan_stats["incompleteDocuments"],
                    "failed": 0,
                    "scanStats": final_scan_stats,
                }
                terminal_fields["error_details"] = json.dumps(details)
                terminal_fields["scan_stats"] = json.dumps(
                    final_scan_stats, ensure_ascii=False
                )
            else:
                terminal_fields["error_details"] = None
            self.store.update_run(
                run_id,
                **terminal_fields,
            )


class MetadataUpgradeJob(MetadataMissingJob):
    """Refetch existing provider metadata and apply only real improvements."""

    def run(
        self,
        run_id: str,
        definition: dict,
        should_terminate=None,
    ) -> None:
        return super().run(
            run_id,
            definition,
            should_terminate,
            force=True,
            force_assets=False,
            operation="metadata_upgrade",
        )


class MusicCatalogRepairJob:
    """Run the versioned deterministic music inventory repair once."""

    REPAIR_VERSION = 1

    def __init__(self, store: JobStore, library_runtime=None):
        self.store = store
        self.db = store.db
        self.library_runtime = library_runtime or globals().get("library_runtime")

    def run(self, run_id: str, definition: dict, should_terminate=None) -> None:
        should_terminate = should_terminate or (lambda: False)
        libraries = self.db.execute(
            "SELECT id,name FROM libraries WHERE type='music' ORDER BY id"
        )
        total = len(libraries)
        self.store.update_run(
            run_id,
            progress_current=0,
            progress_total=max(1, total),
            progress_phase="inventory",
            progress_label="Repairing music catalog identities",
            progress_stage_current=0,
            progress_stage_total=max(1, total),
            progress_stage_unit="libraries",
            message=f"Repairing music catalog identities · 0/{total} libraries",
            thread_name=threading.current_thread().name,
        )
        runtime = self.library_runtime
        if runtime is None:
            raise RuntimeError("Library runtime is unavailable for music repair")
        if not getattr(getattr(runtime, "thread", None), "is_alive", lambda: False)():
            runtime.start()
        repaired = 0
        failed: list[dict] = []
        suppressed: list[str] = []
        try:
            for index, (library_id, name) in enumerate(libraries, start=1):
                if should_terminate():
                    raise JobTerminated()
                runtime.suppress_library_notifications(str(library_id), True)
                suppressed.append(str(library_id))
                job = runtime.enqueue(str(library_id), "scan")
                if not job:
                    raise RuntimeError(f"Music library {library_id} is unavailable")
                result = runtime.wait_for_job(
                    job["id"], should_terminate=should_terminate
                )
                if not result or result.get("state") not in {
                    "completed",
                    "completed_with_warnings",
                }:
                    failed.append(
                        {
                            "libraryId": library_id,
                            "name": name,
                            "jobId": job["id"],
                            "state": result.get("state") if result else "missing",
                            "error": result.get("error") if result else None,
                        }
                    )
                    continue
                repaired += 1
                repair_music_track_contexts(self.db, str(library_id), should_terminate)
                self.store.update_run(
                    run_id,
                    progress_current=index,
                    progress_total=max(1, total),
                    progress_phase="inventory",
                    progress_label="Repairing music catalog identities",
                    progress_stage_current=index,
                    progress_stage_total=max(1, total),
                    progress_stage_unit="libraries",
                    message=(f"Repaired music library {name} · {index}/{total}"),
                )
            if failed:
                failure_time = now()
                retry_at = (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat()
                failure_message = (
                    f"Music catalog repair failed for {len(failed)} library(s)"
                )
                self.store.update_run(
                    run_id,
                    state="failed",
                    progress_current=repaired,
                    progress_total=max(1, total),
                    error="One or more music libraries could not be repaired.",
                    error_details=json.dumps(
                        {
                            "repairVersion": self.REPAIR_VERSION,
                            "repairedLibraries": repaired,
                            "failed": failed,
                        },
                        ensure_ascii=False,
                    ),
                    finished_at=failure_time,
                    message=failure_message,
                )
                # Keep the one-time repair eligible for a bounded retry. The
                # repair definition deliberately has no recurring trigger;
                # last_run_at remains NULL until every music library succeeds.
                self.db.execute(
                    "UPDATE job_definitions SET next_run_at=?,last_state='failed',last_message=?,updated_at=? WHERE id=? AND last_run_at IS NULL",
                    (retry_at, failure_message, failure_time, definition["id"]),
                )
                return
            finished = now()
            message = f"Repaired {repaired}/{total} music libraries"
            self.store.update_run(
                run_id,
                state="completed",
                progress_current=max(1, total),
                progress_total=max(1, total),
                finished_at=finished,
                message=message,
                error_details=json.dumps(
                    {
                        "repairVersion": self.REPAIR_VERSION,
                        "repairedLibraries": repaired,
                    },
                    ensure_ascii=False,
                ),
            )
            self.db.execute(
                "UPDATE job_definitions SET next_run_at=NULL,last_run_at=?,last_run_id=?,last_state='completed',last_message=?,updated_at=? WHERE id=?",
                (finished, run_id, message, finished, definition["id"]),
            )
        finally:
            for library_id in suppressed:
                runtime.suppress_library_notifications(library_id, False)


class MetadataCleanupJob:
    def __init__(self, store: JobStore):
        self.store = store
        self.db = store.db

    def run(self, run_id: str, definition: dict, should_terminate=None) -> None:
        should_terminate = should_terminate or (lambda: False)
        if should_terminate():
            raise JobTerminated()
        self.store.update_run(
            run_id,
            state="running",
            started_at=now(),
            progress_total=1,
            progress_phase="preparation",
            progress_label="Cleaning orphaned data",
            progress_stage_current=0,
            progress_stage_total=7,
            progress_stage_unit="stages",
            message="Cleaning orphaned data · 0/7 stages",
            thread_name=threading.current_thread().name,
        )
        completed = 0

        def progress(_phase, label):
            nonlocal completed
            completed += 1
            self.store.update_run(
                run_id,
                progress_current=completed,
                progress_total=7,
                progress_phase="cleanup",
                progress_label=label,
                progress_stage_current=completed,
                progress_stage_total=7,
                progress_stage_unit="stages",
                message=f"{label} · {completed}/7 stages",
            )

        if not cleanup_orphans(
            self.db,
            progress=progress,
            should_terminate=should_terminate,
        ):
            raise JobTerminated()
        self.store.update_run(
            run_id,
            state="completed",
            progress_current=1,
            progress_total=1,
            finished_at=now(),
            message="Orphaned library data cleaned",
        )


class BazarrSyncJob:
    def __init__(self, store: JobStore):
        self.store = store

    def run(self, run_id: str, should_terminate=None) -> None:
        should_terminate = should_terminate or (lambda: False)
        self.store.update_run(
            run_id,
            state="running",
            started_at=now(),
            progress_current=0,
            progress_total=1,
            progress_phase="preparation",
            progress_label="Preparing Bazarr mapping",
            progress_stage_current=0,
            progress_stage_total=1,
            progress_stage_unit="stages",
            message="Preparing Bazarr mapping",
            thread_name=threading.current_thread().name,
        )

        def progress(current: int, total: int) -> None:
            total = max(1, int(total))
            self.store.update_run(
                run_id,
                progress_current=current,
                progress_total=total,
                progress_phase="mapping",
                progress_label="Synchronizing Bazarr mappings",
                progress_stage_current=current,
                progress_stage_total=total,
                progress_stage_unit="entries",
                message=f"Synchronizing Bazarr mappings · {current}/{total} entries",
            )

        from app.bazarr import BazarrSyncService

        result = BazarrSyncService(self.store.db).sync(
            should_terminate=should_terminate,
            progress=progress,
        )
        if result.get("skipped"):
            message = "Bazarr is not configured"
        else:
            message = (
                f"Mapped {result.get('matched', 0)}/{result.get('episodes', 0)} "
                f"episodes across {result.get('matched_series', 0)}/"
                f"{result.get('series', 0)} series; "
                f"{result.get('matched_movies', 0)}/{result.get('movies', 0)} movies"
            )
            deferred = int(result.get("deferred_series", 0) or 0)
            if deferred:
                message += f"; deferred {deferred} series"
            deferred_movies = int(result.get("deferred_movies", 0) or 0)
            if deferred_movies:
                message += f"; deferred {deferred_movies} movies"
        self.store.update_run(
            run_id,
            state="completed",
            progress_current=1,
            progress_total=1,
            finished_at=now(),
            message=message,
        )


class JobScheduler:
    """Dispatches every scheduled run on its own worker thread."""

    def __init__(self, library_runtime):
        self.store = JobStore()
        self.library_runtime = library_runtime
        self.catalog_work_coordinator = CatalogWorkCoordinator()
        set_coordinator = getattr(library_runtime, "set_catalog_work_coordinator", None)
        if callable(set_coordinator):
            set_coordinator(self.catalog_work_coordinator)
        set_status_callback = getattr(library_runtime, "set_job_status_callback", None)
        if callable(set_status_callback):
            set_status_callback(self._on_library_job_status)
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.active: set[str] = set()
        self.active_definitions: set[str] = set()
        self.cancel_events: dict[str, threading.Event] = {}
        self.worker_threads: dict[str, threading.Thread] = {}
        self.active_lock = threading.RLock()
        self.analysis_maintenance: set[str] = set()
        self._dispatch_last_success: float | None = None
        self._dispatch_last_error: str | None = None
        self._dispatch_consecutive_failures = 0

    def _on_library_job_status(self, job: dict | None) -> None:
        """Mirror full library-job state onto its scheduler definition."""
        if not job or job.get("kind") not in {"scan", "collection_rebuild"}:
            return
        library_id = str(job.get("libraryId") or "")
        if not library_id:
            return
        rows = self.store.db.execute(
            "SELECT id,config FROM job_definitions WHERE kind='library_scan'"
        )
        definition_ids = []
        for definition_id, config_text in rows:
            try:
                config = json.loads(config_text or "{}")
            except (TypeError, json.JSONDecodeError):
                config = {}
            if str((config or {}).get("libraryId") or "") == library_id:
                definition_ids.append(definition_id)
        if not definition_ids:
            return
        columns = {
            row[1]
            for row in self.store.db.execute("PRAGMA table_info(job_definitions)")
        }
        values = {
            "last_state": job.get("state"),
            "last_message": job.get("message") or job.get("error"),
            "updated_at": now(),
        }
        if "last_run_id" in columns:
            values["last_run_id"] = job.get("id")
        if "last_run_at" in columns:
            values["last_run_at"] = job.get("startedAt") or job.get("createdAt")
        fields = [key for key in values if key in columns]
        if not fields:
            return
        for definition_id in definition_ids:
            self.store.db.execute(
                "UPDATE job_definitions SET "
                + ",".join(f"{key}=?" for key in fields)
                + " WHERE id=?",
                [values[key] for key in fields] + [definition_id],
            )

    def _sync_library_definitions(self) -> None:
        """Repair library definition pointers after startup/recovery."""
        runtime_store = getattr(self.library_runtime, "store", None)
        if runtime_store is None:
            return
        try:
            definitions = self.store.db.execute(
                "SELECT id,config FROM job_definitions WHERE kind='library_scan'"
            )
        except Exception:
            return
        for _definition_id, config_text in definitions:
            try:
                library_id = str(
                    (json.loads(config_text or "{}") or {}).get("libraryId") or ""
                )
            except (TypeError, json.JSONDecodeError):
                continue
            if not library_id:
                continue
            rows = runtime_store.db.execute(
                "SELECT id FROM library_jobs WHERE library_id=? "
                "AND kind IN ('scan','collection_rebuild') "
                "ORDER BY created_at DESC LIMIT 1",
                (library_id,),
            )
            if rows:
                job = runtime_store.job(rows[0][0])
                self._on_library_job_status(job)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.store.ensure_defaults()
        legacy_hydration = self.store.by_key("metadata_hydration")
        if legacy_hydration:
            self.store.db.execute(
                "DELETE FROM job_runs WHERE definition_id=?", (legacy_hydration["id"],)
            )
            self.store.db.execute(
                "DELETE FROM job_definitions WHERE id=?", (legacy_hydration["id"],)
            )
        legacy_projections = self.store.db.read_execute(
            "SELECT id FROM job_definitions WHERE kind='catalog_projection'"
        )
        for (definition_id,) in legacy_projections:
            self.store.db.execute(
                "DELETE FROM job_runs WHERE definition_id=?", (definition_id,)
            )
            self.store.db.execute(
                "DELETE FROM job_definitions WHERE id=?", (definition_id,)
            )
        self.store.reconcile_library_definitions(self.library_runtime.store.list())
        self._sync_library_definitions()
        try:
            has_bazarr_mappings = bool(
                self.store.db.execute("SELECT 1 FROM bazarr_episode_mappings LIMIT 1")
            )
        except Exception:
            # Older databases can reach startup before the mapping migration is
            # applied. The normal migration path will make the next startup
            # eligible for the initial sync.
            has_bazarr_mappings = True
        if not has_bazarr_mappings:
            self.enqueue_bazarr_sync()
        # Startup triggers are intentionally armed on every process start and
        # remain manual-only afterward until their next explicit update.
        try:
            startup_rows = self.store.db.execute(
                "SELECT id,definition_id FROM job_schedule_triggers WHERE trigger_type='startup'"
            )
            for trigger_id, definition_id in startup_rows:
                self.store.db.execute(
                    "UPDATE job_schedule_triggers SET next_run_at=?,updated_at=? WHERE id=?",
                    (now(), now(), trigger_id),
                )
                self.store.db.execute(
                    "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
                    (now(), now(), definition_id),
                )
        except Exception:
            pass
        self._recover_active_runs()
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._dispatch, name="zenstream-job-scheduler", daemon=True
        )
        self.thread.start()

    def stop(self, timeout: float = 30.0):
        self.stop_event.set()
        with self.active_lock:
            active = list(self.cancel_events.items())
        for run_id, cancel_event in active:
            cancel_event.set()
            try:
                self.store.update_run(
                    run_id,
                    state="terminating",
                    message="Termination requested during Orchestrator shutdown",
                )
            except Exception:
                logger.warning(
                    "could not mark scheduled run terminating during shutdown run_id=%s",
                    run_id,
                    exc_info=True,
                )
        with self.condition:
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=5)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self.active_lock:
                workers = list(self.worker_threads.values())
            workers = [
                worker for worker in workers if worker is not threading.current_thread()
            ]
            if not workers:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "scheduled jobs did not stop before shutdown timeout active=%s",
                    len(workers),
                )
                return
            for worker in workers:
                worker.join(timeout=min(0.25, remaining))

    def _library_work_active(self) -> bool:
        runtime = getattr(self, "library_runtime", None)
        checker = getattr(runtime, "has_active_inventory_jobs", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            # Do not start destructive/background analysis while the inventory
            # state is unavailable during a concurrent lifecycle transition.
            logger.warning(
                "could not inspect library work before analysis", exc_info=True
            )
            return True

    def _metadata_work_active(self) -> bool:
        """Keep overlapping metadata walks from competing for providers/SQLite."""
        try:
            placeholders = ",".join("?" for _ in METADATA_JOB_KINDS)
            return bool(
                self.store.db.execute(
                    "SELECT 1 FROM job_runs WHERE kind IN ("
                    + placeholders
                    + ") AND state IN ('running','terminating') LIMIT 1",
                    tuple(sorted(METADATA_JOB_KINDS)),
                )
            )
        except Exception:
            with self.active_lock:
                # A failed queue read is safer when it defers metadata until
                # the next dispatcher pass than when it starts another walk.
                return bool(self.active_definitions)

    def _analysis_maintenance_active(self, kind: str) -> bool:
        with self.active_lock:
            return kind in getattr(self, "analysis_maintenance", set())

    def _active_analysis_runs(self, kind: str) -> list[tuple[str, str]]:
        rows = self.store.db.execute(
            "SELECT id,definition_id FROM job_runs "
            "WHERE kind=? AND state IN ('queued','running','terminating')",
            (kind,),
        )
        return [(str(row[0]), str(row[1])) for row in rows]

    def run_analysis_maintenance(self, kind: str, operation, timeout: float = 30.0):
        """Quiesce one analysis kind, run cleanup, and reopen scheduling."""
        if kind not in ANALYSIS_KINDS:
            raise ValueError("Unsupported analysis maintenance kind.")
        with self.active_lock:
            if not hasattr(self, "analysis_maintenance"):
                self.analysis_maintenance = set()
            self.analysis_maintenance.add(kind)

        deadline = time.monotonic() + max(0.0, float(timeout))
        tracked_runs: set[str] = set()
        requested_runs: set[str] = set()
        try:
            while True:
                active_runs = self._active_analysis_runs(kind)
                for run_id, definition_id in active_runs:
                    tracked_runs.add(run_id)
                    if run_id not in requested_runs:
                        self.terminate(definition_id, run_id)
                        requested_runs.add(run_id)
                with self.active_lock:
                    active_workers = tracked_runs.intersection(self.active)
                if not active_runs and not active_workers:
                    return operation()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AnalysisMaintenanceTimeout(
                        "Analysis is still stopping; try again shortly."
                    )
                condition = getattr(self, "condition", None)
                if condition is None:
                    time.sleep(min(0.01, remaining))
                else:
                    with condition:
                        condition.wait(timeout=min(0.25, remaining))
        finally:
            with self.active_lock:
                self.analysis_maintenance.discard(kind)
            condition = getattr(self, "condition", None)
            if condition is not None:
                with condition:
                    condition.notify_all()

    def refresh_library_definition(self, library: dict) -> dict:
        definition = self.store.ensure_library(library)
        desired = max(1, int(library.get("scanIntervalMinutes") or 1440)) * 60
        triggers = definition.get("triggers") or []
        if library.get("watchEnabled", True):
            interval = next(
                (item for item in triggers if item["type"] == "interval"), None
            )
            if interval:
                if interval.get("intervalSeconds") != desired:
                    self.store._replace_triggers(
                        definition["id"], [{**interval, "intervalSeconds": desired}]
                    )
            else:
                self.store._replace_triggers(
                    definition["id"], [{"type": "interval", "intervalSeconds": desired}]
                )
        else:
            self.store._replace_triggers(
                definition["id"],
                [item for item in triggers if item["type"] != "interval"],
            )
        return self.store.update_definition(
            definition["id"], {"config": {"libraryId": library["id"]}}
        )

    def remove_library_definition(self, library_id: str):
        self.store.remove_library_definitions(library_id)

    def run_now(self, definition_id: str, options: dict | None = None) -> dict:
        definition = self.store.definition(definition_id)
        if not definition:
            raise KeyError("Job definition not found")
        if definition["kind"] == "library_scan":
            library_id = (definition.get("config") or {}).get("libraryId")
            job = self.library_runtime.enqueue(library_id, "scan")
            self.store.db.execute(
                "UPDATE job_definitions SET last_state=?,last_run_at=?,last_run_id=?,last_message=?,updated_at=? WHERE id=?",
                (
                    job["state"],
                    now(),
                    job.get("id"),
                    job.get("message") or "Library scan queued",
                    now(),
                    definition_id,
                ),
            )
            return job
        run, _ = self.store.create_or_get_active_run(definition, options=options)
        with self.condition:
            self.condition.notify_all()
        return run

    def enqueue_metadata_missing(self) -> dict:
        definition = self.store.by_key("metadata_missing")
        if not definition:
            self.store.ensure_defaults()
            definition = self.store.by_key("metadata_missing")
        run, _ = self.store.create_or_get_active_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

    def enqueue_metadata_upgrade(self) -> dict:
        definition = self.store.by_key("metadata_upgrade")
        if not definition:
            self.store.ensure_defaults()
            definition = self.store.by_key("metadata_upgrade")
        run, _ = self.store.create_or_get_active_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

    def enqueue_metadata_refresh(self, options: dict | None = None) -> dict:
        definition = self.store.by_key("metadata_refresh")
        if not definition:
            definition = self.store.ensure(
                "metadata_refresh",
                "Refresh media metadata",
                "Refresh indexed movie, TV, album, and track metadata and artwork using the configured sparse rules.",
                "metadata_refresh",
                43200,
                {},
                enabled=False,
            )
        run, _ = self.store.create_or_get_active_run(definition, options=options)
        with self.condition:
            self.condition.notify_all()
        return run

    def enqueue_trickplay_extraction(self) -> dict:
        definition = self.store.by_key("trickplay_extract")
        if not definition:
            self.store.ensure_defaults()
            definition = self.store.by_key("trickplay_extract")
        run, _ = self.store.create_or_get_active_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

    def enqueue_intro_outro_detection(self) -> dict:
        definition = self.store.by_key("intro_outro_detect")
        if not definition:
            self.store.ensure_defaults()
            definition = self.store.by_key("intro_outro_detect")
        run, _ = self.store.create_or_get_active_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

    def enqueue_bazarr_sync(self) -> dict | None:
        from app.bazarr import BazarrConnectionStore

        if BazarrConnectionStore().internal() is None:
            return None
        definition = self.store.by_key("bazarr_sync")
        if not definition:
            self.store.ensure_defaults()
            definition = self.store.by_key("bazarr_sync")
        run, _ = self.store.create_or_get_active_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

    def terminate(self, definition_id: str, run_id: str) -> dict | None:
        runs = [
            run for run in self.store.runs(definition_id, 100) if run["id"] == run_id
        ]
        if not runs:
            return None
        run = runs[0]
        if run["state"] not in {"queued", "running", "terminating"}:
            return run
        with self.active_lock:
            cancel_event = self.cancel_events.get(run_id)
            if cancel_event:
                cancel_event.set()
                self.store.update_run(
                    run_id, state="terminating", message="Termination requested"
                )
            else:
                self.store.update_run(
                    run_id,
                    state="terminated",
                    message="Terminated by administrator",
                    error=None,
                    finished_at=now(),
                )
        with self.condition:
            self.condition.notify_all()
        return next(
            (
                value
                for value in self.store.runs(definition_id, 100)
                if value["id"] == run_id
            ),
            None,
        )

    def _recover_active_runs(self) -> None:
        """Resume one interrupted run per task and terminate stale duplicates."""
        rows = self.store.db.execute(
            "SELECT id,definition_id,state FROM job_runs WHERE state IN ('queued','running','terminating') ORDER BY created_at DESC"
        )
        by_definition: dict[str, list[tuple[str, str]]] = {}
        for run_id, definition_id, state in rows:
            by_definition.setdefault(definition_id, []).append((run_id, state))
        timestamp = now()
        with self.store.db.transaction() as cursor:
            definition_sync_ids: list[tuple[str, str]] = []
            for definition_id, runs in by_definition.items():
                resumable = [run for run in runs if run[1] != "terminating"]
                keep_id = resumable[0][0] if resumable else None
                for run_id, state in runs:
                    if run_id == keep_id:
                        cursor.execute(
                            "UPDATE job_runs SET state='queued',progress_current=0,progress_total=0,message='Queued again after Orchestrator restart',error=NULL,started_at=NULL,finished_at=NULL,thread_name=NULL WHERE id=?",
                            (run_id,),
                        )
                    else:
                        cursor.execute(
                            "UPDATE job_runs SET state='terminated',message='Superseded by the active task run',error=NULL,finished_at=? WHERE id=?",
                            (timestamp, run_id),
                        )
                definition_sync_ids.append((definition_id, keep_id or runs[0][0]))
        # Recovery changes durable run state before worker dispatch. Sync the
        # definition pointer now so a restarted task cannot look idle or point
        # at a run that was just superseded.
        for _definition_id, run_id in definition_sync_ids:
            self.store._sync_definition_from_run(run_id)

    def _schedule_due(self):
        # Versioned repairs are one-time work items rather than recurring
        # triggers. Clear the arm atomically before dispatch; a failed run
        # remains eligible on the next process startup because last_run_at is
        # left unset by MusicCatalogRepairJob.
        try:
            one_time_rows = self.store.db.execute(
                "SELECT id FROM job_definitions WHERE kind='music_catalog_repair' "
                "AND next_run_at IS NOT NULL AND next_run_at<=? AND last_run_at IS NULL",
                (now(),),
            )
        except Exception:
            one_time_rows = []
        for (definition_id,) in one_time_rows:
            definition = self.store.definition(definition_id)
            if not definition or self.store.queued_or_running(definition_id):
                continue
            run, created = self.store.create_or_get_active_run(definition)
            if created:
                self.store.db.execute(
                    "UPDATE job_definitions SET next_run_at=NULL,updated_at=? WHERE id=? AND last_run_at IS NULL",
                    (now(), definition_id),
                )
        for due in self.store.due_triggers():
            definition = due["definition"]
            trigger = due["trigger"]
            if self._analysis_maintenance_active(definition["kind"]):
                continue
            if self.store.queued_or_running(definition["id"]):
                continue
            if definition["kind"] == "library_scan":
                library_id = (definition.get("config") or {}).get("libraryId")
                job = (
                    self.library_runtime.enqueue(library_id, "scan")
                    if library_id
                    else None
                )
                self.store.mark_trigger_scheduled(
                    definition["id"],
                    trigger,
                    job.get("id") if job else None,
                    "Library scan queued",
                )
            else:
                trigger_options = trigger.get("options") or {}
                # refreshAll is deliberately a manual-only option.  Ignore it
                # on legacy persisted triggers so scheduled work always uses
                # the sparse policy.
                trigger_options = {
                    key: value
                    for key, value in trigger_options.items()
                    if key != "refreshAll"
                }
                run, created = self.store.create_or_get_active_run(
                    definition,
                    options=trigger_options,
                    source_trigger_id=trigger["id"],
                )
                if not created:
                    continue
                self.store.mark_trigger_scheduled(definition["id"], trigger, run["id"])

    def _dispatch(self):
        retry_delay = JOB_DISPATCH_BACKOFF_INITIAL
        while not self.stop_event.is_set():
            try:
                self._dispatch_iteration()
                with self.active_lock:
                    previous_failures = getattr(
                        self, "_dispatch_consecutive_failures", 0
                    )
                    self._dispatch_last_success = time.time()
                    self._dispatch_last_error = None
                    self._dispatch_consecutive_failures = 0
                if previous_failures:
                    logger.info(
                        "scheduled job dispatcher recovered after failures=%s",
                        previous_failures,
                    )
                retry_delay = JOB_DISPATCH_BACKOFF_INITIAL
            except (SQLAlchemyTimeoutError, SQLAlchemyError) as error:
                with self.active_lock:
                    self._dispatch_consecutive_failures = (
                        getattr(self, "_dispatch_consecutive_failures", 0) + 1
                    )
                    failures = self._dispatch_consecutive_failures
                    self._dispatch_last_error = type(error).__name__
                logger.warning(
                    "scheduled job dispatcher iteration failed error_type=%s consecutive_failures=%s retry_delay_seconds=%.3f",
                    type(error).__name__,
                    failures,
                    retry_delay,
                    exc_info=True,
                )
                if self.stop_event.wait(retry_delay):
                    break
                retry_delay = min(JOB_DISPATCH_BACKOFF_MAX, retry_delay * 2)
            except Exception as error:
                with self.active_lock:
                    self._dispatch_consecutive_failures = (
                        getattr(self, "_dispatch_consecutive_failures", 0) + 1
                    )
                    failures = self._dispatch_consecutive_failures
                    self._dispatch_last_error = type(error).__name__
                logger.warning(
                    "scheduled job dispatcher iteration failed error_type=%s consecutive_failures=%s retry_delay_seconds=%.3f",
                    type(error).__name__,
                    failures,
                    retry_delay,
                    exc_info=True,
                )
                if self.stop_event.wait(retry_delay):
                    break
                retry_delay = min(JOB_DISPATCH_BACKOFF_MAX, retry_delay * 2)

    def _dispatch_iteration(self):
        self._schedule_due()
        queued = self.store.runs(limit=1000)
        metadata_work_active = self._metadata_work_active()
        for run in queued:
            if run["state"] != "queued":
                continue
            if run["kind"] in METADATA_JOB_KINDS and metadata_work_active:
                continue
            if run["kind"] in CATALOG_EXCLUSIVE_KINDS and self._library_work_active():
                # Inventory admission owns the mutable catalog snapshot;
                # the coordinator below closes the remaining check/start
                # race when both workers become runnable together.
                continue
            if (
                run["kind"] in ANALYSIS_KINDS or run["kind"] == "bazarr_sync"
            ) and self._library_work_active():
                continue
            with self.active_lock:
                if run["kind"] in getattr(self, "analysis_maintenance", set()):
                    continue
                if (
                    run["id"] in self.active
                    or run["definitionId"] in self.active_definitions
                ):
                    continue
                self.active.add(run["id"])
                self.active_definitions.add(run["definitionId"])
                self.cancel_events[run["id"]] = threading.Event()
                try:
                    with self.store.db.transaction() as cursor:
                        cursor.execute(
                            "UPDATE job_runs SET state='running',started_at=?,thread_name=?,message='Starting task' WHERE id=? AND state='queued'",
                            (now(), f"zenstream-job-{run['id'][:8]}", run["id"]),
                        )
                        claimed = cursor.rowcount == 1
                except Exception:
                    self.active.discard(run["id"])
                    self.active_definitions.discard(run["definitionId"])
                    self.cancel_events.pop(run["id"], None)
                    raise
                if not claimed:
                    self.active.discard(run["id"])
                    self.active_definitions.discard(run["definitionId"])
                    self.cancel_events.pop(run["id"], None)
                    continue
                try:
                    # Claiming is intentionally a small atomic SQL update,
                    # but it still changes the definition's observable
                    # state. Keep the dashboard pointer in sync before
                    # the worker starts, including analysis jobs whose
                    # implementation does not emit an initial progress
                    # update.
                    self.store._sync_definition_from_run(run["id"])
                except Exception:
                    logger.warning(
                        "could not synchronize claimed scheduler run=%s",
                        run["id"],
                        exc_info=True,
                    )
                if run["kind"] in METADATA_JOB_KINDS:
                    metadata_work_active = True
            thread = threading.Thread(
                target=self._execute,
                args=(run["id"],),
                name=f"zenstream-job-{run['id'][:8]}",
                daemon=True,
            )
            with self.active_lock:
                self.worker_threads[run["id"]] = thread
            try:
                thread.start()
            except Exception:
                with self.active_lock:
                    self.worker_threads.pop(run["id"], None)
                    self.active.discard(run["id"])
                    self.active_definitions.discard(run["definitionId"])
                    self.cancel_events.pop(run["id"], None)
                self.store.update_run(
                    run["id"],
                    state="queued",
                    started_at=None,
                    finished_at=None,
                    message="Queued again after scheduler recovery",
                )
                raise
        with self.condition:
            self.condition.wait(timeout=1)

    def _execute(self, run_id: str):
        catalog_work_acquired = False
        try:
            columns = {
                row[1] for row in self.store.db.execute("PRAGMA table_info(job_runs)")
            }
            snapshot = ",r.options" if "options" in columns else ",NULL"
            rows = self.store.db.execute(
                f"SELECT r.id,r.definition_id,d.kind,d.config,d.name{snapshot} FROM job_runs r JOIN job_definitions d ON d.id=r.definition_id WHERE r.id=?",
                (run_id,),
            )
            if not rows:
                return
            _, definition_id, kind, config_text, name, options_text = rows[0]
            try:
                config = json.loads(config_text or "{}")
            except json.JSONDecodeError:
                config = {}
            definition = self.store.definition(definition_id) or {
                "id": definition_id,
                "kind": kind,
                "config": config,
                "name": name,
            }
            try:
                run_options = json.loads(options_text or "{}")
            except (TypeError, json.JSONDecodeError):
                run_options = {}
            coordinator = getattr(self, "catalog_work_coordinator", None)
            if kind in CATALOG_EXCLUSIVE_KINDS and coordinator is not None:
                coordinator.acquire_metadata()
                catalog_work_acquired = True
            self.store.begin_progress(run_id, kind)
            if kind == "metadata_missing":
                MetadataMissingJob(
                    self.store,
                    catalog_work_coordinator=(
                        coordinator if catalog_work_acquired else None
                    ),
                ).run(run_id, definition, self.cancel_events[run_id].is_set)
            elif kind == "metadata_upgrade":
                MetadataUpgradeJob(self.store).run(
                    run_id, definition, self.cancel_events[run_id].is_set
                )
            elif kind == "music_catalog_repair":
                MusicCatalogRepairJob(self.store, self.library_runtime).run(
                    run_id, definition, self.cancel_events[run_id].is_set
                )
            elif kind == "metadata_refresh":
                if bool(run_options.get("refreshAll", False)):
                    MetadataMissingJob(
                        self.store,
                        catalog_work_coordinator=(
                            coordinator if catalog_work_acquired else None
                        ),
                    ).run(
                        run_id,
                        definition,
                        self.cancel_events[run_id].is_set,
                        force=True,
                        force_assets=not bool(
                            run_options.get("preserveCachedAssets", False)
                        ),
                    )
                else:
                    MetadataRefreshJob(self.store).run(
                        run_id,
                        definition,
                        self.cancel_events[run_id].is_set,
                        preserve_cached_assets=bool(
                            run_options.get("preserveCachedAssets", False)
                        ),
                    )
            elif kind == "metadata_cleanup":
                MetadataCleanupJob(self.store).run(
                    run_id, definition, self.cancel_events[run_id].is_set
                )
            elif kind == "trickplay_extract":
                self._run_analysis(
                    run_id,
                    kind,
                    TrickplayExtractor(),
                    self.cancel_events[run_id].is_set,
                )
            elif kind == "intro_outro_detect":
                self._run_analysis(
                    run_id,
                    kind,
                    IntroOutroDetector(),
                    self.cancel_events[run_id].is_set,
                )
            elif kind == "bazarr_sync":
                BazarrSyncJob(self.store).run(run_id, self.cancel_events[run_id].is_set)
            elif kind == "calendar_sync":
                from app.calendar import CalendarSyncJob

                CalendarSyncJob(self.store).run(
                    run_id, self.cancel_events[run_id].is_set
                )
            elif kind == "calendar_future_metadata":
                from app.calendar import CalendarFutureMetadataJob

                CalendarFutureMetadataJob(self.store).run(
                    run_id, self.cancel_events[run_id].is_set
                )
            else:
                self.store.update_run(
                    run_id,
                    state="failed",
                    error=f"Unsupported job kind: {kind}",
                    finished_at=now(),
                )
            logger.info("scheduled job complete run_id=%s kind=%s", run_id, kind)
        except JobTerminated:
            self.store.update_run(
                run_id,
                state="terminated",
                message=(
                    "Terminated during Orchestrator shutdown"
                    if self.stop_event.is_set()
                    else "Terminated by administrator"
                ),
                error=None,
                finished_at=now(),
            )
        except Exception as error:
            if (
                self.stop_event.is_set()
                and isinstance(error, RuntimeError)
                and ("cannot schedule new futures after" in str(error).lower())
            ):
                logger.warning(
                    "scheduled job stopped while executors were shutting down run_id=%s",
                    run_id,
                )
                self.store.update_run(
                    run_id,
                    state="terminated",
                    message="Terminated during Orchestrator shutdown",
                    error=None,
                    finished_at=now(),
                )
                return
            details = {
                "operation": "scheduled_job",
                "runId": run_id,
                "exception": type(error).__name__,
                "traceback": traceback.format_exc(),
            }
            logger.exception("scheduled job failed run_id=%s", run_id)
            self.store.update_run(
                run_id,
                state="failed",
                error=f"{type(error).__name__}: {error}",
                error_details=json.dumps(details),
                finished_at=now(),
            )
        finally:
            if catalog_work_acquired:
                coordinator = getattr(self, "catalog_work_coordinator", None)
                if coordinator is not None:
                    coordinator.release_metadata()
            self.store.end_progress(run_id)
            with self.active_lock:
                self.active.discard(run_id)
                self.cancel_events.pop(run_id, None)
                self.worker_threads.pop(run_id, None)
                row = self.store.db.execute(
                    "SELECT definition_id FROM job_runs WHERE id=?", (run_id,)
                )
                if row:
                    self.active_definitions.discard(row[0][0])
            condition = getattr(self, "condition", None)
            if condition is not None:
                with condition:
                    condition.notify_all()

    def _run_analysis(self, run_id, kind, worker, should_terminate):
        pressure_logged = False
        while True:
            foreground_requests = active_requests()
            library_work = self._library_work_active()
            if not foreground_requests and not library_work:
                break
            if should_terminate():
                raise JobTerminated()
            if not pressure_logged:
                logger.info(
                    "analysis job yielding run_id=%s kind=%s active_requests=%s library_work=%s",
                    run_id,
                    kind,
                    foreground_requests,
                    library_work,
                )
                pressure_logged = True
            # Foreground/library transitions wake the scheduler condition when
            # they enqueue or finish work.  The timeout is only a safety net
            # because request counters live in another module; it avoids a
            # tight polling loop while retaining prompt cancellation.
            condition = getattr(self, "condition", None)
            if condition is None:
                # Lightweight test doubles and legacy callers may construct
                # the scheduler without its dispatch condition.
                time.sleep(0.01)
            else:
                with condition:
                    condition.wait(timeout=0.5)
        logger.info(
            "analysis job starting independent worker pool run_id=%s kind=%s",
            run_id,
            kind,
        )
        worker.run(run_id, self.store, should_terminate)
        logger.info(
            "analysis job completed worker pool run_id=%s kind=%s", run_id, kind
        )


scheduler = JobScheduler(library_runtime)
