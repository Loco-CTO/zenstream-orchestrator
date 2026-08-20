from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import httpx

from app.config import Config
from app.logging_config import get_logger
from app.metadata_domain import (
    ARTWORK_CATEGORIES,
    fallback_tiers,
    language_family,
    locale_variants,
    rank_artwork_candidates,
    usable_text,
)
from app.metadata_services import MetadataImageIngestService
from app.models.account_preference import AccountPreference
from app.models.calendar import (
    FutureMetadataCache,
    decrypt_calendar_api_key,
    encrypt_calendar_api_key,
)
from app.models.metadata import MetadataCache, MetadataLanguageSettings
from app.providers import MetadataService, ProviderError

logger = get_logger("calendar")

CALENDAR_PROVIDERS = ("sonarr", "radarr")
EXPECTED_LIBRARY_TYPES = {"sonarr": "tv_series", "radarr": "movies"}
DEFAULT_PAST_DAYS = 7
DEFAULT_FUTURE_DAYS = 90


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def calendar_window(
    current: datetime | None = None,
    past_days: int = DEFAULT_PAST_DAYS,
    future_days: int = DEFAULT_FUTURE_DAYS,
) -> tuple[datetime, datetime]:
    current = current or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    return current - timedelta(days=past_days), current + timedelta(days=future_days)


def _parse_datetime(value) -> tuple[str, str, bool] | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    all_day = len(raw) == 10 and raw[4] == "-" and raw[7] == "-"
    try:
        if all_day:
            parsed = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
    except ValueError:
        return None
    return parsed.isoformat(), parsed.date().isoformat(), all_day


def _string_id(value) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip()
    return raw or None


def _normalize_base_url(value: str | None) -> str:
    raw = str(value or "/").strip()
    if not raw:
        raw = "/"
    if "://" in raw or "?" in raw or "#" in raw:
        raise ValueError("baseUrl must be a path, not a URL or query string")
    if not raw.startswith("/"):
        raw = "/" + raw
    raw = "/" + "/".join(part for part in raw.split("/") if part)
    return raw if raw != "/" else ""


def _normalize_address(value: str | None) -> str:
    address = str(value or "").strip()
    if not address or len(address) > 255 or any(char.isspace() for char in address):
        raise ValueError("address must be a non-empty host or IP address")
    if "://" in address or "/" in address:
        raise ValueError("address must not include a scheme or path")
    parsed = urlparse(f"//{address}")
    if not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("address must be a host or IP address")
    return address


def _parse_port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("port must be between 1 and 65535") from error
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


class CalendarConnectionStore:
    def __init__(self):
        self.db = Config().database

    def _public_row(self, row) -> dict:
        provider, address, port, base_url, use_ssl, library_id, api_key, validated, last_sync, last_error, updated, library_name = row
        return {
            "provider": provider,
            "address": address,
            "port": port,
            "baseUrl": base_url or "/",
            "useSsl": bool(use_ssl),
            "libraryId": library_id,
            "libraryName": library_name,
            "apiKeyConfigured": bool(api_key),
            "configured": True,
            "validatedAt": validated,
            "lastSyncAt": last_sync,
            "lastError": last_error,
            "updatedAt": updated,
        }

    def public(self) -> dict[str, dict]:
        values = {}
        for provider in CALENDAR_PROVIDERS:
            rows = self.db.execute(
                "SELECT c.provider,c.address,c.port,c.base_url,c.use_ssl,c.library_id,c.api_key_ciphertext,"
                "c.validated_at,c.last_sync_at,c.last_error,c.updated_at,l.name "
                "FROM calendar_connections c JOIN libraries l ON l.id=c.library_id WHERE c.provider=?",
                (provider,),
            )
            values[provider] = (
                self._public_row(rows[0])
                if rows
                else {"provider": provider, "configured": False, "apiKeyConfigured": False}
            )
        return values

    def internal(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT provider,address,port,base_url,use_ssl,library_id,api_key_ciphertext "
            "FROM calendar_connections ORDER BY provider"
        )
        values = []
        for provider, address, port, base_url, use_ssl, library_id, ciphertext in rows:
            try:
                api_key = decrypt_calendar_api_key(ciphertext)
            except ValueError:
                logger.error("calendar API key could not be decrypted provider=%s", provider)
                continue
            values.append(
                {
                    "provider": provider,
                    "address": address,
                    "port": int(port),
                    "baseUrl": base_url,
                    "useSsl": bool(use_ssl),
                    "libraryId": library_id,
                    "apiKey": api_key,
                }
            )
        return values

    def save(self, provider: str, values: dict) -> dict:
        if provider not in CALENDAR_PROVIDERS:
            raise ValueError("Unsupported calendar provider")
        if not isinstance(values, dict):
            raise ValueError("Calendar settings must be an object")
        if values.get("enabled") is False:
            self.clear(provider)
            return self.public()[provider]
        address = _normalize_address(values.get("address"))
        port = _parse_port(values.get("port"))
        base_url = _normalize_base_url(values.get("baseUrl"))
        use_ssl = values.get("useSsl")
        if type(use_ssl) is not bool:
            raise ValueError("useSsl must be a boolean")
        library_id = str(values.get("libraryId") or "").strip()
        if not library_id:
            raise ValueError("Choose a library for this calendar service")
        library_rows = self.db.execute(
            "SELECT type FROM libraries WHERE id=?", (library_id,)
        )
        if not library_rows:
            raise ValueError("Selected library does not exist")
        if library_rows[0][0] != EXPECTED_LIBRARY_TYPES[provider]:
            raise ValueError(
                f"{provider.title()} must be mapped to a {EXPECTED_LIBRARY_TYPES[provider]} library"
            )
        existing = self.db.execute(
            "SELECT api_key_ciphertext FROM calendar_connections WHERE provider=?",
            (provider,),
        )
        supplied_key = values.get("apiKey")
        if supplied_key is not None and str(supplied_key).strip():
            ciphertext = encrypt_calendar_api_key(str(supplied_key).strip())
        elif existing:
            ciphertext = existing[0][0]
        else:
            raise ValueError("An API key is required for a new calendar service")
        timestamp = iso_now()
        self.db.execute(
            "INSERT INTO calendar_connections(provider,address,port,base_url,use_ssl,library_id,api_key_ciphertext,validated_at,last_sync_at,last_error,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET address=excluded.address,port=excluded.port,base_url=excluded.base_url,"
            "use_ssl=excluded.use_ssl,library_id=excluded.library_id,api_key_ciphertext=excluded.api_key_ciphertext,validated_at=NULL,last_error=NULL,updated_at=excluded.updated_at",
            (
                provider,
                address,
                port,
                base_url,
                int(use_ssl),
                library_id,
                ciphertext,
                None,
                None,
                None,
                timestamp,
            ),
        )
        return self.public()[provider]

    def clear(self, provider: str) -> None:
        self.db.execute("DELETE FROM calendar_connections WHERE provider=?", (provider,))
        self.db.execute("DELETE FROM calendar_events WHERE provider=?", (provider,))

    def record_sync(self, provider: str, *, error: str | None = None) -> None:
        timestamp = iso_now()
        self.db.execute(
            "UPDATE calendar_connections SET last_sync_at=?,last_error=?,validated_at=CASE WHEN ? IS NULL THEN ? ELSE validated_at END,updated_at=? WHERE provider=?",
            (timestamp, error, error, timestamp, timestamp, provider),
        )


class ArrCalendarClient:
    def __init__(self, connection: dict):
        self.connection = connection
        try:
            configured_timeout = float(os.getenv("METADATA_PROVIDER_TIMEOUT_SECONDS", "20"))
        except ValueError:
            configured_timeout = 20
        self.timeout = max(3.0, min(60.0, configured_timeout))

    @property
    def endpoint(self) -> str:
        scheme = "https" if self.connection["useSsl"] else "http"
        base = self.connection["baseUrl"].rstrip("/")
        return f"{scheme}://{self.connection['address']}:{self.connection['port']}{base}/api/v3/calendar"

    def fetch(self, start: datetime, end: datetime) -> list[dict]:
        params = {
            "start": start.date().isoformat(),
            "end": end.date().isoformat(),
            "unmonitored": "false",
        }
        if self.connection["provider"] == "sonarr":
            params.update(
                {
                    "includeSeries": "true",
                    "includeEpisodeFile": "true",
                    "includeEpisodeImages": "false",
                }
            )
        else:
            params["includeMovieFile"] = "true"
        started = time.monotonic()
        logger.info(
            "calendar provider request start provider=%s endpoint=%s start=%s end=%s",
            self.connection["provider"],
            self.endpoint,
            params["start"],
            params["end"],
        )
        try:
            response = None
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Accept": "application/json",
                    "X-Api-Key": self.connection["apiKey"],
                    "User-Agent": "ZenStream/Calendar",
                },
            ) as client:
                for attempt in range(3):
                    try:
                        response = client.get(self.endpoint, params=params)
                    except httpx.TransportError:
                        if attempt == 2:
                            raise
                        time.sleep(min(5.0, 0.25 * (2**attempt)))
                        continue
                    if response.status_code not in {429, 502, 503, 504} or attempt == 2:
                        break
                    time.sleep(min(5.0, 0.25 * (2**attempt)))
            if response is None:
                raise ProviderError("calendar provider returned no response")
            if response.status_code == 404:
                raise ProviderError("calendar endpoint was not found")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            logger.warning(
                "calendar provider request failed provider=%s endpoint=%s duration_seconds=%.1f error=%s",
                self.connection["provider"],
                self.endpoint,
                time.monotonic() - started,
                error,
            )
            raise ProviderError(
                f"{self.connection['provider']} calendar request failed: {type(error).__name__}: {error}"
            ) from error
        if isinstance(payload, list):
            return [value for value in payload if isinstance(value, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return [value for value in payload["data"] if isinstance(value, dict)]
        raise ProviderError("calendar provider returned an invalid response")


@dataclass(frozen=True)
class CalendarEventValue:
    provider: str
    library_id: str
    source_event_id: str
    kind: str
    release_type: str
    event_at: str
    event_date: str
    all_day: bool
    tvdb_id: str | None = None
    tmdb_id: str | None = None
    series_tvdb_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    has_file: bool = False
    monitored: bool = True


def _normalize_sonarr(item: dict, library_id: str) -> CalendarEventValue | None:
    source_id = _string_id(item.get("id"))
    tvdb_id = _string_id(item.get("tvdbId") or item.get("tvdbID"))
    series = item.get("series") if isinstance(item.get("series"), dict) else {}
    series_tvdb_id = _string_id(series.get("tvdbId") or series.get("tvdbID"))
    parsed = _parse_datetime(item.get("airDateUtc")) or _parse_datetime(item.get("airDate"))
    if not source_id or not tvdb_id or not parsed:
        return None
    season = item.get("seasonNumber")
    episode = item.get("episodeNumber")
    try:
        season = int(season) if season is not None else None
        episode = int(episode) if episode is not None else None
    except (TypeError, ValueError):
        season = episode = None
    return CalendarEventValue(
        provider="sonarr",
        library_id=library_id,
        source_event_id=source_id,
        kind="episode",
        release_type="air",
        event_at=parsed[0],
        event_date=parsed[1],
        all_day=parsed[2],
        tvdb_id=tvdb_id,
        series_tvdb_id=series_tvdb_id,
        season_number=season,
        episode_number=episode,
        has_file=bool(item.get("hasFile")),
        monitored=bool(item.get("monitored", True)),
    )


def _normalize_radarr(item: dict, library_id: str) -> list[CalendarEventValue]:
    source_id = _string_id(item.get("id"))
    tmdb_id = _string_id(item.get("tmdbId") or item.get("tmdbID"))
    if not source_id or not tmdb_id:
        return []
    dates = (
        ("cinema", item.get("inCinemas")),
        ("digital", item.get("digitalRelease")),
        ("physical", item.get("physicalRelease")),
    )
    if not any(value is not None for _, value in dates):
        dates = (("release", item.get("releaseDate")),)
    values = []
    for release_type, raw_date in dates:
        parsed = _parse_datetime(raw_date)
        if not parsed:
            continue
        values.append(
            CalendarEventValue(
                provider="radarr",
                library_id=library_id,
                source_event_id=source_id,
                kind="movie",
                release_type=release_type,
                event_at=parsed[0],
                event_date=parsed[1],
                all_day=parsed[2],
                tmdb_id=tmdb_id,
                has_file=bool(item.get("hasFile")),
                monitored=bool(item.get("monitored", True)),
            )
        )
    return values


def normalize_calendar_events(provider: str, library_id: str, payload: Iterable[dict]) -> list[CalendarEventValue]:
    values = []
    for item in payload:
        if provider == "sonarr":
            normalized = _normalize_sonarr(item, library_id)
            if normalized:
                values.append(normalized)
        else:
            values.extend(_normalize_radarr(item, library_id))
    return values


class CalendarSyncService:
    def __init__(self):
        self.db = Config().database
        self.connections = CalendarConnectionStore()
        self.future_cache = FutureMetadataCache()

    @staticmethod
    def _event_id(event: CalendarEventValue) -> str:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                ":".join(
                    (
                        event.provider,
                        event.library_id,
                        event.source_event_id,
                        event.release_type,
                        event.event_at,
                    )
                ),
            )
        )

    def _matching_entities(self, event: CalendarEventValue) -> list[tuple[str, str]]:
        identities = []
        if event.kind == "episode":
            if event.tvdb_id:
                identities.append(("tvdb", event.tvdb_id, "episode"))
            if event.series_tvdb_id:
                identities.append(("tvdb", event.series_tvdb_id, "series"))
        elif event.tmdb_id:
            identities.append(("tmdb", event.tmdb_id, "movie"))
        matches = []
        for provider, provider_id, entity_type in identities:
            rows = self.db.execute(
                "SELECT e.id,e.entity_type FROM entity_provider_ids p JOIN library_entities e ON e.id=p.entity_id "
                "WHERE e.library_id=? AND p.provider=? AND p.provider_id=? AND e.entity_type=?",
                (event.library_id, provider, provider_id, entity_type),
            )
            matches.extend((row[0], row[1]) for row in rows)
        return list(dict.fromkeys(matches))

    def _upsert(self, event: CalendarEventValue, seen_at: str) -> bool:
        event_id = self._event_id(event)
        values = (
            event_id,
            event.provider,
            event.library_id,
            event.source_event_id,
            event.kind,
            event.release_type,
            event.event_at,
            event.event_date,
            int(event.all_day),
            event.tvdb_id,
            event.tmdb_id,
            event.series_tvdb_id,
            event.season_number,
            event.episode_number,
            int(event.has_file),
            int(event.monitored),
            "future",
            seen_at,
            seen_at,
            seen_at,
        )
        self.db.execute(
            "INSERT INTO calendar_events(id,provider,library_id,source_event_id,kind,release_type,event_at,event_date,all_day,tvdb_id,tmdb_id,series_tvdb_id,season_number,episode_number,has_file,monitored,state,last_seen_at,fetched_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,library_id,source_event_id,release_type,event_at) DO UPDATE SET "
            "kind=excluded.kind,event_date=excluded.event_date,all_day=excluded.all_day,tvdb_id=excluded.tvdb_id,tmdb_id=excluded.tmdb_id,series_tvdb_id=excluded.series_tvdb_id,"
            "season_number=excluded.season_number,episode_number=excluded.episode_number,has_file=excluded.has_file,monitored=excluded.monitored,last_seen_at=excluded.last_seen_at,fetched_at=excluded.fetched_at,updated_at=excluded.updated_at",
            values,
        )
        expected_entity_type = "movie" if event.kind == "movie" else "episode"
        # A parent series is useful for future metadata, but it is not the
        # catalog item represented by an episode calendar event.  Only an
        # exact episode/movie identity can make an event existing.
        matches = [
            match
            for match in self._matching_entities(event)
            if match[1] == expected_entity_type
        ]
        self.db.execute("DELETE FROM calendar_event_entities WHERE event_id=?", (event_id,))
        if matches:
            with self.db.transaction() as cursor:
                cursor.executemany(
                    "INSERT OR IGNORE INTO calendar_event_entities(event_id,entity_id) VALUES(?,?)",
                    [(event_id, entity_id) for entity_id, _ in matches],
                )
        self.db.execute(
            "UPDATE calendar_events SET state=?,updated_at=? WHERE id=?",
            ("existing" if matches else "future", seen_at, event_id),
        )
        if matches:
            identities = []
            if event.kind == "episode":
                if event.tvdb_id:
                    identities.append(("tvdb", "episode", event.tvdb_id))
                if event.series_tvdb_id:
                    identities.append(("tvdb", "series", event.series_tvdb_id))
            elif event.tmdb_id:
                identities.append(("tmdb", "movie", event.tmdb_id))
            for provider, entity_type, provider_id in identities:
                self.future_cache.promote_identity(provider, entity_type, provider_id)
        return bool(matches)

    def _replace_provider_events(
        self, provider: str, library_id: str, payload: Iterable[dict], seen_at: str
    ) -> tuple[int, int]:
        normalized = normalize_calendar_events(provider, library_id, payload)
        existing = 0
        for event in normalized:
            if self._upsert(event, seen_at):
                existing += 1
        self.db.execute(
            "DELETE FROM calendar_events WHERE provider=? AND library_id=? AND last_seen_at<>?",
            (provider, library_id, seen_at),
        )
        return len(normalized), existing

    def sync(self) -> dict:
        start, end = calendar_window()
        results = {"providers": 0, "events": 0, "existing": 0, "errors": []}
        for connection in self.connections.internal():
            provider = connection["provider"]
            try:
                payload = ArrCalendarClient(connection).fetch(start, end)
                seen_at = iso_now()
                events, existing = self._replace_provider_events(
                    provider, connection["libraryId"], payload, seen_at
                )
                self.connections.record_sync(provider)
                results["providers"] += 1
                results["events"] += events
                results["existing"] += existing
            except Exception as error:
                message = f"{provider}: {type(error).__name__}: {error}"
                logger.warning("calendar sync failed provider=%s error=%s", provider, error)
                self.connections.record_sync(provider, error=message[:500])
                results["errors"].append(message[:500])
        return results


class CalendarFutureMetadataService:
    def __init__(self):
        self.db = Config().database
        self.cache = FutureMetadataCache()
        self.metadata = MetadataService()

    def _targets(self) -> list[tuple[str, str, str]]:
        start, end = calendar_window()
        rows = self.db.execute(
            "SELECT kind,tvdb_id,series_tvdb_id,tmdb_id FROM calendar_events e "
            "WHERE e.event_at>=? AND e.event_at<=? AND (e.state='future' OR NOT EXISTS ("
            "SELECT 1 FROM calendar_event_entities x JOIN library_entities linked ON linked.id=x.entity_id "
            "WHERE x.event_id=e.id AND linked.entity_type=CASE WHEN e.kind='movie' THEN 'movie' ELSE 'episode' END"
            "))",
            (start.isoformat(), end.isoformat()),
        )
        values = set()
        for kind, tvdb_id, series_tvdb_id, tmdb_id in rows:
            if kind == "episode":
                if tvdb_id:
                    values.add(("tvdb", "episode", str(tvdb_id)))
                if series_tvdb_id:
                    values.add(("tvdb", "series", str(series_tvdb_id)))
            elif tmdb_id:
                values.add(("tmdb", "movie", str(tmdb_id)))
        return sorted(values)

    def _ingest_images(self, provider: str, entity_type: str, provider_id: str, documents: dict[str, dict], force: bool) -> None:
        db_file = getattr(self.db, "db_file", None)
        if not db_file or db_file == ":memory:":
            return
        image_service = MetadataImageIngestService(
            self.cache,
            image_root=Path(db_file).parent / "future-metadata-cache" / "images",
        )
        include_english = any(language_family(value) == "en" for value in MetadataLanguageSettings().get())
        prefer_no_language = MetadataLanguageSettings().prefer_no_language_for_backdrop()
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
                    include_english=include_english,
                    prefer_no_language_for_backdrop=prefer_no_language,
                )
                for candidate in candidates[:2]:
                    url = candidate.get("url")
                    if not isinstance(url, str):
                        continue
                    target = image_service._target(url)
                    if target is None:
                        continue
                    try:
                        with image_service._file_lock(target):
                            if force or not target.is_file() or target.stat().st_size <= 0:
                                image_service._download(url, target)
                        blur_hash = image_service.hasher(target)
                        image_service._persist(provider, entity_type, provider_id, candidate, target, blur_hash)
                        break
                    except Exception as error:
                        logger.warning(
                            "future metadata image ingest failed provider=%s entity_type=%s provider_id=%s locale=%s type=%s error=%s",
                            provider,
                            entity_type,
                            provider_id,
                            locale,
                            image_type,
                            error,
                        )

    def refetch(self, should_terminate=None, force: bool = True) -> dict:
        should_terminate = should_terminate or (lambda: False)
        locales = MetadataLanguageSettings().get()
        values = {"targets": 0, "updated": 0, "errors": []}
        for provider, entity_type, provider_id in self._targets():
            if should_terminate():
                from app.library import JobTerminated

                raise JobTerminated()
            values["targets"] += 1
            try:
                documents = self.metadata.fetch_locales(
                    provider,
                    entity_type,
                    provider_id,
                    locales,
                    force=force,
                    project=False,
                    cache=self.cache,
                )
                self._ingest_images(provider, entity_type, provider_id, documents, force=False)
                values["updated"] += len(documents)
            except Exception as error:
                message = f"{provider} {entity_type} {provider_id}: {type(error).__name__}: {error}"
                logger.warning("future metadata refetch failed identity=%s error=%s", provider_id, error)
                values["errors"].append(message[:500])
        self.cache.prune_expired()
        return values


class CalendarReadService:
    def __init__(self):
        self.db = Config().database
        self.normal_cache = MetadataCache()
        self.future_cache = FutureMetadataCache()

    def _payload(self, provider: str, entity_type: str, provider_id: str, locale: str, future: bool) -> dict | None:
        cache = self.future_cache if future else self.normal_cache
        return cache.get(provider, entity_type, provider_id, locale)

    def _title(self, provider: str, entity_type: str, provider_id: str | None, locale: str, future: bool) -> str | None:
        if not provider_id:
            return None
        cache = self.future_cache if future else self.normal_cache
        payloads = cache.get_locales(provider, entity_type, provider_id)
        if not payloads:
            return None
        original = next(
            (
                str(payload.get("originalLanguage"))
                for payload in payloads.values()
                if payload.get("originalLanguage")
            ),
            None,
        )
        configured = MetadataLanguageSettings().get()
        tiers = fallback_tiers(
            locale,
            original,
            media=False,
            include_english=any(language_family(value) == "en" for value in configured),
        )
        available = list(payloads)
        for tier in tiers:
            for candidate_locale in locale_variants(tier, available):
                payload = payloads[candidate_locale]
                for field in ("title", "name"):
                    value = payload.get(field)
                    if usable_text(field, value):
                        return str(value)
        return None

    @staticmethod
    def _linked_item(rows: list[dict], kind: str) -> tuple[str | None, str | None]:
        if not rows:
            return None, None
        expected = "movie" if kind == "movie" else "episode"
        # Do not turn a parent series row into the episode/movie catalog item.
        # Older calendar rows may still contain that link until the next sync.
        selected = next((row for row in rows if row["entityType"] == expected), None)
        if selected is None:
            return None, None
        series_id = selected.get("seriesId")
        if not series_id and selected["entityType"] == "series":
            series_id = selected["entityId"]
        return selected["entityId"], series_id

    def list(self, user_id: str, start: datetime, end: datetime) -> dict:
        locale = AccountPreference(user_id).metadata_language()["language"]
        rows = self.db.execute(
            "SELECT DISTINCT e.id,e.provider,e.library_id,l.name,e.kind,e.release_type,e.event_at,e.event_date,e.all_day,"
            "e.tvdb_id,e.tmdb_id,e.series_tvdb_id,e.season_number,e.episode_number,e.has_file,e.monitored,e.state,"
            "x.entity_id,entity.entity_type,parent.id,parent.entity_type,grandparent.id,grandparent.entity_type "
            "FROM calendar_events e JOIN user_library_access access ON access.library_id=e.library_id AND access.user_id=? "
            "JOIN libraries l ON l.id=e.library_id LEFT JOIN calendar_event_entities x ON x.event_id=e.id "
            "LEFT JOIN library_entities entity ON entity.id=x.entity_id LEFT JOIN library_entities parent ON parent.id=entity.parent_id "
            "LEFT JOIN library_entities grandparent ON grandparent.id=parent.parent_id "
            "WHERE (e.event_at>=? AND e.event_at<=?) OR (e.all_day=1 AND e.event_date>=? AND e.event_date<=?) "
            "ORDER BY e.event_at,e.id",
            (
                user_id,
                start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat(),
                start.date().isoformat(),
                end.date().isoformat(),
            ),
        )
        grouped: dict[str, dict] = {}
        links: dict[str, list[dict]] = {}
        for row in rows:
            event_id = row[0]
            value = grouped.setdefault(
                event_id,
                {
                    "id": event_id,
                    "provider": row[1],
                    "libraryId": row[2],
                    "libraryName": row[3],
                    "kind": row[4],
                    "releaseType": row[5],
                    "eventAt": row[6],
                    "eventDate": row[7],
                    "allDay": bool(row[8]),
                    "tvdbId": row[9],
                    "tmdbId": row[10],
                    "seriesTvdbId": row[11],
                    "seasonNumber": row[12],
                    "episodeNumber": row[13],
                    "hasFile": bool(row[14]),
                    "monitored": bool(row[15]),
                    "state": row[16],
                },
            )
            if row[17]:
                links.setdefault(event_id, []).append(
                    {
                        "entityId": row[17],
                        "entityType": row[18],
                        "seriesId": (
                            row[21]
                            if row[22] == "series"
                            else row[19]
                            if row[20] == "series"
                            else None
                        ),
                    }
                )
        events = []
        for event_id, value in grouped.items():
            linked_rows = links.get(event_id, [])
            catalog_item_id, catalog_series_id = self._linked_item(linked_rows, value["kind"])
            future = not catalog_item_id
            provider = "tmdb" if value["kind"] == "movie" else "tvdb"
            entity_type = "movie" if value["kind"] == "movie" else "episode"
            provider_id = value["tmdbId"] if value["kind"] == "movie" else value["tvdbId"]
            title = self._title(provider, entity_type, provider_id, locale, future)
            series_title = self._title(
                "tvdb", "series", value["seriesTvdbId"], locale, future
            )
            value.update(
                {
                    "state": "existing" if catalog_item_id else "future",
                    "title": title,
                    "seriesTitle": series_title,
                    "catalogItemId": catalog_item_id,
                    "catalogSeriesId": catalog_series_id,
                    "metadataStatus": "future" if future else "catalog",
                }
            )
            value.pop("tvdbId", None)
            value.pop("tmdbId", None)
            value.pop("seriesTvdbId", None)
            events.append(value)
        return {"start": start.astimezone(timezone.utc).isoformat(), "end": end.astimezone(timezone.utc).isoformat(), "events": events}


def parse_calendar_window(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    current_start, current_end = calendar_window()
    try:
        parsed_start = datetime.fromisoformat(str(start).replace("Z", "+00:00")) if start else current_start
        parsed_end = datetime.fromisoformat(str(end).replace("Z", "+00:00")) if end else current_end
    except ValueError as error:
        raise ValueError("Calendar start and end must be ISO dates") from error
    if parsed_start.tzinfo is None:
        parsed_start = parsed_start.replace(tzinfo=timezone.utc)
    if parsed_end.tzinfo is None:
        parsed_end = parsed_end.replace(tzinfo=timezone.utc)
    parsed_start = parsed_start.astimezone(timezone.utc)
    parsed_end = parsed_end.astimezone(timezone.utc)
    if parsed_end <= parsed_start:
        raise ValueError("Calendar end must be after start")
    if parsed_end - parsed_start > timedelta(days=100):
        raise ValueError("Calendar windows cannot exceed 100 days")
    return parsed_start, parsed_end


class CalendarSyncJob:
    def __init__(self, store):
        self.store = store

    def run(self, run_id: str, should_terminate) -> None:
        if should_terminate():
            from app.library import JobTerminated

            raise JobTerminated()
        result = CalendarSyncService().sync()
        if should_terminate():
            from app.library import JobTerminated

            raise JobTerminated()
        message = f"Synced {result['events']} calendar events ({result['existing']} linked to catalog)."
        if result["errors"]:
            message += f" {len(result['errors'])} provider connection(s) failed."
        self.store.update_run(
            run_id,
            state="completed",
            progress_current=10000,
            progress_total=10000,
            message=message,
            error=None,
            finished_at=iso_now(),
        )


class CalendarFutureMetadataJob:
    def __init__(self, store):
        self.store = store

    def run(self, run_id: str, should_terminate) -> None:
        result = CalendarFutureMetadataService().refetch(should_terminate=should_terminate)
        message = f"Refetched future metadata for {result['updated']} locale documents across {result['targets']} identities."
        if result["errors"]:
            message += f" {len(result['errors'])} identities failed."
        self.store.update_run(
            run_id,
            state="completed",
            progress_current=10000,
            progress_total=10000,
            message=message,
            error=None,
            finished_at=iso_now(),
        )
