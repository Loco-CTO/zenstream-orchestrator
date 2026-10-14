from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from app.config import Config
from app.logging_config import get_logger
from app.metadata_domain import (
    fallback_tiers,
    language_family,
    locale_variants,
    usable_text,
)
from app.models.account_preference import AccountPreference
from app.models.calendar import (
    FutureMetadataCache,
    decrypt_calendar_api_key,
    encrypt_calendar_api_key,
)
from app.models.metadata import MetadataCache, MetadataLanguageSettings
from app.notifications import FollowService
from app.progress import format_progress_message
from app.providers import MetadataService, ProviderError

logger = get_logger("calendar")

CALENDAR_PROVIDERS = ("sonarr", "radarr")
EXPECTED_LIBRARY_TYPES = {"sonarr": "tv_series", "radarr": "movies"}
CALENDAR_PAST_WEEKS = 1
CALENDAR_FUTURE_WEEKS = 16
MAX_CALENDAR_WINDOW_DAYS = 130
CALENDAR_TITLE_PLACEHOLDERS = frozenset(
    {"tba", "tbd", "to be announced", "to be determined", "unknown"}
)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def calendar_window(
    current: datetime | None = None,
) -> tuple[datetime, datetime]:
    current = current or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    current_week = current - timedelta(days=(current.weekday() + 1) % 7)
    current_week = current_week.replace(hour=0, minute=0, second=0, microsecond=0)
    start = current_week - timedelta(weeks=CALENDAR_PAST_WEEKS)
    end = (
        current_week
        + timedelta(weeks=CALENDAR_FUTURE_WEEKS + 1)
        - timedelta(microseconds=1)
    )
    return start, end


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


def _calendar_identity_id(value) -> str | None:
    """Return a usable calendar identity, treating provider zero values as absent."""
    raw = _string_id(value)
    return raw if raw and raw != "0" else None


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
    if (
        not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
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

    @staticmethod
    def _public_row(row) -> dict:
        (
            provider,
            api_version,
            address,
            port,
            base_url,
            use_ssl,
            library_id,
            api_key,
            validated,
            last_sync,
            last_error,
            updated,
            library_name,
        ) = row
        return {
            "provider": provider,
            "apiVersion": api_version,
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
                "SELECT c.provider,c.api_version,c.address,c.port,c.base_url,c.use_ssl,"
                "c.library_id,c.api_key_ciphertext,"
                "c.validated_at,c.last_sync_at,c.last_error,c.updated_at,l.name "
                "FROM calendar_connections c JOIN libraries l "
                "ON l.id=c.library_id WHERE c.provider=?",
                (provider,),
            )
            values[provider] = (
                self._public_row(rows[0])
                if rows
                else {
                    "provider": provider,
                    "apiVersion": "v3",
                    "configured": False,
                    "apiKeyConfigured": False,
                }
            )
        return values

    def internal(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT provider,api_version,address,port,base_url,use_ssl,"
            "library_id,api_key_ciphertext "
            "FROM calendar_connections ORDER BY provider"
        )
        values = []
        for (
            provider,
            api_version,
            address,
            port,
            base_url,
            use_ssl,
            library_id,
            ciphertext,
        ) in rows:
            try:
                api_key = decrypt_calendar_api_key(ciphertext)
            except ValueError:
                logger.error(
                    "calendar API key could not be decrypted provider=%s", provider
                )
                continue
            values.append(
                {
                    "provider": provider,
                    "apiVersion": api_version,
                    "address": address,
                    "port": int(port),
                    "baseUrl": base_url,
                    "useSsl": bool(use_ssl),
                    "libraryId": library_id,
                    "apiKey": api_key,
                }
            )
        return values

    def _normalized_values(self, provider: str, values: dict) -> dict:
        """Validate and normalize the network and library fields for a connection."""
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
        expected_type = EXPECTED_LIBRARY_TYPES[provider]
        if library_rows[0][0] != expected_type:
            raise ValueError(
                f"{provider.title()} must be mapped to a {expected_type} library"
            )
        return {
            "address": address,
            "port": port,
            "baseUrl": base_url,
            "useSsl": use_ssl,
            "libraryId": library_id,
        }

    @staticmethod
    def _api_version(provider: str, requested: object, existing: list) -> str:
        """Select and validate the configured provider API version."""
        if requested is None:
            version = existing[0][1] if existing else "v3"
        else:
            version = str(requested).strip().lower()
        allowed = {"v3", "v5"} if provider == "sonarr" else {"v3"}
        if version not in allowed:
            if provider == "sonarr":
                raise ValueError("Sonarr API version must be v3 or v5")
            raise ValueError("Radarr API version must be v3")
        return version

    @staticmethod
    def _api_key(supplied: object, existing: list) -> str:
        """Encrypt a supplied key or preserve the previously saved key."""
        if supplied is not None and str(supplied).strip():
            return encrypt_calendar_api_key(str(supplied).strip())
        if existing:
            return existing[0][0]
        raise ValueError("An API key is required for a new calendar service")

    def save(self, provider: str, values: dict) -> dict:
        """Validate and persist one provider connection."""
        if provider not in CALENDAR_PROVIDERS:
            raise ValueError("Unsupported calendar provider")
        if not isinstance(values, dict):
            raise ValueError("Calendar settings must be an object")
        if values.get("enabled") is False:
            self.clear(provider)
            return self.public()[provider]
        normalized = self._normalized_values(provider, values)
        existing = self.db.execute(
            "SELECT api_key_ciphertext,api_version "
            "FROM calendar_connections WHERE provider=?",
            (provider,),
        )
        api_version = self._api_version(provider, values.get("apiVersion"), existing)
        ciphertext = self._api_key(values.get("apiKey"), existing)
        timestamp = iso_now()
        self.db.execute(
            "INSERT INTO calendar_connections("
            "provider,api_version,address,port,base_url,use_ssl,library_id,"
            "api_key_ciphertext,validated_at,last_sync_at,last_error,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "api_version=excluded.api_version,address=excluded.address,"
            "port=excluded.port,base_url=excluded.base_url,"
            "use_ssl=excluded.use_ssl,library_id=excluded.library_id,"
            "api_key_ciphertext=excluded.api_key_ciphertext,validated_at=NULL,"
            "last_error=NULL,updated_at=excluded.updated_at",
            (
                provider,
                api_version,
                normalized["address"],
                normalized["port"],
                normalized["baseUrl"],
                int(normalized["useSsl"]),
                normalized["libraryId"],
                ciphertext,
                None,
                None,
                None,
                timestamp,
            ),
        )
        return self.public()[provider]

    def clear(self, provider: str) -> None:
        self.db.execute(
            "DELETE FROM calendar_connections WHERE provider=?", (provider,)
        )
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
            configured_timeout = float(
                os.getenv("METADATA_PROVIDER_TIMEOUT_SECONDS", "20")
            )
        except ValueError:
            configured_timeout = 20
        self.timeout = max(3.0, min(60.0, configured_timeout))

    @property
    def endpoints(self) -> tuple[str, ...]:
        scheme = "https" if self.connection["useSsl"] else "http"
        base = self.connection["baseUrl"].rstrip("/")
        version = (
            str(self.connection.get("apiVersion") or "v3")
            if self.connection["provider"] == "sonarr"
            else "v3"
        )
        return (
            f"{scheme}://{self.connection['address']}:{self.connection['port']}"
            f"{base}/api/{version}/calendar",
        )

    @property
    def endpoint(self) -> str:
        return self.endpoints[0]

    def _params(self, start: datetime, end: datetime) -> dict:
        """Build the selected provider version's calendar query parameters."""
        params = {"start": start.date().isoformat(), "end": end.date().isoformat()}
        if self.connection["provider"] == "sonarr":
            if str(self.connection.get("apiVersion") or "v3").lower() == "v5":
                params.update(
                    {
                        "includeUnmonitored": "false",
                        "includeSubresources": ["series", "episodeFile"],
                    }
                )
            else:
                params.update(
                    {
                        "unmonitored": "false",
                        "includeSeries": "true",
                        "includeEpisodeFile": "true",
                        "includeEpisodeImages": "false",
                    }
                )
        else:
            params["unmonitored"] = "false"
            params["includeMovieFile"] = "true"
        return params

    def _request(self, endpoint: str, params: dict) -> httpx.Response:
        """Retry transient provider failures against the already selected endpoint."""
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
            attempt = 0
            while True:
                try:
                    response = client.get(endpoint, params=params)
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                else:
                    if response.status_code not in {429, 502, 503, 504} or attempt == 2:
                        return response
                time.sleep(min(5.0, 0.25 * (2**attempt)))
                attempt += 1

    @staticmethod
    def _response_items(payload: object) -> list[dict]:
        """Return calendar records from either supported response envelope."""
        if isinstance(payload, list):
            return [value for value in payload if isinstance(value, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return [value for value in payload["data"] if isinstance(value, dict)]
        raise ProviderError("calendar provider returned an invalid response")

    def fetch(self, start: datetime, end: datetime) -> list[dict]:
        """Fetch events from the configured calendar endpoint."""
        params = self._params(start, end)
        started = time.monotonic()
        logger.info(
            "calendar provider request start provider=%s endpoint=%s start=%s end=%s",
            self.connection["provider"],
            self.endpoint,
            params["start"],
            params["end"],
        )
        try:
            endpoint = self.endpoint
            attempted_endpoint = endpoint
            response = self._request(endpoint, params)
            if response.status_code == 404:
                raise ProviderError(f"calendar endpoint was not found: {endpoint}")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            logger.warning(
                "calendar provider request failed provider=%s endpoint=%s duration_seconds=%.1f error=%s",
                self.connection["provider"],
                attempted_endpoint,
                time.monotonic() - started,
                error,
            )
            raise ProviderError(
                f"{self.connection['provider']} calendar request failed: {type(error).__name__}: {error}"
            ) from error
        return self._response_items(payload)


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
    monitored: bool = True


def _normalize_sonarr(item: dict, library_id: str) -> CalendarEventValue | None:
    source_id = _string_id(item.get("id"))
    tvdb_id = _calendar_identity_id(item.get("tvdbId")) or _calendar_identity_id(
        item.get("tvdbID")
    )
    series = item.get("series") if isinstance(item.get("series"), dict) else {}
    series_tvdb_id = _calendar_identity_id(
        series.get("tvdbId")
    ) or _calendar_identity_id(series.get("tvdbID"))
    parsed = _parse_datetime(item.get("airDateUtc")) or _parse_datetime(
        item.get("airDate")
    )
    if not source_id or not (tvdb_id or series_tvdb_id) or not parsed:
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
                monitored=bool(item.get("monitored", True)),
            )
        )
    return values


def normalize_calendar_events(
    provider: str, library_id: str, payload: Iterable[dict]
) -> list[CalendarEventValue]:
    values = []
    for item in payload:
        if provider == "sonarr":
            normalized = _normalize_sonarr(item, library_id)
            if normalized:
                values.append(normalized)
        else:
            values.extend(_normalize_radarr(item, library_id))
    return values


def _future_metadata_identity_targets(
    kind: str,
    tvdb_id: str | None = None,
    series_tvdb_id: str | None = None,
    tmdb_id: str | None = None,
) -> tuple[tuple[str, str, str], ...]:
    values = []
    if kind == "episode":
        if tvdb_id:
            values.append(("tvdb", "episode", str(tvdb_id)))
        if series_tvdb_id:
            values.append(("tvdb", "series", str(series_tvdb_id)))
    elif tmdb_id:
        values.append(("tmdb", "movie", str(tmdb_id)))
    return tuple(dict.fromkeys(values))


def _event_future_metadata_targets(
    event: CalendarEventValue,
) -> tuple[tuple[str, str, str], ...]:
    return _future_metadata_identity_targets(
        event.kind,
        tvdb_id=event.tvdb_id,
        series_tvdb_id=event.series_tvdb_id,
        tmdb_id=event.tmdb_id,
    )


def _catalog_entity_identities(
    kind: str,
    *,
    tvdb_id: str | None = None,
    series_tvdb_id: str | None = None,
    tmdb_id: str | None = None,
) -> tuple[tuple[str, str, str], ...]:
    """Return provider identities used to link a calendar event to catalog entities."""
    values: list[tuple[str, str, str]] = []
    if kind == "episode":
        if tvdb_id:
            values.append(("tvdb", str(tvdb_id), "episode"))
        if series_tvdb_id:
            values.append(("tvdb", str(series_tvdb_id), "series"))
    elif tmdb_id:
        values.append(("tmdb", str(tmdb_id), "movie"))
    return tuple(dict.fromkeys(values))


def _episode_matches_by_series_number(
    execute,
    library_id: str,
    series_tvdb_id: str | None,
    season_number: int | None,
    episode_number: int | None,
) -> list[tuple[str, str]]:
    """Resolve a Sonarr episode when its episode-level TVDB ID is unavailable."""
    if not series_tvdb_id or season_number is None or episode_number is None:
        return []
    try:
        season_number = int(season_number)
        episode_number = int(episode_number)
    except (TypeError, ValueError):
        return []

    rows = execute(
        "SELECT episode.id,episode.entity_type "
        "FROM entity_provider_ids series_identity "
        "JOIN library_entities series ON series.id=series_identity.entity_id "
        "JOIN library_entities season ON season.parent_id=series.id "
        "AND season.entity_type='season' AND season.season_number=? "
        "JOIN library_entities episode ON episode.parent_id=season.id "
        "AND episode.entity_type='episode' AND episode.episode_number=? "
        "WHERE series.library_id=? AND series.entity_type='series' "
        "AND series_identity.provider='tvdb' "
        "AND series_identity.identifier_type='series' "
        "AND series_identity.provider_id=?",
        (season_number, episode_number, library_id, str(series_tvdb_id)),
    )
    return [(row[0], row[1]) for row in rows]


def _matching_catalog_entities(
    execute,
    library_id: str,
    kind: str,
    tvdb_id: str | None,
    series_tvdb_id: str | None,
    tmdb_id: str | None,
    season_number: int | None,
    episode_number: int | None,
) -> list[tuple[str, str]]:
    """Return provider identity matches, including a numbering fallback for episodes."""
    matches = []
    for provider, provider_id, entity_type in _catalog_entity_identities(
        kind,
        tvdb_id=tvdb_id,
        series_tvdb_id=series_tvdb_id,
        tmdb_id=tmdb_id,
    ):
        rows = execute(
            "SELECT e.id,e.entity_type FROM entity_provider_ids p "
            "JOIN library_entities e ON e.id=p.entity_id "
            "WHERE e.library_id=? AND p.provider=? AND p.provider_id=? "
            "AND e.entity_type=?",
            (library_id, provider, provider_id, entity_type),
        )
        matches.extend((row[0], row[1]) for row in rows)
    if kind == "episode" and not any(
        entity_type == "episode" for _, entity_type in matches
    ):
        matches.extend(
            _episode_matches_by_series_number(
                execute,
                library_id,
                series_tvdb_id,
                season_number,
                episode_number,
            )
        )
    return list(dict.fromkeys(matches))


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
                f"{event.provider}:{event.library_id}:{event.source_event_id}:{event.release_type}:{event.event_at}",
            )
        )

    def _matching_entities(self, event: CalendarEventValue) -> list[tuple[str, str]]:
        return _matching_catalog_entities(
            self.db.execute,
            event.library_id,
            event.kind,
            event.tvdb_id,
            event.series_tvdb_id,
            event.tmdb_id,
            event.season_number,
            event.episode_number,
        )

    def reconcile_catalog_links(self, library_id: str) -> int:
        """Relink stored calendar events after catalog admission or cleanup.

        Calendar provider events can outlive the catalog entity they describe.
        A full provider sync used to be the only path that rebuilt this
        relationship, so newly admitted media remained marked as future until
        that sync ran. Reconcile the complete library event set in one writer
        transaction so links and event state cannot be observed half-updated.
        """
        required_tables = {
            "calendar_events",
            "calendar_event_entities",
            "entity_provider_ids",
            "library_entities",
        }
        available_tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)",
                tuple(sorted(required_tables)),
            )
        }
        if not required_tables <= available_tables:
            return 0

        changed_events = 0
        promoted_identities: set[tuple[str, str, str]] = set()
        updated_at = iso_now()
        with self.db.transaction() as cursor:

            def execute_rows(query, parameters):
                """Fetch lookup rows through the active reconciliation transaction."""
                return cursor.execute(query, parameters).fetchall()

            event_rows = cursor.execute(
                "SELECT id,kind,tvdb_id,tmdb_id,series_tvdb_id,"
                "season_number,episode_number,state "
                "FROM calendar_events WHERE library_id=? ORDER BY id",
                (library_id,),
            ).fetchall()
            for (
                event_id,
                kind,
                tvdb_id,
                tmdb_id,
                series_tvdb_id,
                season_number,
                episode_number,
                current_state,
            ) in event_rows:
                expected_entity_type = "movie" if kind == "movie" else "episode"
                matches = _matching_catalog_entities(
                    execute_rows,
                    library_id,
                    kind,
                    tvdb_id=tvdb_id,
                    series_tvdb_id=series_tvdb_id,
                    tmdb_id=tmdb_id,
                    season_number=season_number,
                    episode_number=episode_number,
                )
                matched_ids = sorted(
                    {
                        entity_id
                        for entity_id, entity_type in matches
                        if entity_type == expected_entity_type
                    }
                )
                linked_ids = sorted(
                    row[0]
                    for row in cursor.execute(
                        "SELECT entity_id FROM calendar_event_entities WHERE event_id=?",
                        (event_id,),
                    ).fetchall()
                )
                next_state = "existing" if matched_ids else "future"
                if linked_ids != matched_ids:
                    cursor.execute(
                        "DELETE FROM calendar_event_entities WHERE event_id=?",
                        (event_id,),
                    )
                    if matched_ids:
                        cursor.executemany(
                            "INSERT INTO calendar_event_entities(event_id,entity_id) VALUES(?,?)",
                            [(event_id, entity_id) for entity_id in matched_ids],
                        )
                if linked_ids != matched_ids or current_state != next_state:
                    cursor.execute(
                        "UPDATE calendar_events SET state=?,updated_at=? WHERE id=?",
                        (next_state, updated_at, event_id),
                    )
                    changed_events += 1
                if matched_ids and (
                    linked_ids != matched_ids or current_state != next_state
                ):
                    promoted_identities.update(
                        _future_metadata_identity_targets(
                            kind,
                            tvdb_id=tvdb_id,
                            series_tvdb_id=series_tvdb_id,
                            tmdb_id=tmdb_id,
                        )
                    )

        for provider, entity_type, provider_id in sorted(promoted_identities):
            self.future_cache.promote_identity(provider, entity_type, provider_id)
        return changed_events

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
            0,
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
        self.db.execute(
            "DELETE FROM calendar_event_entities WHERE event_id=?", (event_id,)
        )
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
        self,
        provider: str,
        library_id: str,
        payload: Iterable[dict],
        seen_at: str,
        start: datetime,
        end: datetime,
        future_metadata_targets: set[tuple[str, str, str]] | None = None,
    ) -> tuple[int, int]:
        start_date = start.date().isoformat()
        end_date = end.date().isoformat()
        payload = list(payload)
        all_normalized = normalize_calendar_events(provider, library_id, payload)
        if provider == "sonarr" and payload and not all_normalized:
            raise ProviderError(
                "Sonarr returned calendar entries without usable "
                "episode or series identities"
            )
        normalized = [
            event
            for event in all_normalized
            if start_date <= event.event_date <= end_date
        ]
        existing = 0
        for event in normalized:
            if self._upsert(event, seen_at):
                existing += 1
            elif future_metadata_targets is not None:
                future_metadata_targets.update(_event_future_metadata_targets(event))
        self.db.execute(
            "DELETE FROM calendar_events WHERE provider=? AND library_id=? AND last_seen_at<>?",
            (provider, library_id, seen_at),
        )
        return len(normalized), existing

    def sync(
        self,
        should_terminate=None,
        metadata_progress: Callable[[int, int, str | None, bool], None] | None = None,
    ) -> dict:
        should_terminate = should_terminate or (lambda: False)
        start, end = calendar_window()
        results = {
            "providers": 0,
            "provider_events": {},
            "events": 0,
            "existing": 0,
            "errors": [],
            "metadata": {"targets": 0, "updated": 0, "errors": []},
        }
        future_metadata_targets: set[tuple[str, str, str]] = set()
        for connection in self.connections.internal():
            if should_terminate():
                from app.library import JobTerminated

                raise JobTerminated()
            provider = connection["provider"]
            try:
                payload = ArrCalendarClient(connection).fetch(start, end)
                seen_at = iso_now()
                events, existing = self._replace_provider_events(
                    provider,
                    connection["libraryId"],
                    payload,
                    seen_at,
                    start,
                    end,
                    future_metadata_targets,
                )
                self.connections.record_sync(provider)
                results["providers"] += 1
                results["provider_events"][provider] = events
                results["events"] += events
                results["existing"] += existing
            except Exception as error:
                message = f"{provider}: {type(error).__name__}: {error}"
                logger.warning(
                    "calendar sync failed provider=%s error=%s", provider, error
                )
                self.connections.record_sync(provider, error=message[:500])
                results["provider_events"][provider] = 0
                results["errors"].append(message[:500])

        if future_metadata_targets:
            try:
                results["metadata"] = CalendarFutureMetadataService().refetch(
                    should_terminate=should_terminate,
                    force=False,
                    progress=metadata_progress,
                    targets=future_metadata_targets,
                )
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                logger.warning("immediate future metadata fetch failed error=%s", error)
                results["metadata"]["errors"].append(message[:500])
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
            "WHERE e.event_date>=? AND e.event_date<=? AND (e.state='future' OR NOT EXISTS ("
            "SELECT 1 FROM calendar_event_entities x JOIN library_entities linked ON linked.id=x.entity_id "
            "WHERE x.event_id=e.id AND linked.entity_type=CASE WHEN e.kind='movie' THEN 'movie' ELSE 'episode' END"
            "))",
            (start.date().isoformat(), end.date().isoformat()),
        )
        values = set()
        for kind, tvdb_id, series_tvdb_id, tmdb_id in rows:
            values.update(
                _future_metadata_identity_targets(
                    kind,
                    tvdb_id=tvdb_id,
                    series_tvdb_id=series_tvdb_id,
                    tmdb_id=tmdb_id,
                )
            )
        return sorted(values)

    def refetch(
        self,
        should_terminate=None,
        force: bool = True,
        progress: Callable[[int, int, str | None, bool], None] | None = None,
        *,
        targets: Iterable[tuple[str, str, str]] | None = None,
    ) -> dict:
        should_terminate = should_terminate or (lambda: False)
        locales = MetadataLanguageSettings().get()
        values = {"targets": 0, "updated": 0, "errors": []}
        if targets is None:
            targets = self._targets()
        else:
            targets = sorted(set(targets))
        total = len(targets)
        if progress:
            progress(0, total, None, False)
        for index, (provider, entity_type, provider_id) in enumerate(targets, 1):
            if should_terminate():
                from app.library import JobTerminated

                raise JobTerminated()
            values["targets"] += 1
            failed = False
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
                values["updated"] += len(documents)
            except Exception as error:
                failed = True
                message = f"{provider} {entity_type} {provider_id}: {type(error).__name__}: {error}"
                logger.warning(
                    "future metadata refetch failed identity=%s error=%s",
                    provider_id,
                    error,
                )
                values["errors"].append(message[:500])
            finally:
                if progress:
                    progress(
                        index,
                        total,
                        f"{provider.upper()} {entity_type} {provider_id}",
                        failed,
                    )
        self.cache.prune_expired()
        return values


class CalendarReadService:
    def __init__(self):
        self.db = Config().database
        self.normal_cache = MetadataCache()
        self.future_cache = FutureMetadataCache()

    @staticmethod
    def _usable_title(value) -> bool:
        if not usable_text("title", value):
            return False
        normalized = " ".join(str(value).split()).casefold()
        return normalized not in CALENDAR_TITLE_PLACEHOLDERS

    def _payload(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        future: bool,
    ) -> dict | None:
        cache = self.future_cache if future else self.normal_cache
        return cache.get(provider, entity_type, provider_id, locale)

    def _title(
        self,
        provider: str,
        entity_type: str,
        provider_id: str | None,
        locale: str,
        future: bool,
    ) -> str | None:
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
                    if self._usable_title(value):
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
        return selected["entityId"], series_id

    def list(self, user_id: str, start: datetime, end: datetime) -> dict:
        locale = AccountPreference(user_id).metadata_language()["language"]
        rows = self.db.execute(
            "SELECT DISTINCT e.id,e.provider,e.library_id,l.name,e.kind,e.release_type,e.event_at,e.event_date,e.all_day,"
            "e.tvdb_id,e.tmdb_id,e.series_tvdb_id,e.season_number,e.episode_number,"
            "CASE WHEN EXISTS ("
            "SELECT 1 FROM calendar_event_entities playable_link "
            "JOIN library_entities playable_entity ON playable_entity.id=playable_link.entity_id "
            "JOIN media_files playable_media ON playable_media.entity_id=playable_entity.id "
            "WHERE playable_link.event_id=e.id AND playable_entity.library_id=e.library_id "
            "AND playable_entity.entity_type=CASE WHEN e.kind='movie' THEN 'movie' ELSE 'episode' END "
            "AND playable_media.role='media'"
            ") THEN 1 ELSE 0 END,e.monitored,e.state,"
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
            catalog_item_id, catalog_series_id = self._linked_item(
                linked_rows, value["kind"]
            )
            future = not catalog_item_id
            provider = "tmdb" if value["kind"] == "movie" else "tvdb"
            entity_type = "movie" if value["kind"] == "movie" else "episode"
            provider_id = (
                value["tmdbId"] if value["kind"] == "movie" else value["tvdbId"]
            )
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
            follow_entity_id = (
                catalog_item_id if value["kind"] == "movie" else catalog_series_id
            )
            follow_provider_id = (
                value["tmdbId"] if value["kind"] == "movie" else value["seriesTvdbId"]
            )
            follow_available = bool(follow_provider_id or follow_entity_id)
            value["followAvailable"] = follow_available
            value["following"] = (
                FollowService(self.db).following_for_identity(
                    user_id,
                    value["libraryId"],
                    "movie" if value["kind"] == "movie" else "series",
                    "tmdb" if value["kind"] == "movie" else "tvdb",
                    str(follow_provider_id) if follow_provider_id else None,
                    follow_entity_id,
                )
                if follow_available
                else False
            )
            value.pop("tvdbId", None)
            value.pop("tmdbId", None)
            value.pop("seriesTvdbId", None)
            events.append(value)
        return {
            "start": start.astimezone(timezone.utc).isoformat(),
            "end": end.astimezone(timezone.utc).isoformat(),
            "events": events,
        }


def parse_calendar_window(
    start: str | None, end: str | None
) -> tuple[datetime, datetime]:
    current_start, current_end = calendar_window()
    try:
        parsed_start = (
            datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            if start
            else current_start
        )
        parsed_end = (
            datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            if end
            else current_end
        )
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
    if parsed_end - parsed_start > timedelta(days=MAX_CALENDAR_WINDOW_DAYS):
        raise ValueError(
            f"Calendar windows cannot exceed {MAX_CALENDAR_WINDOW_DAYS} days"
        )
    return parsed_start, parsed_end


class CalendarSyncJob:
    def __init__(self, store):
        self.store = store

    def run(self, run_id: str, should_terminate) -> None:
        if should_terminate():
            from app.library import JobTerminated

            raise JobTerminated()

        def metadata_progress(
            current: int, total: int, item: str | None, failed: bool
        ) -> None:
            label = "Fetching new calendar metadata"
            if failed:
                label = "Calendar metadata identity failed"
            self.store.update_run(
                run_id,
                progress_current=current,
                progress_total=total,
                progress_phase="metadata",
                progress_label=label,
                progress_stage_current=current,
                progress_stage_total=total,
                progress_stage_unit="identities",
                progress_current_item=item,
                message=format_progress_message(
                    label,
                    item=item,
                    current=current,
                    total=total,
                    unit="identities",
                ),
            )

        result = CalendarSyncService().sync(
            should_terminate=should_terminate,
            metadata_progress=metadata_progress,
        )
        if should_terminate():
            from app.library import JobTerminated

            raise JobTerminated()
        provider_counts = result.get("provider_events", {})
        provider_summary = ", ".join(
            f"{provider.title()}: {provider_counts[provider]}"
            for provider in CALENDAR_PROVIDERS
            if provider in provider_counts
        )
        if provider_summary:
            message = (
                f"Synced {result['events']} calendar events ({provider_summary}; "
                f"{result['existing']} linked to catalog)."
            )
        else:
            message = (
                f"Synced {result['events']} calendar events "
                f"({result['existing']} linked to catalog)."
            )
        if result["errors"]:
            message += f" {len(result['errors'])} provider connection(s) failed."
        metadata = result["metadata"]
        if metadata["targets"]:
            message += (
                f" Fetched metadata for {metadata['targets']} new calendar identities."
            )
        if metadata["errors"]:
            message += (
                f" {len(metadata['errors'])} calendar metadata identity(ies) failed."
            )
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
        def progress(current: int, total: int, item: str | None, failed: bool) -> None:
            label = "Refetching future metadata"
            if failed:
                label = "Future metadata identity failed"
            self.store.update_run(
                run_id,
                progress_current=current,
                progress_total=total,
                progress_phase="metadata",
                progress_label=label,
                progress_stage_current=current,
                progress_stage_total=total,
                progress_stage_unit="identities",
                progress_current_item=item,
                message=format_progress_message(
                    label,
                    item=item,
                    current=current,
                    total=total,
                    unit="identities",
                ),
            )

        result = CalendarFutureMetadataService().refetch(
            should_terminate=should_terminate,
            progress=progress,
        )
        message = f"Refetched future metadata for {result['updated']} locale documents across {result['targets']} identities."
        if result["errors"]:
            message += f" {len(result['errors'])} identities failed."
        self.store.update_run(
            run_id,
            state="completed",
            progress_current=10000,
            progress_total=10000,
            progress_phase="finalization",
            progress_label="Future metadata complete",
            progress_stage_current=result["targets"],
            progress_stage_total=result["targets"],
            progress_stage_unit="identities",
            progress_current_item=None,
            message=message,
            error=None,
            finished_at=iso_now(),
        )
