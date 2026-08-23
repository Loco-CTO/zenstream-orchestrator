from __future__ import annotations

import hashlib
import json
import os
import re
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
_ADDRESS_RE = re.compile(r"^[^\s/:]+$")


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
            "SELECT id,name FROM libraries WHERE type='tv_series' ORDER BY sort_order,name COLLATE NOCASE"
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
            if library_rows[0][0] != "tv_series":
                raise ValueError("Bazarr can only be mapped to TV libraries")
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

    @property
    def base_url(self) -> str:
        scheme = "https" if self.connection["useSsl"] else "http"
        base = str(self.connection.get("baseUrl") or "").rstrip("/")
        return f"{scheme}://{self.connection['address']}:{self.connection['port']}{base}/api"

    def request(self, method: str, path: str, params=None, json_body=None):
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Accept": "application/json",
                    "X-Api-Key": self.connection["apiKey"],
                    "User-Agent": "ZenStream/Bazarr",
                },
            ) as client:
                response = client.request(method, url, params=params, json=json_body)
            if response.status_code == 404:
                raise BazarrError("Bazarr endpoint was not found")
            response.raise_for_status()
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as error:
                raise BazarrError("Bazarr returned invalid JSON") from error
        except BazarrError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise BazarrError(
                f"Bazarr request failed: {type(error).__name__}"
            ) from error

    def series(self) -> list[dict]:
        return _data_list(
            self.request("GET", "/series", params={"start": 0, "length": -1})
        )

    def episodes(self, series_id: int) -> list[dict]:
        return _data_list(
            self.request("GET", "/episodes", params=[("seriesid[]", str(series_id))])
        )

    def search(self, episode_id: int) -> list[dict]:
        return _data_list(
            self.request("GET", "/providers/episodes", params={"episodeid": episode_id})
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


@dataclass(frozen=True)
class BazarrTarget:
    user_id: str
    entity_id: str
    source_id: str
    media_file_id: str
    library_id: str
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


def _target(user_id: str, entity_id: str, source_id: str | None) -> BazarrTarget:
    if not source_id:
        raise HTTPException(400, "A playback source is required.")
    catalog = Catalog()
    entity = catalog.require_entity(user_id, entity_id)
    if entity[3] != "episode":
        raise HTTPException(400, "Subtitle downloader is available for episodes only.")
    db = catalog.db
    rows = db.execute(
        "SELECT ms.id,mf.id,e.library_id,l.directory,mf.relative_path,mf.size,mf.modified_ns,"
        "mf.quick_fingerprint,e.season_number,e.episode_number,series.relative_path "
        "FROM media_sources ms JOIN media_files mf ON mf.id=ms.media_file_id "
        "JOIN library_entities e ON e.id=ms.entity_id JOIN libraries l ON l.id=e.library_id "
        "JOIN library_entities season ON season.id=e.parent_id "
        "JOIN library_entities series ON series.id=season.parent_id "
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
    # The episode's series is stable through the hierarchy, while provider
    # identities are only used below to reject a conflicting Bazarr record.
    series_rows = db.execute(
        "SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=(SELECT parent_id FROM library_entities WHERE id=(SELECT parent_id FROM library_entities WHERE id=?)) AND identifier_type='series' ORDER BY provider",
        (entity_id,),
    )
    return BazarrTarget(
        user_id=user_id,
        entity_id=entity_id,
        source_id=str(row[0]),
        media_file_id=str(row[1]),
        library_id=str(row[2]),
        library_directory=str(row[3]),
        relative_path=str(row[4]),
        series_relative_path=str(row[10]),
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


def _provider_conflict(target: BazarrTarget, series: dict) -> bool:
    target_tvdb = dict(target.series_provider_ids).get("tvdb")
    bazarr_tvdb = _provider_id(series, "tvdbId", "tvdbid", "tvdb_id")
    return bool(target_tvdb and bazarr_tvdb and target_tvdb != bazarr_tvdb)


def _find_episode(client: BazarrClient, target: BazarrTarget) -> dict:
    if not target.target_path or not target.bazarr_root_path:
        raise BazarrMatchError(
            "not_configured",
            "This TV library is not mapped to the subtitle downloader.",
        )
    expected_series_path = mapped_path(
        target.bazarr_root_path, target.series_relative_path
    )
    series_candidates = [
        series
        for series in client.series()
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
    if _provider_conflict(target, series):
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
    expected_episode_path = _path_key(target.target_path)
    episode_candidates = [
        episode
        for episode in client.episodes(series_id)
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
    bazarr_season = _integer(episode.get("season"))
    bazarr_episode = _integer(episode.get("episode"))
    if (
        target.season_number is not None
        and bazarr_season is not None
        and target.season_number != bazarr_season
    ) or (
        target.episode_number is not None
        and bazarr_episode is not None
        and target.episode_number != bazarr_episode
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


def _subtitle_summary(value: dict) -> dict:
    return {
        "language": _provider_id(value, "language", "lang", "code"),
        "name": _provider_id(value, "name", "language_name", "label", "provider"),
        "provider": _provider_id(value, "provider", "provider_name"),
        "hearingImpaired": _bool_value(value.get("hearing_impaired", value.get("hi"))),
        "forced": _bool_value(value.get("forced")),
        "format": _provider_id(value, "format", "extension", "codec"),
    }


def _candidate(value: dict) -> dict | None:
    provider = _provider_id(value, "provider", "provider_name")
    subtitle = _provider_id(value, "subtitle", "subtitleId", "subtitle_id", "id")
    if not provider or not subtitle:
        return None
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
        "name": _provider_id(value, "name", "release", "filename", "label", "provider")
        or provider,
        "hearingImpaired": hi,
        "forced": forced,
        "originalFormat": original_format,
        "format": _provider_id(value, "format", "extension", "codec"),
        "score": value.get("score"),
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
                "This TV library has no path available to the subtitle downloader.",
            )
        return BazarrClient(connection)

    def status(self, user_id: str, entity_id: str, source_id: str | None) -> dict:
        target = _target(user_id, entity_id, source_id)
        try:
            resolution = _find_episode(self._client(target), target)
        except BazarrMatchError as error:
            return _target_status(target, None, error.code, str(error))
        return _target_status(target, resolution, "matched")

    def search(self, user_id: str, entity_id: str, source_id: str | None) -> dict:
        target = _target(user_id, entity_id, source_id)
        client = self._client(target)
        resolution = _find_episode(client, target)
        raw_matches = client.search(resolution["episodeId"])
        matches = []
        for raw in raw_matches:
            value = _candidate(raw)
            if value is None:
                continue
            match_id = issue_ticket(
                user_id,
                "resource",
                BAZARR_MATCH_TTL_SECONDS,
                entity=entity_id,
                sourceId=target.source_id,
                mediaFileId=target.media_file_id,
                size=target.size,
                modifiedNs=target.modified_ns,
                fingerprint=target.quick_fingerprint,
                seriesId=resolution["seriesId"],
                episodeId=resolution["episodeId"],
                candidateId=value["candidateId"],
                provider=value["provider"],
                subtitle=value["subtitle"],
                hi=value["hearingImpaired"],
                forced=value["forced"],
                originalFormat=value["originalFormat"],
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
        resolution = _find_episode(self._client(target), target)
        if (
            payload.get("seriesId") != resolution["seriesId"]
            or payload.get("episodeId") != resolution["episodeId"]
        ):
            raise HTTPException(
                409,
                "The subtitle downloader episode changed; search for subtitles again.",
            )
        self._client(target).download(
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
