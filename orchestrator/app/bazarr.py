from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from app.catalog import Catalog
from app.client_auth import issue_ticket, read_ticket
from app.config import Config
from app.library import sidecar_media_path
from app.logging_config import get_logger
from app.models.metadata import _fernet
from cryptography.fernet import InvalidToken
from fastapi import HTTPException

logger = get_logger("bazarr")

BAZARR_MATCH_TTL_SECONDS = 15 * 60
BAZARR_LOOKUP_CACHE_SECONDS = 20.0
BAZARR_NEGATIVE_LOOKUP_CACHE_SECONDS = 3.0
BAZARR_EMPTY_SEARCH_RETRY_DELAY_SECONDS = 0.25
BAZARR_SYNC_PENDING_MESSAGE = "The subtitle downloader mapping is being refreshed."
_ADDRESS_RE = re.compile(r"^[^\s/:]+$")

_bazarr_cache_lock = threading.Lock()
_bazarr_series_cache: dict[tuple, tuple[float, list[dict]]] = {}
_bazarr_episode_cache: dict[tuple, tuple[float, list[dict]]] = {}
_bazarr_movie_cache: dict[tuple, tuple[float, list[dict]]] = {}
_bazarr_resolution_cache: dict[
    tuple, tuple[float, dict | None, tuple[str, str] | None]
] = {}


class BazarrError(RuntimeError):
    """A transport or payload error from the configured Bazarr instance."""


class BazarrMatchError(BazarrError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def encrypt_bazarr_api_key(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_bazarr_api_key(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as error:
        raise ValueError(
            "Stored Bazarr API key cannot be decrypted; enter it again."
        ) from error


def _normalize_address(value: object) -> str:
    address = str(value or "").strip()
    if not address or len(address) > 255 or not _ADDRESS_RE.fullmatch(address):
        raise ValueError("address must be a non-empty host or IP address")
    if "@" in address:
        raise ValueError("address must be a host or IP address")
    parsed = urlparse(f"//{address}")
    if (
        not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("address must be a host or IP address")
    return address


def _normalize_base_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" in raw or "?" in raw or "#" in raw:
        raise ValueError("baseUrl must be a path, not a URL or query string")
    if not raw.startswith("/"):
        raw = "/" + raw
    normalized = "/" + "/".join(part for part in raw.split("/") if part)
    return "" if normalized == "/" else normalized


def _parse_port(value: object) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("port must be between 1 and 65535") from error
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def _path_key(value: object) -> str:
    """Normalize Windows/POSIX paths for exact cross-container comparison."""
    original = str(value or "").strip()
    windows_style = bool(re.match(r"^[A-Za-z]:", original)) or "\\" in original
    raw = original.replace("\\", "/")
    if not raw:
        return ""
    prefix = ""
    if re.match(r"^[A-Za-z]:", raw):
        prefix, raw = raw[:2], raw[2:]
    absolute = raw.startswith("/")
    parts: list[str] = []
    for part in raw.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not absolute:
                parts.append(part)
            continue
        parts.append(part)
    normalized = "/".join(parts)
    if absolute:
        normalized = "/" + normalized
    if prefix:
        normalized = prefix + normalized
    normalized = normalized.rstrip("/") or "/"
    return normalized.casefold() if windows_style else normalized


def mapped_path(root: str, relative_path: str) -> str:
    root_value = str(root or "").strip().replace("\\", "/").rstrip("/")
    relative_value = str(relative_path or "").strip().replace("\\", "/").lstrip("/")
    if not root_value or not relative_value:
        raise ValueError("A Bazarr root path and relative media path are required")
    return f"{root_value}/{relative_value}"


def _effective_bazarr_root(
    mapping_root: object, library_directory: object
) -> str | None:
    for value in (mapping_root, library_directory):
        root = str(value or "").strip()
        if root:
            return root
    return None


def path_is_under(path: str, root: str) -> bool:
    path_value = _path_key(path)
    root_value = _path_key(root)
    return path_value == root_value or path_value.startswith(root_value + "/")


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _integer(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _data_list(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    raise BazarrError("Bazarr returned an invalid list response")


def _timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class BazarrConnectionStore:
    def __init__(self):
        self.db = Config().database

    def _libraries(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id,name FROM libraries WHERE type IN ('tv_series','movies') ORDER BY sort_order,name COLLATE NOCASE"
        )
        return [{"id": row[0], "name": row[1]} for row in rows]

    def public(self) -> dict:
        rows = self.db.execute(
            "SELECT address,port,base_url,use_ssl,api_key_ciphertext,updated_at FROM bazarr_settings WHERE id=1"
        )
        settings = rows[0] if rows else None
        mappings = self.db.execute(
            "SELECT m.library_id,m.bazarr_root_path,m.updated_at,l.name "
            "FROM bazarr_library_mappings m JOIN libraries l ON l.id=m.library_id "
            "ORDER BY l.sort_order,l.name COLLATE NOCASE"
        )
        return {
            "configured": bool(settings),
            "address": settings[0] if settings else None,
            "port": int(settings[1]) if settings else None,
            "baseUrl": settings[2] if settings else "",
            "useSsl": bool(settings[3]) if settings else False,
            "apiKeyConfigured": bool(settings and settings[4]),
            "updatedAt": settings[5] if settings else None,
            "mappings": [
                {
                    "libraryId": row[0],
                    "libraryName": row[3],
                    "bazarrRootPath": row[1],
                    "updatedAt": row[2],
                }
                for row in mappings
            ],
            "libraries": self._libraries(),
        }

    def internal(self) -> dict | None:
        rows = self.db.execute(
            "SELECT address,port,base_url,use_ssl,api_key_ciphertext FROM bazarr_settings WHERE id=1"
        )
        if not rows:
            return None
        row = rows[0]
        try:
            api_key = decrypt_bazarr_api_key(row[4])
        except ValueError:
            logger.error("Bazarr API key could not be decrypted")
            return None
        mappings = self.db.execute(
            "SELECT library_id,bazarr_root_path FROM bazarr_library_mappings ORDER BY library_id"
        )
        return {
            "address": row[0],
            "port": int(row[1]),
            "baseUrl": row[2] or "",
            "useSsl": bool(row[3]),
            "apiKey": api_key,
            "mappings": {
                row[0]: row[1] for row in mappings if row[1] and str(row[1]).strip()
            },
        }

    def save(self, values: dict) -> dict:
        if not isinstance(values, dict):
            raise ValueError("Bazarr settings must be an object")
        if values.get("enabled") is False:
            self.clear()
            return self.public()
        address = _normalize_address(values.get("address"))
        port = _parse_port(values.get("port"))
        base_url = _normalize_base_url(values.get("baseUrl"))
        use_ssl = values.get("useSsl")
        if type(use_ssl) is not bool:
            raise ValueError("useSsl must be a boolean")
        existing = self.db.execute(
            "SELECT api_key_ciphertext FROM bazarr_settings WHERE id=1"
        )
        supplied_key = values.get("apiKey")
        if supplied_key is not None and str(supplied_key).strip():
            ciphertext = encrypt_bazarr_api_key(str(supplied_key).strip())
        elif existing:
            ciphertext = existing[0][0]
        else:
            raise ValueError("An API key is required for a new Bazarr service")

        raw_mappings = values.get("mappings")
        if not isinstance(raw_mappings, list):
            raise ValueError("mappings must be an array")
        normalized_mappings: dict[str, str] = {}
        for mapping in raw_mappings:
            if not isinstance(mapping, dict):
                raise ValueError("Each Bazarr mapping must be an object")
            library_id = str(mapping.get("libraryId") or "").strip()
            root_path = str(mapping.get("bazarrRootPath") or "").strip()
            if not library_id or not root_path or len(root_path) > 4096:
                raise ValueError("Each Bazarr mapping needs a library and root path")
            library_rows = self.db.execute(
                "SELECT type FROM libraries WHERE id=?", (library_id,)
            )
            if not library_rows:
                raise ValueError("Selected Bazarr library does not exist")
            if library_rows[0][0] not in {"tv_series", "movies"}:
                raise ValueError("Bazarr can only be mapped to TV or movie libraries")
            normalized_mappings[library_id] = root_path.rstrip("/\\") or root_path

        updated_at = _timestamp()
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO bazarr_settings(id,address,port,base_url,use_ssl,api_key_ciphertext,updated_at) "
                "VALUES(1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET address=excluded.address,port=excluded.port,"
                "base_url=excluded.base_url,use_ssl=excluded.use_ssl,api_key_ciphertext=excluded.api_key_ciphertext,"
                "updated_at=excluded.updated_at",
                (address, port, base_url, int(use_ssl), ciphertext, updated_at),
            )
            cursor.execute("DELETE FROM bazarr_library_mappings")
            cursor.execute("DELETE FROM bazarr_episode_mappings")
            cursor.execute("DELETE FROM bazarr_series_mappings")
            cursor.execute("DELETE FROM bazarr_movie_mappings")
            cursor.executemany(
                "INSERT INTO bazarr_library_mappings(library_id,bazarr_root_path,updated_at) VALUES(?,?,?)",
                [
                    (library_id, root_path, updated_at)
                    for library_id, root_path in normalized_mappings.items()
                ],
            )
        return self.public()

    def clear(self) -> None:
        with self.db.transaction() as cursor:
            cursor.execute("DELETE FROM bazarr_library_mappings")
            cursor.execute("DELETE FROM bazarr_episode_mappings")
            cursor.execute("DELETE FROM bazarr_series_mappings")
            cursor.execute("DELETE FROM bazarr_movie_mappings")
            cursor.execute("DELETE FROM bazarr_settings WHERE id=1")


class BazarrClient:
    def __init__(self, connection: dict):
        self.connection = connection
        try:
            configured_timeout = float(
                os.getenv("METADATA_PROVIDER_TIMEOUT_SECONDS", "20")
            )
        except ValueError:
            configured_timeout = 20.0
        self.timeout = max(3.0, min(90.0, configured_timeout))
        self._http_client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "application/json",
                "X-Api-Key": self.connection["apiKey"],
                "User-Agent": "ZenStream/Bazarr",
            },
        )

    @property
    def cache_key(self) -> tuple[str, int, str, bool]:
        return (
            str(self.connection["address"]),
            int(self.connection["port"]),
            str(self.connection.get("baseUrl") or ""),
            bool(self.connection.get("useSsl")),
        )

    def close(self) -> None:
        self._http_client.close()

    @property
    def base_url(self) -> str:
        scheme = "https" if self.connection["useSsl"] else "http"
        base = str(self.connection.get("baseUrl") or "").rstrip("/")
        return f"{scheme}://{self.connection['address']}:{self.connection['port']}{base}/api"

    def request(self, method: str, path: str, params=None, json_body=None):
        url = f"{self.base_url}/{path.lstrip('/')}"
        response = None
        try:
            response = self._http_client.request(
                method, url, params=params, json=json_body
            )
            if response.status_code == 404:
                raise BazarrError("Bazarr endpoint was not found")
            response.raise_for_status()
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as error:
                raise BazarrError("Bazarr returned invalid JSON") from error
        except (httpx.HTTPError, ValueError) as error:
            raise BazarrError(
                f"Bazarr request failed: {type(error).__name__}"
            ) from error
        finally:
            if response is not None:
                response.close()

    def series(self) -> list[dict]:
        key = self.cache_key
        now = time.monotonic()
        with _bazarr_cache_lock:
            cached = _bazarr_series_cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
        value = _data_list(
            self.request("GET", "/series", params={"start": 0, "length": -1})
        )
        with _bazarr_cache_lock:
            _bazarr_series_cache[key] = (now + BAZARR_LOOKUP_CACHE_SECONDS, value)
        return value

    def episodes(self, series_id: int) -> list[dict]:
        key = (*self.cache_key, series_id)
        now = time.monotonic()
        with _bazarr_cache_lock:
            cached = _bazarr_episode_cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
        value = _data_list(
            self.request("GET", "/episodes", params=[("seriesid[]", str(series_id))])
        )
        with _bazarr_cache_lock:
            _bazarr_episode_cache[key] = (now + BAZARR_LOOKUP_CACHE_SECONDS, value)
        return value

    def movies(self) -> list[dict]:
        key = self.cache_key
        now = time.monotonic()
        with _bazarr_cache_lock:
            cached = _bazarr_movie_cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
        value = _data_list(
            self.request("GET", "/movies", params={"start": 0, "length": -1})
        )
        with _bazarr_cache_lock:
            _bazarr_movie_cache[key] = (now + BAZARR_LOOKUP_CACHE_SECONDS, value)
        return value

    def search(self, episode_id: int) -> list[dict]:
        return _data_list(
            self.request("GET", "/providers/episodes", params={"episodeid": episode_id})
        )

    def search_movie(self, movie_id: int) -> list[dict]:
        return _data_list(
            self.request("GET", "/providers/movies", params={"radarrid": movie_id})
        )

    def download(self, values: dict) -> None:
        self.request(
            "POST",
            "/providers/episodes",
            params={
                "seriesid": values["seriesId"],
                "episodeid": values["episodeId"],
                "hi": "True" if values["hi"] else "False",
                "forced": "True" if values["forced"] else "False",
                "original_format": "True" if values["originalFormat"] else "False",
                "provider": values["provider"],
                "subtitle": values["subtitle"],
            },
        )

    def download_movie(self, values: dict) -> None:
        self.request(
            "POST",
            "/providers/movies",
            params={
                "radarrid": values["movieId"],
                "hi": "True" if values["hi"] else "False",
                "forced": "True" if values["forced"] else "False",
                "original_format": "True" if values["originalFormat"] else "False",
                "provider": values["provider"],
                "subtitle": values["subtitle"],
            },
        )


@dataclass(frozen=True)
class BazarrTarget:
    user_id: str
    entity_id: str
    source_id: str
    media_file_id: str
    library_id: str
    series_entity_id: str
    library_directory: str
    relative_path: str
    series_relative_path: str
    season_number: int | None
    episode_number: int | None
    size: int | None
    modified_ns: int | None
    quick_fingerprint: str | None
    bazarr_root_path: str | None
    target_path: str | None
    local_subtitles: tuple[dict, ...]
    series_provider_ids: tuple[tuple[str, str], ...]
    media_type: str = "episode"
    movie_provider_ids: tuple[tuple[str, str], ...] = ()


def _target(user_id: str, entity_id: str, source_id: str | None) -> BazarrTarget:
    if not source_id:
        raise HTTPException(400, "A playback source is required.")
    catalog = Catalog()
    entity = catalog.require_entity(user_id, entity_id)
    media_type = str(entity[3])
    if media_type not in {"episode", "movie"}:
        raise HTTPException(
            400, "Subtitle downloader is available for movies and episodes only."
        )
    db = catalog.db
    rows = db.execute(
        "SELECT ms.id,mf.id,e.library_id,l.directory,mf.relative_path,mf.size,mf.modified_ns,"
        "mf.quick_fingerprint,e.season_number,e.episode_number,series.id,series.relative_path,e.entity_type "
        "FROM media_sources ms JOIN media_files mf ON mf.id=ms.media_file_id "
        "JOIN library_entities e ON e.id=ms.entity_id JOIN libraries l ON l.id=e.library_id "
        "LEFT JOIN library_entities season ON season.id=e.parent_id AND e.entity_type='episode' "
        "LEFT JOIN library_entities series ON series.id=season.parent_id "
        "WHERE ms.id=? AND ms.entity_id=? AND mf.role='media'",
        (source_id, entity_id),
    )
    if not rows:
        raise HTTPException(404, "Playback source not found.")
    row = rows[0]
    mapping_rows = db.execute(
        "SELECT bazarr_root_path FROM bazarr_library_mappings WHERE library_id=?",
        (row[2],),
    )
    bazarr_root = _effective_bazarr_root(
        mapping_rows[0][0] if mapping_rows else None,
        row[3],
    )
    target_path = mapped_path(bazarr_root, row[4]) if bazarr_root else None
    file_rows = db.execute(
        "SELECT id,relative_path,role,language FROM media_files WHERE entity_id=? AND role IN ('media','subtitle','lyrics') ORDER BY relative_path COLLATE NOCASE",
        (entity_id,),
    )
    media_paths = [row[1] for row in file_rows if row[2] == "media"]
    sidecar_rows = [row for row in file_rows if row[2] in {"subtitle", "lyrics"}]
    media_path = Path(row[4])
    local_subtitles = tuple(
        {
            "id": sidecar[0],
            "relativePath": sidecar[1],
            "role": sidecar[2],
            "language": sidecar[3],
        }
        for sidecar in sidecar_rows
        if _associated_sidecar(media_path, Path(sidecar[1]), media_paths)
    )
    if media_type == "episode":
        # The episode's series is stable through the hierarchy, while provider
        # identities are only used below to reject a conflicting Bazarr record.
        series_rows = db.execute(
            "SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=(SELECT parent_id FROM library_entities WHERE id=(SELECT parent_id FROM library_entities WHERE id=?)) AND identifier_type='series' ORDER BY provider",
            (entity_id,),
        )
        movie_rows = []
    else:
        series_rows = []
        movie_rows = db.execute(
            "SELECT provider,provider_id FROM entity_provider_ids "
            "WHERE entity_id=? AND provider IN ('tmdb','imdb') "
            "AND identifier_type IN ('movie','imdb') ORDER BY provider",
            (entity_id,),
        )
    return BazarrTarget(
        user_id=user_id,
        entity_id=entity_id,
        source_id=str(row[0]),
        media_file_id=str(row[1]),
        library_id=str(row[2]),
        series_entity_id=str(row[10] or ""),
        library_directory=str(row[3] or ""),
        relative_path=str(row[4]),
        series_relative_path=str(row[11] or ""),
        season_number=_integer(row[8]),
        episode_number=_integer(row[9]),
        size=_integer(row[5]),
        modified_ns=_integer(row[6]),
        quick_fingerprint=str(row[7]) if row[7] is not None else None,
        bazarr_root_path=str(bazarr_root) if bazarr_root else None,
        target_path=target_path,
        local_subtitles=local_subtitles,
        series_provider_ids=tuple(
            (str(value[0]), str(value[1])) for value in series_rows
        ),
        media_type=media_type,
        movie_provider_ids=tuple(
            (str(value[0]), str(value[1])) for value in movie_rows
        ),
    )


def _associated_sidecar(
    media_path: Path, sidecar_path: Path, media_paths: list[str] | None = None
) -> bool:
    candidates = media_paths if media_paths is not None else [str(media_path)]
    matching_media = sidecar_media_path(sidecar_path, candidates)
    return matching_media == media_path


def _provider_id(item: dict, *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _release_name(item: dict) -> str | None:
    release_info = item.get("release_info")
    if isinstance(release_info, (list, tuple)):
        values = [
            str(value).strip()
            for value in release_info
            if value is not None and str(value).strip()
        ]
        if values:
            return ", ".join(values)
    elif release_info is not None and str(release_info).strip():
        return str(release_info).strip()
    return _provider_id(item, "releaseName", "release_name", "release")


def _provider_conflict(target: BazarrTarget, series: dict) -> bool:
    target_tvdb = dict(target.series_provider_ids).get("tvdb")
    bazarr_tvdb = _provider_id(series, "tvdbId", "tvdbid", "tvdb_id")
    return bool(target_tvdb and bazarr_tvdb and target_tvdb != bazarr_tvdb)


def _movie_provider_conflict(
    movie_provider_ids: tuple[tuple[str, str], ...], movie: dict
) -> bool:
    target_ids = dict(movie_provider_ids)
    for provider, keys in (
        ("tmdb", ("tmdbId", "tmdbid", "tmdb_id")),
        ("imdb", ("imdbId", "imdbid", "imdb_id")),
    ):
        target_id = target_ids.get(provider)
        bazarr_id = _provider_id(movie, *keys)
        if target_id and bazarr_id and target_id != bazarr_id:
            return True
    return False


def _resolve_series_values(
    bazarr_root_path: str | None,
    series_relative_path: str,
    series_provider_ids: tuple[tuple[str, str], ...],
    series_values: list[dict],
) -> dict:
    if not bazarr_root_path or not series_relative_path:
        raise BazarrMatchError(
            "not_configured",
            "This TV library is not mapped to the subtitle downloader.",
        )
    expected_series_path = mapped_path(bazarr_root_path, series_relative_path)
    series_candidates = [
        series
        for series in series_values
        if _path_key(series.get("path")) == _path_key(expected_series_path)
    ]
    if not series_candidates:
        raise BazarrMatchError(
            "unmatched",
            "The subtitle downloader has no series entry for this library path.",
        )
    if len(series_candidates) > 1:
        raise BazarrMatchError(
            "ambiguous",
            "The subtitle downloader has multiple series entries for this library path.",
        )
    series = series_candidates[0]
    target_tvdb = dict(series_provider_ids).get("tvdb")
    bazarr_tvdb = _provider_id(series, "tvdbId", "tvdbid", "tvdb_id")
    if target_tvdb and bazarr_tvdb and target_tvdb != bazarr_tvdb:
        raise BazarrMatchError(
            "identity_conflict",
            "The subtitle downloader series identity conflicts with this catalog series.",
        )
    series_id = _integer(
        _provider_id(series, "sonarrSeriesId", "sonarr_series_id", "id")
    )
    if series_id is None:
        raise BazarrMatchError(
            "unmatched", "The subtitle downloader did not return an internal series ID."
        )
    return series


def _resolve_episode_values(
    target_path: str | None,
    season_number: int | None,
    episode_number: int | None,
    series: dict,
    episode_values: list[dict],
) -> dict:
    if not target_path:
        raise BazarrMatchError(
            "not_configured",
            "This TV library has no path available to the subtitle downloader.",
        )
    series_id = _integer(
        _provider_id(series, "sonarrSeriesId", "sonarr_series_id", "id")
    )
    if series_id is None:
        raise BazarrMatchError(
            "unmatched", "The subtitle downloader did not return an internal series ID."
        )
    expected_episode_path = _path_key(target_path)
    episode_candidates = [
        episode
        for episode in episode_values
        if _path_key(episode.get("path")) == expected_episode_path
    ]
    if not episode_candidates:
        raise BazarrMatchError(
            "unmatched",
            "The subtitle downloader has no episode entry for this exact file path.",
        )
    if len(episode_candidates) > 1:
        raise BazarrMatchError(
            "ambiguous",
            "The subtitle downloader has multiple episode entries for this exact file path.",
        )
    episode = episode_candidates[0]
    episode_series_id = _integer(
        _provider_id(
            episode,
            "seriesId",
            "series_id",
            "sonarrSeriesId",
            "sonarr_series_id",
        )
    )
    if episode_series_id is not None and episode_series_id != series_id:
        raise BazarrMatchError(
            "identity_conflict",
            "The subtitle downloader episode belongs to a different series.",
        )
    bazarr_season = _integer(episode.get("season"))
    bazarr_episode = _integer(episode.get("episode"))
    if (
        season_number is not None
        and bazarr_season is not None
        and season_number != bazarr_season
    ) or (
        episode_number is not None
        and bazarr_episode is not None
        and episode_number != bazarr_episode
    ):
        raise BazarrMatchError(
            "identity_conflict",
            "The subtitle downloader episode numbering conflicts with this catalog episode.",
        )
    episode_id = _integer(
        _provider_id(episode, "sonarrEpisodeId", "sonarr_episode_id", "id")
    )
    if episode_id is None:
        raise BazarrMatchError(
            "unmatched",
            "The subtitle downloader did not return an internal episode ID.",
        )
    return {
        "series": series,
        "episode": episode,
        "seriesId": series_id,
        "episodeId": episode_id,
    }


def _resolve_movie_values(
    bazarr_root_path: str | None,
    target_path: str | None,
    movie_provider_ids: tuple[tuple[str, str], ...],
    movie_values: list[dict],
) -> dict:
    if not bazarr_root_path or not target_path:
        raise BazarrMatchError(
            "not_configured",
            "This movie library has no path available to the subtitle downloader.",
        )
    expected_movie_path = _path_key(target_path)
    movie_candidates = [
        movie
        for movie in movie_values
        if _path_key(movie.get("path")) == expected_movie_path
    ]
    if not movie_candidates:
        raise BazarrMatchError(
            "unmatched",
            "The subtitle downloader has no movie entry for this exact file path.",
        )
    if len(movie_candidates) > 1:
        raise BazarrMatchError(
            "ambiguous",
            "The subtitle downloader has multiple movie entries for this exact file path.",
        )
    movie = movie_candidates[0]
    if _movie_provider_conflict(movie_provider_ids, movie):
        raise BazarrMatchError(
            "identity_conflict",
            "The subtitle downloader movie identity conflicts with this catalog movie.",
        )
    movie_id = _integer(
        _provider_id(movie, "radarrId", "radarr_id", "movieId", "movie_id", "id")
    )
    if movie_id is None:
        raise BazarrMatchError(
            "unmatched", "The subtitle downloader did not return an internal movie ID."
        )
    return {"movie": movie, "movieId": movie_id}


class BazarrMappingStore:
    def __init__(self, db=None):
        self.db = db or Config().database

    def resolve(self, target: BazarrTarget) -> dict:
        if target.media_type == "movie":
            return self._resolve_movie(target)
        rows = self.db.execute(
            "SELECT entity_id,series_entity_id,target_path,size,modified_ns,"
            "quick_fingerprint,bazarr_series_id,bazarr_episode_id,state,title,"
            "season_number,episode_number,subtitles_json,message "
            "FROM bazarr_episode_mappings WHERE media_file_id=?",
            (target.media_file_id,),
        )
        if not rows:
            raise BazarrMatchError("sync_pending", BAZARR_SYNC_PENDING_MESSAGE)
        row = rows[0]
        if (
            str(row[0]) != target.entity_id
            or str(row[1]) != target.series_entity_id
            or _path_key(row[2]) != _path_key(target.target_path)
            or _integer(row[3]) != target.size
            or _integer(row[4]) != target.modified_ns
            or (str(row[5]) if row[5] is not None else None) != target.quick_fingerprint
        ):
            raise BazarrMatchError("sync_pending", BAZARR_SYNC_PENDING_MESSAGE)
        state = str(row[8] or "sync_pending")
        if state != "matched":
            raise BazarrMatchError(
                state,
                str(row[13] or BAZARR_SYNC_PENDING_MESSAGE),
            )
        series_id = _integer(row[6])
        episode_id = _integer(row[7])
        if series_id is None or episode_id is None:
            raise BazarrMatchError("sync_pending", BAZARR_SYNC_PENDING_MESSAGE)
        try:
            subtitles = json.loads(row[12] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            subtitles = []
        if not isinstance(subtitles, list):
            subtitles = []
        return {
            "series": {
                "path": (
                    mapped_path(target.bazarr_root_path, target.series_relative_path)
                    if target.bazarr_root_path
                    else None
                ),
                "sonarrSeriesId": series_id,
            },
            "episode": {
                "path": target.target_path,
                "sonarrEpisodeId": episode_id,
                "title": row[9],
                "season": _integer(row[10]),
                "episode": _integer(row[11]),
                "subtitles": [item for item in subtitles if isinstance(item, dict)],
            },
            "seriesId": series_id,
            "episodeId": episode_id,
        }

    def _resolve_movie(self, target: BazarrTarget) -> dict:
        rows = self.db.execute(
            "SELECT entity_id,target_path,size,modified_ns,quick_fingerprint,bazarr_movie_id,"
            "state,title,subtitles_json,message FROM bazarr_movie_mappings "
            "WHERE media_file_id=?",
            (target.media_file_id,),
        )
        if not rows:
            raise BazarrMatchError("sync_pending", BAZARR_SYNC_PENDING_MESSAGE)
        row = rows[0]
        if (
            str(row[0]) != target.entity_id
            or _path_key(row[1]) != _path_key(target.target_path)
            or _integer(row[2]) != target.size
            or _integer(row[3]) != target.modified_ns
            or (str(row[4]) if row[4] is not None else None) != target.quick_fingerprint
        ):
            raise BazarrMatchError("sync_pending", BAZARR_SYNC_PENDING_MESSAGE)
        state = str(row[6] or "sync_pending")
        if state != "matched":
            raise BazarrMatchError(
                state,
                str(row[9] or BAZARR_SYNC_PENDING_MESSAGE),
            )
        movie_id = _integer(row[5])
        if movie_id is None:
            raise BazarrMatchError("sync_pending", BAZARR_SYNC_PENDING_MESSAGE)
        try:
            subtitles = json.loads(row[8] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            subtitles = []
        if not isinstance(subtitles, list):
            subtitles = []
        return {
            "movie": {
                "path": target.target_path,
                "radarrId": movie_id,
                "movieId": movie_id,
                "title": row[7],
                "subtitles": [item for item in subtitles if isinstance(item, dict)],
            },
            "movieId": movie_id,
        }


def _find_mapped_episode(target: BazarrTarget) -> dict:
    if not target.target_path or not target.bazarr_root_path:
        raise BazarrMatchError(
            "not_configured",
            "This TV library is not mapped to the subtitle downloader.",
        )
    return BazarrMappingStore().resolve(target)


def _find_mapped_movie(target: BazarrTarget) -> dict:
    if not target.target_path or not target.bazarr_root_path:
        raise BazarrMatchError(
            "not_configured",
            "This movie library is not mapped to the subtitle downloader.",
        )
    return BazarrMappingStore().resolve(target)


def _find_mapped_target(target: BazarrTarget) -> dict:
    if target.media_type == "movie":
        return _find_mapped_movie(target)
    return _find_mapped_episode(target)


def _resolution_cache_key(client: BazarrClient, target: BazarrTarget) -> tuple | None:
    client_key = getattr(client, "cache_key", None)
    if client_key is None:
        return None
    return (
        client_key,
        target.media_type,
        target.media_file_id,
        target.target_path,
        target.size,
        target.modified_ns,
        target.quick_fingerprint,
    )


def _find_episode(client: BazarrClient, target: BazarrTarget) -> dict:
    cache_key = _resolution_cache_key(client, target)
    now = time.monotonic()
    if cache_key is not None:
        with _bazarr_cache_lock:
            cached = _bazarr_resolution_cache.get(cache_key)
        if cached and cached[0] > now:
            _, resolution, error = cached
            if error:
                raise BazarrMatchError(*error)
            if resolution is not None:
                return resolution

    try:
        resolution = _find_episode_uncached(client, target)
    except BazarrMatchError as error:
        if cache_key is not None:
            with _bazarr_cache_lock:
                _bazarr_resolution_cache[cache_key] = (
                    now + BAZARR_NEGATIVE_LOOKUP_CACHE_SECONDS,
                    None,
                    (error.code, str(error)),
                )
        raise
    if cache_key is not None:
        with _bazarr_cache_lock:
            _bazarr_resolution_cache[cache_key] = (
                now + BAZARR_LOOKUP_CACHE_SECONDS,
                resolution,
                None,
            )
    return resolution


def _find_episode_uncached(client: BazarrClient, target: BazarrTarget) -> dict:
    series = _resolve_series_values(
        target.bazarr_root_path,
        target.series_relative_path,
        target.series_provider_ids,
        client.series(),
    )
    series_id = _integer(
        _provider_id(series, "sonarrSeriesId", "sonarr_series_id", "id")
    )
    if series_id is None:
        raise BazarrMatchError(
            "unmatched", "The subtitle downloader did not return an internal series ID."
        )
    return _resolve_episode_values(
        target.target_path,
        target.season_number,
        target.episode_number,
        series,
        client.episodes(series_id),
    )


def _find_movie(client: BazarrClient, target: BazarrTarget) -> dict:
    cache_key = _resolution_cache_key(client, target)
    now = time.monotonic()
    if cache_key is not None:
        with _bazarr_cache_lock:
            cached = _bazarr_resolution_cache.get(cache_key)
        if cached and cached[0] > now:
            _, resolution, error = cached
            if error:
                raise BazarrMatchError(*error)
            if resolution is not None:
                return resolution

    try:
        resolution = _resolve_movie_values(
            target.bazarr_root_path,
            target.target_path,
            target.movie_provider_ids,
            client.movies(),
        )
    except BazarrMatchError as error:
        if cache_key is not None:
            with _bazarr_cache_lock:
                _bazarr_resolution_cache[cache_key] = (
                    now + BAZARR_NEGATIVE_LOOKUP_CACHE_SECONDS,
                    None,
                    (error.code, str(error)),
                )
        raise
    if cache_key is not None:
        with _bazarr_cache_lock:
            _bazarr_resolution_cache[cache_key] = (
                now + BAZARR_LOOKUP_CACHE_SECONDS,
                resolution,
                None,
            )
    return resolution


def _subtitle_summary(value: dict) -> dict:
    return {
        "language": _provider_id(value, "language", "lang", "code"),
        "name": _provider_id(value, "name", "language_name", "label", "provider"),
        "provider": _provider_id(value, "provider", "provider_name"),
        "hearingImpaired": _bool_value(
            value.get(
                "hearing_impaired",
                value.get("hearingImpaired", value.get("hi")),
            )
        ),
        "forced": _bool_value(value.get("forced")),
        "format": _provider_id(value, "format", "extension", "codec"),
    }


def _candidate(value: dict) -> dict | None:
    provider = _provider_id(value, "provider", "provider_name")
    subtitle = _provider_id(value, "subtitle", "subtitleId", "subtitle_id", "id")
    if not provider or not subtitle:
        return None
    name = _provider_id(value, "name", "filename", "label", "provider") or provider
    release_name = _release_name(value)
    hi = _bool_value(value.get("hearing_impaired", value.get("hi")))
    forced = _bool_value(value.get("forced"))
    original_format = _bool_value(
        value.get("original_format", value.get("originalFormat"))
    )
    candidate_id = hashlib.sha256(
        json.dumps(
            [provider, subtitle, hi, forced, original_format],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "candidateId": candidate_id,
        "provider": provider,
        "language": _provider_id(value, "language", "lang", "code"),
        "name": name,
        "releaseName": release_name,
        "hearingImpaired": hi,
        "forced": forced,
        "originalFormat": original_format,
        "format": _provider_id(value, "format", "extension", "codec"),
        "score": value.get("score"),
        "uploader": _provider_id(value, "uploader", "uploader_name"),
        "subtitle": subtitle,
    }


def _target_status(
    target: BazarrTarget,
    resolution: dict | None,
    state: str,
    message: str | None = None,
) -> dict:
    value = {
        "state": state,
        "sourceId": target.source_id,
        "relativePath": target.relative_path,
        "hasLocalSubtitle": bool(target.local_subtitles),
        "localSubtitles": list(target.local_subtitles),
    }
    if message:
        value["message"] = message
    if resolution:
        if target.media_type == "movie":
            movie = resolution["movie"]
            value["movie"] = {
                "movieId": resolution["movieId"],
                "title": movie.get("title") or movie.get("sceneName"),
                "subtitles": [
                    _subtitle_summary(item)
                    for item in (movie.get("subtitles") or [])
                    if isinstance(item, dict)
                ],
            }
        else:
            episode = resolution["episode"]
            value["episode"] = {
                "seriesId": resolution["seriesId"],
                "episodeId": resolution["episodeId"],
                "title": episode.get("title") or episode.get("sceneName"),
                "season": _integer(episode.get("season")),
                "episode": _integer(episode.get("episode")),
                "subtitles": [
                    _subtitle_summary(item)
                    for item in (episode.get("subtitles") or [])
                    if isinstance(item, dict)
                ],
            }
    return value


def _search_candidates(
    client: BazarrClient, media_id: int, media_type: str = "episode"
) -> list[dict]:
    """Retry one empty provider response while Bazarr finishes its lookup."""
    values: list[dict] = []
    for attempt in range(2):
        raw_matches = (
            client.search_movie(media_id)
            if media_type == "movie"
            else client.search(media_id)
        )
        values = [
            value for raw in raw_matches if (value := _candidate(raw)) is not None
        ]
        if values or attempt == 1:
            break
        time.sleep(BAZARR_EMPTY_SEARCH_RETRY_DELAY_SECONDS)
    return values


class BazarrSyncService:
    def __init__(self, db=None):
        self.db = db or Config().database

    @staticmethod
    def _series_target_path(row: dict) -> str | None:
        root = row.get("bazarr_root_path")
        relative_path = str(row.get("series_relative_path") or "").strip()
        if not root or not relative_path:
            return None
        try:
            return mapped_path(str(root), relative_path)
        except ValueError:
            return None

    def _inventory(self) -> dict[str, list[dict]]:
        rows = self.db.execute(
            "SELECT mf.id,e.id,e.library_id,l.directory,mf.relative_path,mf.size,"
            "mf.modified_ns,mf.quick_fingerprint,e.season_number,e.episode_number,"
            "series.id,series.relative_path,bm.bazarr_root_path "
            "FROM media_files mf "
            "JOIN library_entities e ON e.id=mf.entity_id "
            "JOIN libraries l ON l.id=e.library_id AND l.type='tv_series' "
            "JOIN library_entities season ON season.id=e.parent_id "
            "JOIN library_entities series ON series.id=season.parent_id "
            "LEFT JOIN bazarr_library_mappings bm ON bm.library_id=e.library_id "
            "WHERE mf.role='media' AND e.entity_type='episode' "
            "ORDER BY e.library_id,series.id,mf.relative_path COLLATE NOCASE"
        )
        groups: dict[str, list[dict]] = {}
        for row in rows:
            value = {
                "media_file_id": str(row[0]),
                "entity_id": str(row[1]),
                "library_id": str(row[2]),
                "library_directory": str(row[3] or ""),
                "relative_path": str(row[4]),
                "size": _integer(row[5]),
                "modified_ns": _integer(row[6]),
                "quick_fingerprint": (str(row[7]) if row[7] is not None else None),
                "season_number": _integer(row[8]),
                "episode_number": _integer(row[9]),
                "series_entity_id": str(row[10]),
                "series_relative_path": str(row[11] or ""),
                "bazarr_root_path": _effective_bazarr_root(row[12], row[3]),
            }
            root = value["bazarr_root_path"]
            value["target_path"] = (
                mapped_path(root, value["relative_path"]) if root else None
            )
            groups.setdefault(value["series_entity_id"], []).append(value)

        series_ids = list(groups)
        provider_ids: dict[str, list[tuple[str, str]]] = {
            series_id: [] for series_id in series_ids
        }
        if series_ids:
            placeholders = ",".join("?" for _ in series_ids)
            provider_rows = self.db.execute(
                "SELECT entity_id,provider,provider_id FROM entity_provider_ids "
                "WHERE identifier_type='series' AND entity_id IN ("
                + placeholders
                + ") ORDER BY entity_id,provider",
                series_ids,
            )
            for entity_id, provider, provider_id in provider_rows:
                provider_ids.setdefault(str(entity_id), []).append(
                    (str(provider), str(provider_id))
                )
        for series_id, values in groups.items():
            identities = tuple(provider_ids.get(series_id, []))
            for value in values:
                value["series_provider_ids"] = identities
        return groups

    def _movie_inventory(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT mf.id,e.id,e.library_id,l.directory,mf.relative_path,mf.size,"
            "mf.modified_ns,mf.quick_fingerprint,bm.bazarr_root_path "
            "FROM media_files mf "
            "JOIN library_entities e ON e.id=mf.entity_id "
            "JOIN libraries l ON l.id=e.library_id AND l.type='movies' "
            "LEFT JOIN bazarr_library_mappings bm ON bm.library_id=e.library_id "
            "WHERE mf.role='media' AND e.entity_type='movie' "
            "ORDER BY e.library_id,e.id,mf.relative_path COLLATE NOCASE"
        )
        values: list[dict] = []
        entity_ids: list[str] = []
        for row in rows:
            value = {
                "media_file_id": str(row[0]),
                "entity_id": str(row[1]),
                "library_id": str(row[2]),
                "library_directory": str(row[3] or ""),
                "relative_path": str(row[4]),
                "size": _integer(row[5]),
                "modified_ns": _integer(row[6]),
                "quick_fingerprint": (str(row[7]) if row[7] is not None else None),
                "bazarr_root_path": _effective_bazarr_root(row[8], row[3]),
            }
            root = value["bazarr_root_path"]
            value["target_path"] = (
                mapped_path(root, value["relative_path"]) if root else None
            )
            values.append(value)
            entity_ids.append(value["entity_id"])

        provider_ids: dict[str, list[tuple[str, str]]] = {
            entity_id: [] for entity_id in entity_ids
        }
        if entity_ids:
            placeholders = ",".join("?" for _ in entity_ids)
            provider_rows = self.db.execute(
                "SELECT entity_id,provider,provider_id FROM entity_provider_ids "
                "WHERE entity_id IN ("
                + placeholders
                + ") AND provider IN ('tmdb','imdb') "
                "AND identifier_type IN ('movie','imdb') ORDER BY entity_id,provider",
                entity_ids,
            )
            for entity_id, provider, provider_id in provider_rows:
                provider_ids.setdefault(str(entity_id), []).append(
                    (str(provider), str(provider_id))
                )
        for value in values:
            value["movie_provider_ids"] = tuple(
                provider_ids.get(value["entity_id"], [])
            )
        return values

    def _prune_deleted_inventory(self) -> None:
        with self.db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM bazarr_episode_mappings WHERE media_file_id NOT IN "
                "(SELECT id FROM media_files WHERE role='media')"
            )
            cursor.execute(
                "DELETE FROM bazarr_series_mappings WHERE series_entity_id NOT IN "
                "(SELECT id FROM library_entities WHERE entity_type='series')"
            )
            cursor.execute(
                "DELETE FROM bazarr_movie_mappings WHERE media_file_id NOT IN "
                "(SELECT id FROM media_files WHERE role='media')"
            )

    def _write_group(
        self,
        values: list[dict],
        *,
        series_state: str,
        series_message: str,
        series: dict | None,
        episodes: list[dict] | None,
        synced_at: str,
    ) -> tuple[int, int]:
        first = values[0]
        series_id = (
            _integer(_provider_id(series, "sonarrSeriesId", "sonarr_series_id", "id"))
            if series
            else None
        )
        series_target_path = self._series_target_path(first)
        episode_results: list[tuple[dict, str, str, dict | None]] = []
        matched = 0
        for value in values:
            if episodes is None or series is None:
                episode_results.append((value, series_state, series_message, None))
                continue
            try:
                resolution = _resolve_episode_values(
                    value["target_path"],
                    value["season_number"],
                    value["episode_number"],
                    series,
                    episodes,
                )
            except BazarrMatchError as error:
                episode_results.append((value, error.code, str(error), None))
            else:
                matched += 1
                episode_results.append((value, "matched", "", resolution))

        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO bazarr_series_mappings("
                "series_entity_id,library_id,target_path,bazarr_series_id,state,"
                "message,updated_at,synced_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(series_entity_id) DO UPDATE SET "
                "library_id=excluded.library_id,target_path=excluded.target_path,"
                "bazarr_series_id=excluded.bazarr_series_id,state=excluded.state,"
                "message=excluded.message,updated_at=excluded.updated_at,"
                "synced_at=excluded.synced_at",
                (
                    first["series_entity_id"],
                    first["library_id"],
                    series_target_path,
                    series_id,
                    series_state,
                    series_message or None,
                    synced_at,
                    synced_at,
                ),
            )
            for value, state, message, resolution in episode_results:
                episode = resolution["episode"] if resolution else None
                episode_id = resolution["episodeId"] if resolution else None
                episode_series_id = resolution["seriesId"] if resolution else series_id
                subtitles = [
                    _subtitle_summary(item)
                    for item in (episode.get("subtitles") or [] if episode else [])
                    if isinstance(item, dict)
                ]
                cursor.execute(
                    "INSERT INTO bazarr_episode_mappings("
                    "media_file_id,entity_id,series_entity_id,target_path,size,"
                    "modified_ns,quick_fingerprint,bazarr_series_id,"
                    "bazarr_episode_id,state,title,season_number,episode_number,"
                    "subtitles_json,message,updated_at,synced_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(media_file_id) DO UPDATE SET "
                    "entity_id=excluded.entity_id,series_entity_id=excluded.series_entity_id,"
                    "target_path=excluded.target_path,size=excluded.size,"
                    "modified_ns=excluded.modified_ns,quick_fingerprint=excluded.quick_fingerprint,"
                    "bazarr_series_id=excluded.bazarr_series_id,"
                    "bazarr_episode_id=excluded.bazarr_episode_id,state=excluded.state,"
                    "title=excluded.title,season_number=excluded.season_number,"
                    "episode_number=excluded.episode_number,subtitles_json=excluded.subtitles_json,"
                    "message=excluded.message,updated_at=excluded.updated_at,"
                    "synced_at=excluded.synced_at",
                    (
                        value["media_file_id"],
                        value["entity_id"],
                        value["series_entity_id"],
                        value["target_path"],
                        value["size"],
                        value["modified_ns"],
                        value["quick_fingerprint"],
                        episode_series_id,
                        episode_id,
                        state,
                        (
                            _provider_id(episode, "title", "sceneName")
                            if episode
                            else None
                        ),
                        _integer(episode.get("season")) if episode else None,
                        _integer(episode.get("episode")) if episode else None,
                        json.dumps(
                            subtitles, ensure_ascii=False, separators=(",", ":")
                        ),
                        message or None,
                        synced_at,
                        synced_at,
                    ),
                )
        return matched, len(episode_results)

    def _write_movie(
        self,
        value: dict,
        *,
        state: str,
        message: str,
        resolution: dict | None,
        synced_at: str,
    ) -> None:
        movie = resolution["movie"] if resolution else None
        movie_id = resolution["movieId"] if resolution else None
        subtitles = [
            _subtitle_summary(item)
            for item in (movie.get("subtitles") or [] if movie else [])
            if isinstance(item, dict)
        ]
        title = _provider_id(movie, "title", "sceneName", "name") if movie else None
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO bazarr_movie_mappings("
                "media_file_id,entity_id,library_id,target_path,size,modified_ns,"
                "quick_fingerprint,bazarr_movie_id,state,title,subtitles_json,message,"
                "updated_at,synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(media_file_id) DO UPDATE SET "
                "entity_id=excluded.entity_id,library_id=excluded.library_id,"
                "target_path=excluded.target_path,size=excluded.size,"
                "modified_ns=excluded.modified_ns,quick_fingerprint=excluded.quick_fingerprint,"
                "bazarr_movie_id=excluded.bazarr_movie_id,state=excluded.state,"
                "title=excluded.title,subtitles_json=excluded.subtitles_json,"
                "message=excluded.message,updated_at=excluded.updated_at,"
                "synced_at=excluded.synced_at",
                (
                    value["media_file_id"],
                    value["entity_id"],
                    value["library_id"],
                    value["target_path"],
                    value["size"],
                    value["modified_ns"],
                    value["quick_fingerprint"],
                    movie_id,
                    state,
                    title,
                    json.dumps(subtitles, ensure_ascii=False, separators=(",", ":")),
                    message or None,
                    synced_at,
                    synced_at,
                ),
            )

    def sync(self, should_terminate=None, progress=None) -> dict:
        should_terminate = should_terminate or (lambda: False)
        connection = BazarrConnectionStore().internal()
        if not connection:
            return {
                "skipped": True,
                "series": 0,
                "matched_series": 0,
                "episodes": 0,
                "matched": 0,
                "deferred_series": 0,
                "movies": 0,
                "matched_movies": 0,
                "deferred_movies": 0,
            }

        groups = self._inventory()
        movie_inventory = self._movie_inventory()
        self._prune_deleted_inventory()
        if not groups and not movie_inventory:
            return {
                "skipped": False,
                "series": 0,
                "matched_series": 0,
                "episodes": 0,
                "matched": 0,
                "deferred_series": 0,
                "movies": 0,
                "matched_movies": 0,
                "deferred_movies": 0,
            }
        total = len(groups) + len(movie_inventory)
        if progress:
            progress(0, total)
        client = BazarrClient(connection)
        matched_series = 0
        matched_episodes = 0
        episode_total = 0
        deferred_series = 0
        matched_movies = 0
        deferred_movies = 0
        processed = 0
        try:
            try:
                series_values = client.series() if groups else []
            except BazarrError:
                series_values = None
                deferred_series = len(groups)
                logger.warning(
                    "could not refresh Bazarr series inventory", exc_info=True
                )
            try:
                movie_values = client.movies() if movie_inventory else []
            except BazarrError:
                movie_values = None
                deferred_movies = len(movie_inventory)
                logger.warning(
                    "could not refresh Bazarr movie inventory", exc_info=True
                )

            for values in groups.values():
                if should_terminate():
                    from app.library import JobTerminated

                    raise JobTerminated()
                episode_total += len(values)
                first = values[0]
                if series_values is not None:
                    try:
                        series = _resolve_series_values(
                            first["bazarr_root_path"],
                            first["series_relative_path"],
                            first["series_provider_ids"],
                            series_values,
                        )
                    except BazarrMatchError as error:
                        matched, _ = self._write_group(
                            values,
                            series_state=error.code,
                            series_message=str(error),
                            series=None,
                            episodes=None,
                            synced_at=_timestamp(),
                        )
                        matched_episodes += matched
                    else:
                        matched_series += 1
                        series_id = _integer(
                            _provider_id(
                                series, "sonarrSeriesId", "sonarr_series_id", "id"
                            )
                        )
                        try:
                            episodes = client.episodes(series_id)
                        except BazarrError:
                            deferred_series += 1
                            logger.warning(
                                "could not refresh Bazarr episode mapping series_entity_id=%s",
                                first["series_entity_id"],
                                exc_info=True,
                            )
                        else:
                            matched, _ = self._write_group(
                                values,
                                series_state="matched",
                                series_message="",
                                series=series,
                                episodes=episodes,
                                synced_at=_timestamp(),
                            )
                            matched_episodes += matched
                processed += 1
                if progress:
                    progress(processed, total)

            if movie_values is not None:
                for value in movie_inventory:
                    if should_terminate():
                        from app.library import JobTerminated

                        raise JobTerminated()
                    try:
                        resolution = _resolve_movie_values(
                            value["bazarr_root_path"],
                            value["target_path"],
                            value["movie_provider_ids"],
                            movie_values,
                        )
                    except BazarrMatchError as error:
                        self._write_movie(
                            value,
                            state=error.code,
                            message=str(error),
                            resolution=None,
                            synced_at=_timestamp(),
                        )
                    else:
                        self._write_movie(
                            value,
                            state="matched",
                            message="",
                            resolution=resolution,
                            synced_at=_timestamp(),
                        )
                        matched_movies += 1
                    processed += 1
                    if progress:
                        progress(processed, total)
            else:
                processed += len(movie_inventory)
                if progress and movie_inventory:
                    progress(processed, total)
        finally:
            client.close()
        return {
            "skipped": False,
            "series": len(groups),
            "matched_series": matched_series,
            "episodes": episode_total,
            "matched": matched_episodes,
            "deferred_series": deferred_series,
            "movies": len(movie_inventory),
            "matched_movies": matched_movies,
            "deferred_movies": deferred_movies,
        }


class BazarrSubtitleService:
    def __init__(self):
        self.store = BazarrConnectionStore()

    def _client(self, target: BazarrTarget) -> BazarrClient:
        connection = self.store.internal()
        if not connection:
            raise BazarrMatchError(
                "not_configured", "The subtitle downloader is not configured."
            )
        if not target.bazarr_root_path:
            raise BazarrMatchError(
                "not_configured",
                "This library has no path available to the subtitle downloader.",
            )
        return BazarrClient(connection)

    def status(self, user_id: str, entity_id: str, source_id: str | None) -> dict:
        target = _target(user_id, entity_id, source_id)
        try:
            if not self.store.internal():
                raise BazarrMatchError(
                    "not_configured", "The subtitle downloader is not configured."
                )
            resolution = _find_mapped_target(target)
        except BazarrMatchError as error:
            return _target_status(target, None, error.code, str(error))
        return _target_status(target, resolution, "matched")

    def search(self, user_id: str, entity_id: str, source_id: str | None) -> dict:
        target = _target(user_id, entity_id, source_id)
        resolution = _find_mapped_target(target)
        client = self._client(target)
        try:
            media_id_key = "movieId" if target.media_type == "movie" else "episodeId"
            values = _search_candidates(
                client, resolution[media_id_key], target.media_type
            )

            matches = []
            for value in values:
                ticket_values = {
                    "entity": entity_id,
                    "sourceId": target.source_id,
                    "mediaFileId": target.media_file_id,
                    "size": target.size,
                    "modifiedNs": target.modified_ns,
                    "fingerprint": target.quick_fingerprint,
                    "mediaType": target.media_type,
                    "candidateId": value["candidateId"],
                    "provider": value["provider"],
                    "subtitle": value["subtitle"],
                    "hi": value["hearingImpaired"],
                    "forced": value["forced"],
                    "originalFormat": value["originalFormat"],
                }
                if target.media_type == "movie":
                    ticket_values["movieId"] = resolution["movieId"]
                else:
                    ticket_values.update(
                        {
                            "seriesId": resolution["seriesId"],
                            "episodeId": resolution["episodeId"],
                        }
                    )
                match_id = issue_ticket(
                    user_id,
                    "resource",
                    BAZARR_MATCH_TTL_SECONDS,
                    **ticket_values,
                )
                matches.append(
                    {key: item for key, item in value.items() if key != "subtitle"}
                    | {"matchId": match_id}
                )
            return {
                "state": "matches" if matches else "no_matches",
                "sourceId": target.source_id,
                "relativePath": target.relative_path,
                "matches": matches,
            }
        finally:
            client.close()

    def download(
        self, user_id: str, entity_id: str, source_id: str | None, match_id: str
    ) -> dict:
        target = _target(user_id, entity_id, source_id)
        payload = read_ticket(
            match_id,
            "resource",
            {"uid": user_id, "entity": entity_id, "sourceId": target.source_id},
        )
        for key, actual in (
            ("mediaFileId", target.media_file_id),
            ("size", target.size),
            ("modifiedNs", target.modified_ns),
            ("fingerprint", target.quick_fingerprint),
        ):
            if payload.get(key) != actual:
                raise HTTPException(
                    409, "The media file changed; search for subtitles again."
                )
        resolution = _find_mapped_target(target)
        ticket_media_type = payload.get("mediaType") or "episode"
        if ticket_media_type != target.media_type:
            raise HTTPException(
                409, "The subtitle downloader target changed; search again."
            )
        if target.media_type == "movie":
            if payload.get("movieId") != resolution["movieId"]:
                raise HTTPException(
                    409,
                    "The subtitle downloader movie changed; search for subtitles again.",
                )
        elif (
            payload.get("seriesId") != resolution["seriesId"]
            or payload.get("episodeId") != resolution["episodeId"]
        ):
            raise HTTPException(
                409,
                "The subtitle downloader episode changed; search for subtitles again.",
            )
        client = self._client(target)
        try:
            if target.media_type == "movie":
                client.download_movie(
                    {
                        "movieId": resolution["movieId"],
                        "provider": payload.get("provider"),
                        "subtitle": payload.get("subtitle"),
                        "hi": bool(payload.get("hi")),
                        "forced": bool(payload.get("forced")),
                        "originalFormat": bool(payload.get("originalFormat")),
                    }
                )
            else:
                client.download(
                    {
                        "seriesId": resolution["seriesId"],
                        "episodeId": resolution["episodeId"],
                        "provider": payload.get("provider"),
                        "subtitle": payload.get("subtitle"),
                        "hi": bool(payload.get("hi")),
                        "forced": bool(payload.get("forced")),
                        "originalFormat": bool(payload.get("originalFormat")),
                    }
                )
        finally:
            client.close()
        from app.library import runtime

        runtime.request_reconcile(
            target.library_id,
            str(Path(target.library_directory) / Path(target.relative_path)),
        )
        return {
            "state": "download_started",
            "sourceId": target.source_id,
            "relativePath": target.relative_path,
            "reconcileQueued": True,
        }
