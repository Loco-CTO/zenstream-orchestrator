"""Native media-library inventory and background job runtime."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from app.config import Config
from app.logging_config import get_logger

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - optional in minimal installations
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment,misc]


LIBRARY_TYPES = {"tv_series", "movies", "music", "collection"}
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".webm", ".mpg", ".mpeg", ".vob"}
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aiff", ".aif", ".ape", ".wv"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
LYRIC_EXTENSIONS = {".lrc", ".elrc", ".txt"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
ID_RE = re.compile(r"\[(?P<provider>tmdbid|tvdbid|imdbid)-(?P<id>[^\]]+)\]", re.IGNORECASE)
EPISODE_RE = re.compile(r"(?i)(?:^|[^A-Z0-9])S(?P<season>\d+)E(?P<episode>\d+)(?:[-.]?E(?P<end>\d+))?")
SEASON_RE = re.compile(r"(?i)^(?:season\s*|s)(\d+)$")
ACTIVE_JOB_STATES = ("queued", "running", "terminating")
logger = get_logger("library")


class JobTerminated(Exception):
    """Raised when a background worker acknowledges a termination request."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def normalized_path(path: str) -> str:
    value = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.isdir(value):
        raise ValueError("Library directory does not exist or is not a directory.")
    return os.path.normcase(os.path.normpath(value))


def relative(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def provider_ids(name: str) -> list[tuple[str, str, str]]:
    result = []
    for match in ID_RE.finditer(name):
        provider = match.group("provider").lower()
        provider = {"tmdbid": "tmdb", "tvdbid": "tvdb", "imdbid": "imdb"}[provider]
        identifier_type = {"tmdb": "movie", "tvdb": "series", "imdb": "imdb"}[provider]
        result.append((provider, identifier_type, match.group("id").strip()))
    return result


def parse_nfo_ids(path: Path) -> list[tuple[str, str, str]]:
    if path.suffix.lower() != ".nfo":
        return []
    try:
        import xml.etree.ElementTree as ET

        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return []
    values = []
    for node in root.findall(".//uniqueid"):
        provider = (node.attrib.get("type") or "").lower()
        value = (node.text or "").strip()
        if not value:
            continue
        if provider in {"tmdb", "themoviedb"}:
            values.append(("tmdb", "movie", value))
        elif provider in {"tvdb", "thetvdb"}:
            values.append(("tvdb", "series", value))
        elif provider in {"imdb"}:
            values.append(("imdb", "imdb", value))
    return values


def parse_audio_tags(path: Path) -> dict[str, str]:
    try:
        from mutagen import File

        audio = File(path, easy=False)
        if audio is None or not audio.tags:
            return {}
        tags: dict[str, str] = {}
        for raw_key, raw_value in audio.tags.items():
            key = str(raw_key).upper()
            if isinstance(raw_value, (list, tuple)):
                value = str(raw_value[0]) if raw_value else ""
            else:
                value = str(raw_value)
            if value:
                tags[key] = value
        return tags
    except Exception:
        return {}


def guess_media(path: Path) -> dict:
    """Use GuessIt as a tolerant fallback for release-style filenames."""
    try:
        from guessit import guessit

        value = guessit(path.name)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def media_role(path: Path) -> str | None:
    suffix = path.suffix.lower()
    name = path.stem.lower()
    if suffix in VIDEO_EXTENSIONS or suffix in AUDIO_EXTENSIONS:
        return "media"
    if suffix in SUBTITLE_EXTENSIONS:
        return "subtitle"
    if suffix in LYRIC_EXTENSIONS:
        return "lyrics"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if name == "theme" or path.parent.name.lower() == "theme-music":
        return "theme"
    return None


class LibraryStore:
    def __init__(self):
        self.db = Config().database

    def list(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id,name,type,directory,watch_enabled,scan_interval_minutes,scan_state,scan_error,last_scan_started_at,last_scan_finished_at,created_at,updated_at FROM libraries ORDER BY name COLLATE NOCASE"
        )
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row[0], "name": row[1], "type": row[2], "directory": row[3],
            "watchEnabled": bool(row[4]), "scanIntervalMinutes": row[5], "scanState": row[6],
            "scanError": row[7], "lastScanStartedAt": row[8], "lastScanFinishedAt": row[9],
            "createdAt": row[10], "updatedAt": row[11],
        }

    def get(self, library_id: str) -> dict | None:
        rows = self.db.execute(
            "SELECT id,name,type,directory,watch_enabled,scan_interval_minutes,scan_state,scan_error,last_scan_started_at,last_scan_finished_at,created_at,updated_at FROM libraries WHERE id=?",
            (library_id,),
        )
        return self._row(rows[0]) if rows else None

    def sources(self, library_id: str) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT source_library_id FROM library_sources WHERE library_id=? ORDER BY source_library_id", (library_id,))]

    def create(self, name: str, library_type: str, directory: str | None, watch_enabled: bool = True, interval: int = 1440, source_ids: Iterable[str] = ()) -> dict:
        name = name.strip()
        if not name or library_type not in LIBRARY_TYPES:
            raise ValueError("A name and supported library type are required.")
        if library_type == "collection":
            directory = None
            source_ids = list(dict.fromkeys(source_ids))
            if not source_ids:
                raise ValueError("A Collection library needs at least one Movie or TV source library.")
        else:
            if not directory:
                raise ValueError("A directory is required for physical libraries.")
            directory = normalized_path(directory)
        interval = max(15, min(43200, int(interval or 1440)))
        library_id = new_id()
        timestamp = now()
        with self.db.transaction() as cursor:
            cursor.execute("SELECT 1 FROM libraries WHERE name=? COLLATE NOCASE", (name,))
            if cursor.fetchone():
                raise ValueError("A library with that name already exists.")
            if directory:
                cursor.execute("SELECT 1 FROM libraries WHERE directory=?", (directory,))
                if cursor.fetchone():
                    raise ValueError("A library already uses that directory.")
            cursor.execute(
                "INSERT INTO libraries(id,name,type,directory,watch_enabled,scan_interval_minutes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (library_id, name, library_type, directory, int(watch_enabled), interval, timestamp, timestamp),
            )
            for source_id in source_ids:
                cursor.execute("SELECT type FROM libraries WHERE id=?", (source_id,))
                source = cursor.fetchone()
                if not source or source[0] not in {"movies", "tv_series"}:
                    raise ValueError("Collections can source only Movie and TV libraries.")
                cursor.execute("INSERT INTO library_sources(library_id,source_library_id) VALUES(?,?)", (library_id, source_id))
        return self.get(library_id)  # type: ignore[return-value]

    def update(self, library_id: str, values: dict) -> dict:
        current = self.get(library_id)
        if not current:
            raise KeyError("Library not found")
        name = str(values.get("name", current["name"])).strip()
        interval = max(15, min(43200, int(values.get("scanIntervalMinutes", current["scanIntervalMinutes"]) or 1440)))
        directory = current["directory"]
        if current["type"] != "collection" and "directory" in values:
            directory = normalized_path(str(values["directory"]))
        watch_enabled = int(bool(values.get("watchEnabled", current["watchEnabled"])))
        self.db.execute(
            "UPDATE libraries SET name=?,directory=?,watch_enabled=?,scan_interval_minutes=?,updated_at=? WHERE id=?",
            (name, directory, watch_enabled, interval, now(), library_id),
        )
        if current["type"] == "collection" and "sourceLibraryIds" in values:
            source_ids = list(dict.fromkeys(values["sourceLibraryIds"]))
            with self.db.transaction() as cursor:
                cursor.execute("DELETE FROM library_sources WHERE library_id=?", (library_id,))
                for source_id in source_ids:
                    cursor.execute("SELECT type FROM libraries WHERE id=?", (source_id,))
                    source = cursor.fetchone()
                    if not source or source[0] not in {"movies", "tv_series"}:
                        raise ValueError("Collections can source only Movie and TV libraries.")
                    cursor.execute("INSERT INTO library_sources(library_id,source_library_id) VALUES(?,?)", (library_id, source_id))
        return self.get(library_id)  # type: ignore[return-value]

    def delete(self, library_id: str) -> bool:
        library = self.get(library_id)
        if not library:
            return False
        self.db.execute("DELETE FROM libraries WHERE id=?", (library_id,))
        return True

    def set_scan_state(self, library_id: str, state: str, error: str | None = None, started: str | None = None, finished: str | None = None) -> None:
        self.db.execute(
            "UPDATE libraries SET scan_state=?,scan_error=?,last_scan_started_at=COALESCE(?,last_scan_started_at),last_scan_finished_at=COALESCE(?,last_scan_finished_at),updated_at=? WHERE id=?",
            (state, error, started, finished, now(), library_id),
        )

    def create_job(self, library_id: str, kind: str) -> dict:
        job_id = new_id()
        timestamp = now()
        self.db.execute("INSERT INTO library_jobs(id,library_id,kind,created_at) VALUES(?,?,?,?)", (job_id, library_id, kind, timestamp))
        return self.job(job_id)  # type: ignore[return-value]

    def job(self, job_id: str) -> dict | None:
        rows = self.db.execute("SELECT id,library_id,kind,state,progress_current,progress_total,message,error,error_details,created_at,started_at,finished_at FROM library_jobs WHERE id=?", (job_id,))
        if not rows:
            return None
        row = rows[0]
        return {"id": row[0], "libraryId": row[1], "kind": row[2], "state": row[3], "progressCurrent": row[4], "progressTotal": row[5], "message": row[6], "error": row[7], "errorDetails": row[8], "createdAt": row[9], "startedAt": row[10], "finishedAt": row[11]}

    def jobs(self, library_id: str) -> list[dict]:
        return [self.job(row[0]) for row in self.db.execute("SELECT id FROM library_jobs WHERE library_id=? ORDER BY created_at DESC LIMIT 50", (library_id,)) if self.job(row[0])]  # type: ignore[list-item]

    def update_job(self, job_id: str, **values) -> None:
        allowed = {"state", "progress_current", "progress_total", "message", "error", "error_details", "started_at", "finished_at"}
        updates = [(key, value) for key, value in values.items() if key in allowed]
        if not updates:
            return
        fields = ",".join(f"{key}=?" for key, _ in updates)
        self.db.execute(f"UPDATE library_jobs SET {fields} WHERE id=?", [value for _, value in updates] + [job_id])


class LibraryScanner:
    def __init__(self, store: LibraryStore | None = None):
        self.store = store or LibraryStore()
        self.db = self.store.db
        self._scan_seen_ids: set[str] = set()
        self._scan_created_ids: list[str] = []

    def scan(self, library_id: str, job_id: str, should_terminate: Callable[[], bool] | None = None) -> None:
        should_terminate = should_terminate or (lambda: False)
        library = self.store.get(library_id)
        if not library:
            raise ValueError("Library not found")
        if library["type"] == "collection":
            self.derive_collection(library_id, job_id, should_terminate)
            return
        root = Path(library["directory"])
        if not root.is_dir():
            raise ValueError("Library directory is no longer available")
        started = now()
        self.store.set_scan_state(library_id, "scanning", started=started, error=None)
        self.store.update_job(job_id, state="running", started_at=started, message="Discovering media")
        self._scan_seen_ids = set()
        self._scan_created_ids = []
        try:
            self._check_termination(should_terminate)
            if library["type"] == "movies":
                count = self._scan_movies(library_id, root, job_id, should_terminate)
            elif library["type"] == "tv_series":
                count = self._scan_series(library_id, root, job_id, should_terminate, resolve_immediately=True)
            else:
                count = self._scan_music(library_id, root, job_id, should_terminate)
            if library["type"] != "tv_series":
                self._resolve_and_seed(library_id, library["type"], job_id, should_terminate)
            self._prune_missing_entities(library_id)
            finished = now()
            self.store.update_job(job_id, state="completed", progress_current=count, progress_total=count, finished_at=finished, message=f"Indexed {count} entries")
            self.store.set_scan_state(library_id, "ready", finished=finished)
        except JobTerminated:
            finished = now()
            self.store.update_job(job_id, state="terminated", message="Terminated by administrator", error=None, finished_at=finished)
            self.store.set_scan_state(library_id, "ready", finished=finished)
        except Exception as error:
            self._remove_created_entities()
            details = {"libraryId": library_id, "jobId": job_id, "exception": type(error).__name__, "traceback": traceback.format_exc()}
            summary = f"Library scan failed for '{library.get('name', library_id)}': {type(error).__name__}: {error}"
            logger.exception("library scan failed library_id=%s job_id=%s", library_id, job_id)
            self.store.update_job(job_id, state="failed", error=summary, error_details=json.dumps(details), finished_at=now())
            self.store.set_scan_state(library_id, "error", error=summary, finished=now())
            raise

    @staticmethod
    def _check_termination(should_terminate: Callable[[], bool]) -> None:
        if should_terminate():
            raise JobTerminated()

    def _entity(self, library_id: str, parent_id: str | None, entity_type: str, path: str | None, **numbers) -> str:
        timestamp = now()
        fields = {"season_number": None, "episode_number": None, "episode_end_number": None, "disc_number": None, "track_number": None}
        fields.update(numbers)
        existing = self.db.execute(
            "SELECT id FROM library_entities WHERE library_id=? AND entity_type=? AND relative_path IS ?",
            (library_id, entity_type, path),
        )
        if existing:
            entity_id = existing[0][0]
            self.db.execute(
                "UPDATE library_entities SET parent_id=?,season_number=?,episode_number=?,episode_end_number=?,disc_number=?,track_number=?,updated_at=? WHERE id=?",
                (parent_id, fields["season_number"], fields["episode_number"], fields["episode_end_number"], fields["disc_number"], fields["track_number"], timestamp, entity_id),
            )
        else:
            entity_id = new_id()
            self.db.execute(
                "INSERT INTO library_entities(id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,disc_number,track_number,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (entity_id, library_id, parent_id, entity_type, path, fields["season_number"], fields["episode_number"], fields["episode_end_number"], fields["disc_number"], fields["track_number"], timestamp, timestamp),
            )
            self._scan_created_ids.append(entity_id)
        self._scan_seen_ids.add(entity_id)
        return entity_id

    def _prune_missing_entities(self, library_id: str) -> None:
        rows = self.db.execute("SELECT id FROM library_entities WHERE library_id=?", (library_id,))
        missing = [row[0] for row in rows if row[0] not in self._scan_seen_ids]
        if not missing:
            return
        with self.db.transaction() as cursor:
            cursor.executemany("DELETE FROM library_entities WHERE id=?", [(entity_id,) for entity_id in missing])

    def _remove_created_entities(self) -> None:
        if not self._scan_created_ids:
            return
        with self.db.transaction() as cursor:
            for entity_id in reversed(self._scan_created_ids):
                cursor.execute("DELETE FROM library_entities WHERE id=?", (entity_id,))
        self._scan_created_ids = []

    def _needs_metadata(self, entity_id: str) -> bool:
        row = self.db.execute("SELECT match_status FROM library_entities WHERE id=?", (entity_id,))
        if not row or row[0][0] in {"unresolved", "failed"}:
            return True
        return not bool(self.db.execute("SELECT 1 FROM entity_provider_ids WHERE entity_id=? LIMIT 1", (entity_id,)))

    def _ids(self, entity_id: str, values: Iterable[tuple[str, str, str]]) -> None:
        from app.providers import PRIMARY_PROVIDER_BY_ENTITY

        row = self.db.execute("SELECT entity_type FROM library_entities WHERE id=?", (entity_id,))
        entity_type = row[0][0] if row else ""
        primary_provider = PRIMARY_PROVIDER_BY_ENTITY.get(entity_type)
        found = False
        for provider, identifier_type, value in values:
            if provider in {"tmdb", "tvdb"} and entity_type in {"movie", "series"}:
                identifier_type = "movie" if entity_type == "movie" else "series"
            self.db.execute("INSERT OR REPLACE INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES(?,?,?,?,?)", (entity_id, provider, identifier_type, value, int(provider == primary_provider)))
            found = True
        if found:
            self.db.execute("UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='explicit_id',updated_at=? WHERE id=?", (now(), entity_id))

    def _resolve_and_seed(self, library_id: str, library_type: str, job_id: str, should_terminate: Callable[[], bool]) -> None:
        """Resolve top-level inventory entities and seed English/common metadata."""
        from app.providers import MetadataService, ProviderError

        entity_types = {
            "movies": {"movie"},
            "tv_series": {"series"},
            "music": {"artist", "release", "track"},
        }.get(library_type, set())
        if not entity_types:
            return
        # Resolve roots first. Releases and tracks are resolved after their
        # artist/release parents have supplied stable MusicBrainz IDs.
        parent_filter = " AND parent_id IS NULL"
        rows = self.db.execute(
            "SELECT id,entity_type,relative_path,season_number,episode_number FROM library_entities WHERE library_id=?{} AND entity_type IN ({}) ORDER BY relative_path".format(parent_filter, ",".join("?" * len(entity_types))),
            [library_id, *sorted(entity_types)],
        )
        created_ids = set(self._scan_created_ids)
        rows = [row for row in rows if row[0] in self._scan_seen_ids and (row[0] in created_ids or self._needs_metadata(row[0]))]
        self.store.update_job(job_id, progress_total=len(rows), progress_current=0, message="Resolving provider metadata")
        service = MetadataService()
        for index, (entity_id, entity_type, relative_path, _season, _episode) in enumerate(rows, start=1):
            self._check_termination(should_terminate)
            query, year = _inventory_query(relative_path or "")
            explicit = [
                {"provider": row[0], "id": row[2]}
                for row in self.db.execute("SELECT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id=?", (entity_id,))
            ]
            try:
                result = service.resolve_inventory_entity(entity_type, query, year, explicit)
            except ProviderError as error:
                self.db.execute("UPDATE library_entities SET match_status='failed',match_confidence=NULL,match_method='scan_resolution',updated_at=? WHERE id=?", (now(), entity_id))
                logger.exception("scan resolution failed library_id=%s entity_id=%s entity_type=%s path=%s", library_id, entity_id, entity_type, relative_path)
                raise ValueError(f"Metadata resolution failed for {entity_type} '{query}' at '{relative_path}': {error}") from error
            values = []
            for value in result["providerIds"]:
                identifier_type = "movie" if entity_type == "movie" else "series" if entity_type == "series" else entity_type
                values.append((value["provider"], identifier_type, value["id"]))
            self._ids(entity_id, values)
            self.db.execute("UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='scan_resolution',updated_at=? WHERE id=?", (now(), entity_id))
            if entity_type == "series":
                self._derive_tmdb_child_ids(entity_id)
                self._derive_provider_child_ids(entity_id, result["metadata"])
                self._derive_tvdb_episode_ids(entity_id, service)
                self.db.execute("UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='parent_resolution',updated_at=? WHERE parent_id=? AND match_status='unresolved'", (now(), entity_id))
            self.store.update_job(job_id, progress_current=index, message=f"Resolved {query}")
        self._seed_all_children(library_id, service, job_id, should_terminate)

    def _resolve_series_immediately(self, library_id: str, series_id: str, relative_path: str, service, job_id: str, should_terminate: Callable[[], bool]) -> None:
        """Resolve one discovered series and all of its children before continuing."""
        from app.providers import ProviderError

        self._check_termination(should_terminate)
        if self._needs_metadata(series_id):
            query, year = _inventory_query(relative_path or "")
            explicit = [
                {"provider": row[0], "id": row[2]}
                for row in self.db.execute("SELECT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id=?", (series_id,))
            ]
            try:
                result = service.resolve_inventory_entity("series", query, year, explicit)
            except ProviderError as error:
                self.db.execute("UPDATE library_entities SET match_status='failed',match_confidence=NULL,match_method='scan_resolution',updated_at=? WHERE id=?", (now(), series_id))
                raise ValueError(f"Metadata resolution failed for series '{query}' at '{relative_path}': {error}") from error
            self._ids(series_id, [(value["provider"], "series", value["id"]) for value in result["providerIds"]])
            self.db.execute("UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='scan_resolution',updated_at=? WHERE id=?", (now(), series_id))
        self._aggregate_series_children(series_id, service)
        self._derive_tmdb_child_ids(series_id)
        # The series hierarchy is useful for bulk caching but can omit an
        # episode from the returned aggregate.  Season details are the
        # authoritative TVDB source for exact episode IDs, so run the same
        # derivation used by the incremental resolution path before seeding.
        self._derive_tvdb_episode_ids(series_id, service)
        self.db.execute("UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='parent_resolution',updated_at=? WHERE parent_id=? AND match_status='unresolved'", (now(), series_id))
        self._seed_all_children(library_id, service, job_id, should_terminate, parent_id=series_id)

    def _aggregate_series_children(self, series_id: str, service) -> None:
        """Map all discovered seasons and episodes from resolved parent IDs."""
        from app.providers import ProviderError

        provider_rows = self.db.execute(
            "SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY is_primary DESC,provider",
            (series_id,),
        )
        primary_provider = "tvdb"
        child_rows = self.db.execute(
            "SELECT id,entity_type,season_number,episode_number FROM library_entities WHERE parent_id=? AND entity_type='season' ORDER BY season_number",
            (series_id,),
        )
        seasons = list(child_rows)
        season_ids = [row[0] for row in seasons]
        episodes = []
        if season_ids:
            episodes = self.db.execute(
                "SELECT id,entity_type,season_number,episode_number FROM library_entities WHERE parent_id IN ({}) AND entity_type='episode' ORDER BY season_number,episode_number".format(",".join("?" * len(season_ids))),
                season_ids,
            )
        by_provider = {row[0]: row[1] for row in provider_rows}
        if not by_provider.get(primary_provider):
            raise ProviderError(f"Resolved series {series_id} has no TVDB ID")
        for provider, provider_id in by_provider.items():
            if provider not in {"tvdb", "tmdb"}:
                continue
            try:
                aggregate = service.aggregate_series(provider, provider_id, "en")
            except Exception as error:
                if provider == primary_provider:
                    raise
                logger.warning("secondary series aggregation failed series_id=%s provider=%s: %s", series_id, provider, error)
                continue
            mapped_seasons = {}
            for metadata in aggregate.get("seasons", []):
                child_number = next((value.get("season") for value in metadata.get("children", []) if value.get("type") == "episode"), None)
                if child_number is None:
                    # Season metadata itself does not always echo its number;
                    # provider IDs for TMDB encode it, while TVDB payloads do.
                    provider_value = str(metadata.get("providerId") or "")
                    child_number = provider_value.rsplit(":", 1)[-1] if provider == "tmdb" and ":" in provider_value else None
                if child_number is None:
                    child_number = metadata.get("seasonNumber")
                if child_number is None:
                    continue
                mapped_seasons[int(child_number)] = metadata
            for child_id, entity_type, season_number, episode_number in seasons:
                metadata = mapped_seasons.get(int(season_number))
                if not metadata:
                    continue
                self._ids(child_id, [(provider, "season", str(metadata["providerId"]))])
                self._persist_normalized_ids(child_id, "season", metadata)
            mapped_episodes = {}
            for metadata in aggregate.get("episodes", []):
                provider_value = str(metadata.get("providerId") or "")
                if provider == "tmdb" and provider_value.count(":") >= 2:
                    parts = provider_value.split(":")
                    key = (int(parts[-2]), int(parts[-1]))
                else:
                    episode_child = next((value for value in metadata.get("children", []) if value.get("type") == "episode"), None)
                    season_value = episode_child.get("season") if episode_child else metadata.get("seasonNumber")
                    episode_value = episode_child.get("episode") if episode_child else metadata.get("episodeNumber")
                    if season_value is None or episode_value is None:
                        continue
                    key = (int(season_value), int(episode_value))
                mapped_episodes[key] = metadata
            for child_id, entity_type, season_number, episode_number in episodes:
                metadata = mapped_episodes.get((int(season_number), int(episode_number)))
                if not metadata:
                    continue
                self._ids(child_id, [(provider, "episode", str(metadata["providerId"]))])
                self._persist_normalized_ids(child_id, "episode", metadata)

    def _seed_all_children(self, library_id: str, service, job_id: str, should_terminate: Callable[[], bool], parent_id: str | None = None) -> None:
        """Fetch common metadata and IDs for every season, episode, release, and track."""
        if parent_id:
            rows = self.db.execute(
                "SELECT id,entity_type,relative_path,parent_id,season_number,episode_number FROM library_entities WHERE library_id=? AND (parent_id=? OR parent_id IN (SELECT id FROM library_entities WHERE parent_id=? AND entity_type='season')) ORDER BY length(relative_path),relative_path",
                (library_id, parent_id, parent_id),
            )
        else:
            rows = self.db.execute("SELECT id,entity_type,relative_path,parent_id,season_number,episode_number FROM library_entities WHERE library_id=? AND parent_id IS NOT NULL ORDER BY length(relative_path),relative_path", (library_id,))
        created_ids = set(self._scan_created_ids)
        rows = [row for row in rows if row[0] in self._scan_seen_ids and (row[0] in created_ids or self._needs_metadata(row[0]))]
        self.store.update_job(job_id, progress_total=len(rows), progress_current=0, message="Seeding child metadata")
        for index, (entity_id, entity_type, relative_path, row_parent_id, season_number, episode_number) in enumerate(rows, start=1):
            self._check_termination(should_terminate)
            provider_rows = self.db.execute("SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY is_primary DESC,provider", (entity_id,))
            if not provider_rows:
                if entity_type in {"season", "episode"} and parent_id:
                    raise ValueError(f"No provider ID was aggregated for {entity_type} '{relative_path}'")
                query, year = _inventory_query(relative_path or "")
                try:
                    result = service.resolve_inventory_entity(entity_type, query, year, [])
                    self._ids(entity_id, [(value["provider"], entity_type, value["id"]) for value in result["providerIds"]])
                    provider_rows = [(value["provider"], value["id"]) for value in result["providerIds"]]
                except Exception as error:
                    self.db.execute("UPDATE library_entities SET match_status='failed',match_method='scan_resolution',updated_at=? WHERE id=?", (now(), entity_id))
                    logger.exception("child resolution failed library_id=%s entity_id=%s type=%s path=%s", library_id, entity_id, entity_type, relative_path)
                    raise ValueError(f"Metadata resolution failed for {entity_type} '{relative_path}': {type(error).__name__}: {error}") from error
            priorities = {"season": ["tvdb", "tmdb"], "episode": ["tvdb", "tmdb"], "release": ["musicbrainz"], "track": ["musicbrainz"]}.get(entity_type, [row[0] for row in provider_rows])
            required = next((provider for provider in priorities if any(row[0] == provider for row in provider_rows)), priorities[0] if priorities else None)
            fetched = False
            required_succeeded = False
            errors = []
            for provider in priorities:
                provider_id = next((row[1] for row in provider_rows if row[0] == provider), None)
                if not provider_id:
                    continue
                try:
                    normalized = service.fetch(provider, entity_type, provider_id, "en", force=False)
                    fetched = True
                    if provider == required:
                        required_succeeded = True
                    self._persist_normalized_ids(entity_id, entity_type, normalized)
                    self._persist_child_ids(entity_id, normalized)
                except Exception as error:
                    errors.append(f"{provider}: {type(error).__name__}: {error}")
                    logger.exception("child metadata seed failed entity_id=%s type=%s provider=%s provider_id=%s", entity_id, entity_type, provider, provider_id)
            if not fetched or (required and not required_succeeded):
                raise ValueError(f"Metadata resolution failed for {entity_type} '{relative_path}': required provider {required or 'provider'} could not be seeded; {'; '.join(errors) or 'no usable provider metadata'}")
            self.db.execute("UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='scan_child_resolution',updated_at=? WHERE id=?", (now(), entity_id))
            self.store.update_job(job_id, progress_current=index, message=f"Seeded {entity_type} {relative_path}")

    def _persist_normalized_ids(self, entity_id: str, entity_type: str, normalized: dict) -> None:
        values = []
        for value in normalized.get("ids", []) or []:
            if value.get("provider") and value.get("id"):
                values.append((value["provider"], value.get("identifierType") or entity_type, str(value["id"])))
        self._ids(entity_id, values)

    def _persist_child_ids(self, parent_id: str, normalized: dict) -> None:
        """Attach provider child IDs to inventory children by stable numbers."""
        tracks = [value for value in normalized.get("tracks", []) or [] if value.get("id")]
        if not tracks:
            return
        children = self.db.execute("SELECT id,track_number,disc_number FROM library_entities WHERE parent_id=? AND entity_type='track' ORDER BY disc_number,track_number,relative_path", (parent_id,))
        for index, (entity_id, track_number, disc_number) in enumerate(children):
            candidate = next((value for value in tracks if track_number is not None and int(value.get("position") or 0) == int(track_number)), None)
            candidate = candidate or (tracks[index] if index < len(tracks) else None)
            if candidate and candidate.get("id"):
                self._ids(entity_id, [("musicbrainz", "recording", str(candidate["id"]))])

    def _derive_tmdb_child_ids(self, series_id: str) -> None:
        provider_rows = self.db.execute("SELECT provider_id FROM entity_provider_ids WHERE entity_id=? AND provider='tmdb'", (series_id,))
        if not provider_rows:
            return
        seasons = self.db.execute("SELECT id,entity_type,season_number,episode_number FROM library_entities WHERE parent_id=? AND entity_type='season'", (series_id,))
        season_ids = [row[0] for row in seasons]
        children = list(seasons)
        if season_ids:
            children.extend(self.db.execute("SELECT id,entity_type,season_number,episode_number FROM library_entities WHERE parent_id IN ({}) AND entity_type='episode'".format(",".join("?" * len(season_ids))), season_ids))
        for child_id, entity_type, season_number, episode_number in children:
            if entity_type == "season":
                provider_id = f"{provider_rows[0][0]}:{season_number}"
            elif entity_type == "episode" and season_number is not None and episode_number is not None:
                provider_id = f"{provider_rows[0][0]}:{season_number}:{episode_number}"
            else:
                continue
            self._ids(child_id, [("tmdb", entity_type, provider_id)])
            self.db.execute("UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='parent_resolution',updated_at=? WHERE id=?", (now(), child_id))

    def _derive_provider_child_ids(self, series_id: str, metadata: dict) -> None:
        if metadata.get("provider") == "tmdb":
            return
        seasons = self.db.execute("SELECT id,entity_type,season_number,episode_number FROM library_entities WHERE parent_id=? AND entity_type='season'", (series_id,))
        season_ids = [row[0] for row in seasons]
        children = list(seasons)
        if season_ids:
            children.extend(self.db.execute("SELECT id,entity_type,season_number,episode_number FROM library_entities WHERE parent_id IN ({}) AND entity_type='episode'".format(",".join("?" * len(season_ids))), season_ids))
        for child_id, entity_type, season_number, episode_number in children:
            for value in metadata.get("children", []) or []:
                if value.get("type") != entity_type or int(value.get("season", -1)) != int(season_number if season_number is not None else -2):
                    continue
                if entity_type == "episode" and int(value.get("episode", -1)) != int(episode_number if episode_number is not None else -2):
                    continue
                self._ids(child_id, [(metadata.get("provider", ""), entity_type, str(value["id"]))])
                break

    def _derive_tvdb_episode_ids(self, series_id: str, service) -> None:
        """Fetch TVDB season details and attach exact TVDB episode IDs."""
        seasons = self.db.execute(
            "SELECT id,season_number,relative_path FROM library_entities WHERE parent_id=? AND entity_type='season' ORDER BY season_number",
            (series_id,),
        )
        for season_id, season_number, season_path in seasons:
            provider_rows = self.db.execute(
                "SELECT provider_id FROM entity_provider_ids WHERE entity_id=? AND provider='tvdb'",
                (season_id,),
            )
            if not provider_rows:
                raise ValueError(f"TVDB season ID could not be resolved for season {season_number} at '{season_path}'")
            season_provider_id = str(provider_rows[0][0])
            try:
                normalized = service.fetch("tvdb", "season", season_provider_id, "en", force=True)
            except Exception as error:
                raise ValueError(f"TVDB season details failed for season {season_number} at '{season_path}' (ID {season_provider_id}): {type(error).__name__}: {error}") from error
            self._persist_normalized_ids(season_id, "season", normalized)
            episodes = self.db.execute(
                "SELECT id,episode_number,relative_path FROM library_entities WHERE parent_id=? AND entity_type='episode' ORDER BY episode_number,relative_path",
                (season_id,),
            )
            tvdb_children = [value for value in normalized.get("children", []) or [] if value.get("type") == "episode" and value.get("id") is not None]
            for episode_id, episode_number, episode_path in episodes:
                match = next((value for value in tvdb_children if int(value.get("season", season_number)) == int(season_number) and int(value.get("episode", -1)) == int(episode_number)), None)
                if not match:
                    # Some TVDB series are missing a newly aired or
                    # alternate-season episode even though TMDB has it.
                    # Keep the secondary provider identity and let seeding
                    # use it rather than failing the entire library scan.
                    if self.db.execute("SELECT 1 FROM entity_provider_ids WHERE entity_id=? AND provider='tmdb' LIMIT 1", (episode_id,)):
                        continue
                    raise ValueError(f"TVDB episode ID could not be resolved for S{int(season_number):02d}E{int(episode_number):02d} at '{episode_path}'")
                self._ids(episode_id, [("tvdb", "episode", str(match["id"]))])

    def _files(self, entity_id: str, root: Path, files: Iterable[Path]) -> None:
        self.db.execute("DELETE FROM media_files WHERE entity_id=?", (entity_id,))
        for path in files:
            role = media_role(path)
            if not role:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            language = None
            parts = path.stem.split(".")
            if len(parts) > 1 and len(parts[-1]) in {2, 3}:
                language = parts[-1].lower()
            self.db.execute(
                "INSERT OR REPLACE INTO media_files(id,entity_id,relative_path,role,language,flags,size,modified_ns) VALUES(?,?,?,?,?,?,?,?)",
                (new_id(), entity_id, relative(str(root), str(path)), role, language, None, stat.st_size, stat.st_mtime_ns),
            )

    def _scan_movies(self, library_id: str, root: Path, job_id: str, should_terminate: Callable[[], bool]) -> int:
        entries = [path for path in root.iterdir() if path.is_dir() or path.suffix.lower() in VIDEO_EXTENSIONS]
        self.store.update_job(job_id, progress_total=len(entries))
        count = 0
        for entry in entries:
            self._check_termination(should_terminate)
            entity = self._entity(library_id, None, "movie", relative(str(root), str(entry)))
            self._ids(entity, provider_ids(entry.name))
            files = list(entry.rglob("*")) if entry.is_dir() else [entry]
            for nfo in (path for path in files if path.suffix.lower() == ".nfo"):
                self._ids(entity, parse_nfo_ids(nfo))
            self._files(entity, root, [path for path in files if path.is_file()])
            count += 1
            self.store.update_job(job_id, progress_current=count, message=f"Indexed {entry.name}")
        return count

    def _scan_series(self, library_id: str, root: Path, job_id: str, should_terminate: Callable[[], bool], resolve_immediately: bool = False) -> int:
        from app.providers import MetadataService

        series_dirs = [path for path in root.iterdir() if path.is_dir()]
        self.store.update_job(job_id, progress_total=len(series_dirs))
        episode_count = 0
        service = MetadataService() if resolve_immediately else None
        for series_index, series_dir in enumerate(series_dirs, start=1):
            self._check_termination(should_terminate)
            series = self._entity(library_id, None, "series", relative(str(root), str(series_dir)))
            self._ids(series, provider_ids(series_dir.name))
            season_dirs = [path for path in series_dir.iterdir() if path.is_dir() and (SEASON_RE.match(path.name) or path.name.lower() == "specials")]
            if any(path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS for path in series_dir.iterdir()):
                season_dirs.append(series_dir)
            for season_dir in season_dirs:
                match = SEASON_RE.match(season_dir.name)
                season_folder_number = int(match.group(1)) if match else (0 if season_dir.name.lower() == "specials" else 1)
                season = self._entity(library_id, series, "season", relative(str(root), str(season_dir)), season_number=season_folder_number)
                episode_paths = season_dir.iterdir() if season_dir == series_dir else season_dir.rglob("*")
                for media in episode_paths:
                    self._check_termination(should_terminate)
                    if not media.is_file() or media.suffix.lower() not in VIDEO_EXTENSIONS:
                        continue
                    episode_match = EPISODE_RE.search(media.stem)
                    guessed = guess_media(media) if not episode_match else {}
                    if not episode_match and not (guessed.get("season") and guessed.get("episode")):
                        continue
                    filename_season_number = int(episode_match.group("season")) if episode_match else int(guessed["season"])
                    episode_number = int(episode_match.group("episode")) if episode_match else int(guessed["episode"] if not isinstance(guessed["episode"], list) else guessed["episode"][0])
                    end_number = int(episode_match.group("end")) if episode_match and episode_match.group("end") else None
                    # The directory establishes the season hierarchy. Keep the
                    # filename season only for loose episodes stored directly
                    # under the series directory.
                    episode_season_number = filename_season_number if season_dir == series_dir else season_folder_number
                    episode = self._entity(library_id, season, "episode", relative(str(root), str(media)), season_number=episode_season_number, episode_number=episode_number, episode_end_number=end_number)
                    self._ids(episode, provider_ids(media.name))
                    self._files(episode, root, [media] + [sidecar for sidecar in media.parent.iterdir() if sidecar.is_file() and sidecar.stem.startswith(media.stem) and sidecar != media])
                    episode_count += 1
            self._files(series, root, [path for path in series_dir.iterdir() if path.is_file()])
            children = self.db.execute(
                "SELECT id FROM library_entities WHERE library_id=? AND (parent_id=? OR parent_id IN (SELECT id FROM library_entities WHERE parent_id=? AND entity_type='season'))",
                (library_id, series, series),
            )
            needs_resolution = self._needs_metadata(series) or any(self._needs_metadata(row[0]) for row in children if row[0] in self._scan_seen_ids)
            if service and needs_resolution:
                self._resolve_series_immediately(library_id, series, relative(str(root), str(series_dir)), service, job_id, should_terminate)
            self.store.update_job(job_id, progress_current=series_index, message=f"Indexed {series_dir.name} ({episode_count} episodes)")
        return len(series_dirs)

    def _scan_music(self, library_id: str, root: Path, job_id: str, should_terminate: Callable[[], bool]) -> int:
        album_dirs = []
        for directory, _, filenames in os.walk(root):
            if any(Path(name).suffix.lower() in AUDIO_EXTENSIONS for name in filenames):
                album_dirs.append(Path(directory))
        self.store.update_job(job_id, progress_total=len(album_dirs))
        count = 0
        artists: dict[str, str] = {}
        for album_dir in album_dirs:
            self._check_termination(should_terminate)
            artist_name = album_dir.relative_to(root).parts[0] if album_dir.relative_to(root).parts else album_dir.name
            artist = artists.get(artist_name)
            if not artist:
                artist = self._entity(library_id, None, "artist", artist_name)
                artists[artist_name] = artist
            release = self._entity(library_id, artist, "release", relative(str(root), str(album_dir)))
            tracks = [path for path in album_dir.iterdir() if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS]
            for track in sorted(tracks):
                self._check_termination(should_terminate)
                tags = parse_audio_tags(track)
                track_number = _int_tag(tags.get("TRACKNUMBER") or tags.get("TRACK"))
                disc_number = _int_tag(tags.get("DISCNUMBER") or tags.get("DISC"))
                entity = self._entity(library_id, release, "track", relative(str(root), str(track)), disc_number=disc_number, track_number=track_number)
                self._ids(entity, _music_ids(tags))
                self._files(entity, root, [track] + [sidecar for sidecar in track.parent.iterdir() if sidecar.is_file() and sidecar.stem.startswith(track.stem) and sidecar != track])
                count += 1
            self._files(release, root, [path for path in album_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS])
            self.store.update_job(job_id, progress_current=count, message=f"Indexed {album_dir.name}")
        return count

    def derive_collection(self, library_id: str, job_id: str, should_terminate: Callable[[], bool] | None = None) -> None:
        should_terminate = should_terminate or (lambda: False)
        library = self.store.get(library_id)
        if not library:
            raise ValueError("Library not found")
        sources = self.store.sources(library_id)
        self.store.set_scan_state(library_id, "scanning", started=now(), error=None)
        self.store.update_job(job_id, state="running", started_at=now(), message="Deriving collections")
        with self.db.transaction() as cursor:
            cursor.execute("DELETE FROM library_entities WHERE library_id=?", (library_id,))
        try:
            from app.providers import MetadataService, ProviderError, TVDBClient

            service = MetadataService()
            client = service.client("tvdb")
            if not isinstance(client, TVDBClient):
                raise ProviderError("TheTVDB is not configured")
            source_rows = self.db.execute(
                "SELECT e.id,e.entity_type,p.provider_id FROM library_entities e JOIN entity_provider_ids p ON p.entity_id=e.id WHERE e.library_id IN ({}) AND p.provider='tvdb' AND p.identifier_type IN ('series','movie')".format(",".join("?" * len(sources))),
                sources,
            ) if sources else []
            by_id = {(row[1], str(row[2])): row[0] for row in source_rows}
            lists: dict[str, dict] = {}
            # Lists are paged; stop at the first short page to avoid unbounded calls.
            for page in range(0, 50):
                self._check_termination(should_terminate)
                page_values = client.lists(page)
                for value in page_values:
                    if value.get("isOfficial"):
                        lists[str(value.get("id"))] = value
                if len(page_values) < 100:
                    break
            self.store.update_job(job_id, progress_total=len(lists))
            count = 0
            for list_id, base in lists.items():
                self._check_termination(should_terminate)
                try:
                    payload = client.list_details(list_id)
                except ProviderError:
                    continue
                data = payload.get("data", payload)
                members = []
                for entity in data.get("entities", []) or []:
                    key = ("movie", str(entity.get("movieId"))) if entity.get("movieId") else ("series", str(entity.get("seriesId"))) if entity.get("seriesId") else None
                    if key and key in by_id:
                        members.append(by_id[key])
                members = list(dict.fromkeys(members))
                if len(members) < 2:
                    continue
                collection = self._entity(library_id, None, "collection", f"tvdb-list-{list_id}")
                self._ids(collection, [("tvdb", "collection", list_id)])
                for position, source_entity in enumerate(members):
                    self.db.execute("INSERT INTO collection_members(collection_entity_id,source_entity_id,position) VALUES(?,?,?)", (collection, source_entity, position))
                # Store a lightweight localized seed; full metadata remains the provider cache.
                title = base.get("name") or data.get("name") or f"Collection {list_id}"
                service.cache.put("tvdb", "collection", list_id, "en", {"title": title, "overview": data.get("overview"), "provider": "tvdb", "providerId": list_id, "images": []})
                count += 1
                self.store.update_job(job_id, progress_current=count, message=f"Derived {title}")
            self.store.update_job(job_id, state="completed", progress_current=count, progress_total=len(lists), finished_at=now(), message=f"Derived {count} official collections")
            self.store.set_scan_state(library_id, "ready", finished=now())
        except JobTerminated:
            finished = now()
            self.store.update_job(job_id, state="terminated", message="Terminated by administrator", error=None, finished_at=finished)
            self.store.set_scan_state(library_id, "ready", finished=finished)
        except Exception as error:
            summary = f"Collection derivation failed for library '{library_id}': {type(error).__name__}: {error}"
            details = {"libraryId": library_id, "jobId": job_id, "operation": "collection_derivation", "exception": type(error).__name__, "traceback": traceback.format_exc()}
            logger.exception("collection derivation failed library_id=%s job_id=%s", library_id, job_id)
            self.store.update_job(job_id, state="failed", error=summary, error_details=json.dumps(details), finished_at=now())
            self.store.set_scan_state(library_id, "error", error=summary, finished=now())
            raise


def _inventory_query(relative_path: str) -> tuple[str, str | None]:
    path = Path(relative_path)
    raw = path.stem if path.suffix else path.name
    parsed = guess_media(path)
    query = str(parsed.get("title") or raw)
    query = re.sub(r"\[[^\]]+\]", " ", query)
    query = re.sub(r"\s+", " ", query).strip(" .-_[]")
    year = str(parsed.get("year") or "")[:4] or None
    if not year:
        match = re.search(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", raw)
        year = match.group(1) if match else None
    return query or raw, year


def _int_tag(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def _music_ids(tags: dict[str, str]) -> list[tuple[str, str, str]]:
    ids: list[tuple[str, str, str]] = []
    mapping = {
        "MUSICBRAINZ_ARTISTID": "artist",
        "MUSICBRAINZ_ALBUMARTISTID": "artist",
        "MUSICBRAINZ_RELEASEGROUPID": "release_group",
        "MUSICBRAINZ_ALBUMID": "release",
        "MUSICBRAINZ_TRACKID": "recording",
        "MUSICBRAINZ_RELEASETRACKID": "release_track",
        "MUSICBRAINZ_WORKID": "work",
    }
    for key, identifier_type in mapping.items():
        for value in (tags.get(key) or "").split(";"):
            if value.strip():
                ids.append(("musicbrainz", identifier_type, value.strip()))
    return ids


class _LibraryChangeHandler(FileSystemEventHandler):
    def __init__(self, runtime: "LibraryRuntime", library_id: str):
        self.runtime = runtime
        self.library_id = library_id

    def on_any_event(self, event):  # watchdog emits separate create/modify/delete/move events
        if not getattr(event, "is_directory", False):
            self.runtime.request_reconcile(self.library_id)


class LibraryRuntime:
    """Durable scan worker with daily repair scheduling and optional filesystem watching."""

    def __init__(self):
        self.store = LibraryStore()
        self.scanner = LibraryScanner(self.store)
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.observer = None
        self._watch_paths: set[str] = set()
        self._reconcile_due: dict[str, float] = {}
        self._active_jobs: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_lock = threading.RLock()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._recover_active_jobs()
        for library in self.store.list():
            unresolved = self.store.db.execute("SELECT COUNT(*) FROM library_entities WHERE library_id=? AND match_status IN ('unresolved','failed')", (library["id"],))[0][0]
            if unresolved:
                self.enqueue(library["id"], "scan")
        self.stop_event.clear()
        self._configure_watchers()
        self.thread = threading.Thread(target=self._run, name="zenstream-library-jobs", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=5)
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None
        self._watch_paths.clear()

    def refresh_watchers(self) -> None:
        if not (self.thread and self.thread.is_alive()):
            return
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None
        self._watch_paths.clear()
        self._configure_watchers()

    def enqueue(self, library_id: str, kind: str = "scan") -> dict:
        # Scan, reconcile, and collection rebuild all mutate the same inventory.
        # Claim the task atomically so different triggers cannot overlap.
        with self.store.db.transaction() as cursor:
            cursor.execute("SELECT id FROM library_jobs WHERE library_id=? AND state IN ('queued','running','terminating') ORDER BY created_at DESC LIMIT 1", (library_id,))
            existing = cursor.fetchone()
            if existing:
                job_id = existing[0]
            else:
                job_id = new_id()
                cursor.execute("INSERT INTO library_jobs(id,library_id,kind,created_at) VALUES(?,?,?,?)", (job_id, library_id, kind, now()))
        job = self.store.job(job_id)
        with self.condition:
            self.condition.notify_all()
        return job  # type: ignore[return-value]

    def terminate(self, job_id: str) -> dict | None:
        job = self.store.job(job_id)
        if not job or job["state"] not in ACTIVE_JOB_STATES:
            return job
        finished = now()
        with self._active_lock:
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event:
                cancel_event.set()
                self.store.update_job(job_id, state="terminating", message="Termination requested")
            else:
                self.store.update_job(job_id, state="terminated", message="Terminated by administrator", error=None, finished_at=finished)
        with self.condition:
            self.condition.notify_all()
        return self.store.job(job_id)

    def _recover_active_jobs(self) -> None:
        """Resume one interrupted run per library and collapse stale duplicates."""
        rows = self.store.db.execute(
            "SELECT id,library_id,state FROM library_jobs WHERE state IN ('queued','running','terminating') ORDER BY created_at DESC"
        )
        by_library: dict[str, list[tuple[str, str]]] = {}
        for job_id, library_id, state in rows:
            by_library.setdefault(library_id, []).append((job_id, state))
        timestamp = now()
        with self.store.db.transaction() as cursor:
            for library_id, jobs in by_library.items():
                resumable = [job for job in jobs if job[1] != "terminating"]
                keep_id = resumable[0][0] if resumable else None
                for job_id, state in jobs:
                    if job_id == keep_id:
                        cursor.execute("UPDATE library_jobs SET state='queued',progress_current=0,progress_total=0,message='Queued again after Orchestrator restart',error=NULL,started_at=NULL,finished_at=NULL WHERE id=?", (job_id,))
                    else:
                        cursor.execute("UPDATE library_jobs SET state='terminated',message='Superseded by the active task run',error=NULL,finished_at=? WHERE id=?", (timestamp, job_id))
                cursor.execute("UPDATE libraries SET scan_state='idle',scan_error=NULL,updated_at=? WHERE id=?", (timestamp, library_id))

    def request_reconcile(self, library_id: str) -> None:
        # Debounce bursts from copy/move operations into one reconcile job.
        self._reconcile_due[library_id] = time.monotonic() + 5
        with self.condition:
            self.condition.notify_all()

    def _configure_watchers(self) -> None:
        if Observer is None:
            return
        observer = Observer()
        for library in self.store.list():
            directory = library.get("directory")
            if not library.get("watchEnabled") or not directory or not os.path.isdir(directory):
                continue
            try:
                observer.schedule(_LibraryChangeHandler(self, library["id"]), directory, recursive=True)
                self._watch_paths.add(directory)
            except OSError:
                continue
        if self._watch_paths:
            observer.start()
            self.observer = observer

    def _schedule_repairs(self) -> None:
        now_epoch = time.time()
        for library in self.store.list():
            if library["type"] == "collection" or not library.get("scanIntervalMinutes"):
                continue
            finished = library.get("lastScanFinishedAt")
            try:
                due = not finished or datetime.fromisoformat(finished).timestamp() + library["scanIntervalMinutes"] * 60 <= now_epoch
            except (TypeError, ValueError, OSError):
                due = True
            unresolved = self.store.db.execute("SELECT COUNT(*) FROM library_entities WHERE library_id=? AND match_status IN ('unresolved','failed')", (library["id"],))[0][0]
            due = due or (bool(unresolved) and not finished)
            if due:
                self.enqueue(library["id"], "scan")

    def _run(self):
        while not self.stop_event.is_set():
            due = [library_id for library_id, deadline in self._reconcile_due.items() if time.monotonic() >= deadline]
            for library_id in due:
                self.enqueue(library_id, "reconcile")
                self._reconcile_due.pop(library_id, None)
            rows = self.store.db.execute("SELECT id,library_id,kind FROM library_jobs WHERE state='queued' ORDER BY created_at LIMIT 1")
            if not rows:
                with self.condition:
                    self.condition.wait(timeout=1)
                continue
            job_id, library_id, kind = rows[0]
            with self._active_lock:
                if job_id in self._active_jobs:
                    with self.condition:
                        self.condition.wait(timeout=0.2)
                    continue
                self._active_jobs.add(job_id)
                self._cancel_events[job_id] = threading.Event()
                with self.store.db.transaction() as cursor:
                    cursor.execute("UPDATE library_jobs SET state='running',started_at=?,message='Starting scan' WHERE id=? AND state='queued'", (now(), job_id))
                    claimed = cursor.rowcount == 1
                if not claimed:
                    self._active_jobs.discard(job_id)
                    self._cancel_events.pop(job_id, None)
                    continue
            threading.Thread(target=self._execute_job, args=(job_id, library_id, kind), name=f"zenstream-library-{job_id[:8]}", daemon=True).start()

    def _execute_job(self, job_id: str, library_id: str, kind: str) -> None:
        try:
            if kind in {"scan", "reconcile", "collection_rebuild"}:
                self.scanner.scan(library_id, job_id, self._cancel_events[job_id].is_set)
            else:
                self.store.update_job(job_id, state="failed", error=f"Unsupported job kind: {kind}", finished_at=now())
        except Exception:
            # The scanner records the durable error; keep the worker alive for later jobs.
            logger.exception("library worker failed job_id=%s library_id=%s kind=%s", job_id, library_id, kind)
            pass
        finally:
            with self._active_lock:
                self._active_jobs.discard(job_id)
                self._cancel_events.pop(job_id, None)
            with self.condition:
                self.condition.notify_all()


runtime = LibraryRuntime()
