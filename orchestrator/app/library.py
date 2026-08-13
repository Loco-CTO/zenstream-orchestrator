from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import re
import stat
import threading
import time
import traceback
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import Future, as_completed
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty

from app.config import Config
from app.images import LocalArtworkCache, blurhash_for_image
from app.logging_config import get_logger
from app.worker_config import configured_worker_limit

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - optional in minimal installations
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment,misc]


LIBRARY_TYPES = {"tv_series", "movies", "music", "collection"}
VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".wmv",
    ".ts",
    ".m2ts",
    ".webm",
    ".mpg",
    ".mpeg",
    ".vob",
}
AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".wma",
    ".aiff",
    ".aif",
    ".ape",
    ".wv",
}
# Sidecars are deliberately broader than the formats rendered natively by
# either client. The playback API normalizes text and bitmap subtitle formats
# to WebVTT at request time.
SUBTITLE_EXTENSIONS = {
    ".srt",
    ".ass",
    ".ssa",
    ".vtt",
    ".webvtt",
    ".sub",
    ".smi",
    ".sami",
    ".ttml",
    ".dfxp",
    ".xml",
    ".sup",
    ".idx",
    ".mks",
    ".mpl2",
    ".rt",
    ".scc",
    ".stl",
    ".usf",
    ".cap",
    ".pjs",
    ".aqt",
    ".jacosub",
    ".gsub",
    ".dks",
    ".mpsub",
    ".xss",
}
LYRIC_EXTENSIONS = {
    ".lrc",
    ".elrc",
    ".txt",
    ".lyrics",
    ".qrc",
    ".krc",
    ".ksc",
    ".irc",
    ".yrc",
}
LANGUAGE_ALIASES = {
    "eng": "en",
    "jpn": "ja",
    "jap": "ja",
    "deu": "de",
    "ger": "de",
    "fra": "fr",
    "fre": "fr",
    "spa": "es",
    "ita": "it",
    "kor": "ko",
    "por": "pt",
    "rus": "ru",
    "zho": "zh",
    "chi": "zh",
    "tha": "th",
    "vie": "vi",
    "ara": "ar",
    "und": None,
}
LANGUAGE_NAMES = {
    "en": "English",
    "ja": "Japanese",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "ko": "Korean",
    "pt": "Portuguese",
    "ru": "Russian",
    "zh": "Chinese",
    "zh-CN": "Chinese (Simplified)",
    "zh-TW": "Chinese (Traditional)",
    "th": "Thai",
    "vi": "Vietnamese",
    "ar": "Arabic",
}
LANGUAGE_MARKERS = {
    "default",
    "forced",
    "sdh",
    "cc",
    "hi",
    "sub",
    "subtitle",
    "subs",
    "lyrics",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
ID_RE = re.compile(
    r"\[(?P<provider>tmdbid|tvdbid|imdbid)-(?P<id>[^\]]+)\]", re.IGNORECASE
)
EPISODE_RE = re.compile(
    r"(?i)(?:^|[^A-Z0-9])S(?P<season>\d+)E(?P<episode>\d+)(?:[-.]?E(?P<end>\d+))?"
)
SEASON_RE = re.compile(r"(?i)^(?:season\s*|s)(\d+)$")
ACTIVE_JOB_STATES = ("queued", "running", "terminating")
logger = get_logger("library")


def _path_key(value: str | os.PathLike[str]) -> str:
    """Return a platform-aware stable key for relative filesystem paths."""

    normalized = os.path.normcase(str(value).replace("\\", "/"))
    return normalized.replace("\\", "/").strip("/")


def _top_level_key(value: str | os.PathLike[str]) -> str:
    return _path_key(value).split("/", 1)[0]


class FairMetadataExecutor:
    """Bound metadata root work globally and rotate admission across libraries."""

    def __init__(self, max_workers: int | None = None):
        self.max_workers = max_workers or configured_worker_limit(
            "METADATA_ROOT_WORKERS", 64
        )
        self._condition = threading.Condition()
        self._queues: dict[str, deque] = {}
        self._libraries = deque()
        for index in range(self.max_workers):
            threading.Thread(
                target=self._worker,
                name=f"zenstream-metadata-roots-{index + 1}",
                daemon=True,
            ).start()

    def submit(self, library_id: str, work, /, *args, **kwargs) -> Future:
        future = Future()
        with self._condition:
            queue = self._queues.get(library_id)
            if queue is None:
                queue = self._queues[library_id] = deque()
                self._libraries.append(library_id)
            queue.append((future, work, args, kwargs))
            self._condition.notify()
        return future

    def _worker(self) -> None:
        while True:
            with self._condition:
                while not self._libraries:
                    self._condition.wait()
                library_id = self._libraries.popleft()
                queue = self._queues[library_id]
                future, work, args, kwargs = queue.popleft()
                if queue:
                    self._libraries.append(library_id)
                else:
                    self._queues.pop(library_id, None)
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(work(*args, **kwargs))
            except BaseException as error:
                future.set_exception(error)


metadata_root_executor = FairMetadataExecutor()


class JobTerminated(Exception):
    """Raised when a background worker acknowledges a termination request."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


QUICK_FINGERPRINT_SAMPLE_SIZE = 1024 * 1024
SIDECAR_STAT_TIMEOUT_SECONDS = 10.0


def _quick_fingerprint(path: Path, size: int | None = None) -> tuple[str, int]:
    file_size = int(size if size is not None else path.stat().st_size)
    digest = hashlib.sha256()
    digest.update(f"size:{file_size}".encode("ascii"))
    with path.open("rb") as handle:
        first = handle.read(QUICK_FINGERPRINT_SAMPLE_SIZE)
        digest.update(b"first:")
        digest.update(first)
        bytes_read = len(first)
        if file_size > QUICK_FINGERPRINT_SAMPLE_SIZE:
            handle.seek(
                max(
                    QUICK_FINGERPRINT_SAMPLE_SIZE,
                    file_size - QUICK_FINGERPRINT_SAMPLE_SIZE,
                )
            )
            last = handle.read(QUICK_FINGERPRINT_SAMPLE_SIZE)
            digest.update(b"last:")
            digest.update(last)
            bytes_read += len(last)
    return digest.hexdigest(), bytes_read


def _isolated_stat_worker(request_queue, response_queue) -> None:
    while True:
        request = request_queue.get()
        if request is None:
            return
        request_id, path = request
        try:
            value = os.stat(path)
            response_queue.put(
                (request_id, True, int(value.st_size), int(value.st_mtime_ns))
            )
        except OSError:
            response_queue.put((request_id, False, 0, 0))


class _SidecarStatWorker:
    def __init__(self):
        self._lock = threading.Lock()
        self._request_queue = None
        self._response_queue = None
        self._process = None
        self._request_id = 0

    def _stop(self) -> None:
        process = self._process
        request_queue = self._request_queue
        response_queue = self._response_queue
        self._process = None
        self._request_queue = None
        self._response_queue = None
        if process is not None and process.is_alive():
            process.terminate()
            process.join(1.0)
        if request_queue is not None:
            request_queue.close()
            request_queue.join_thread()
        if response_queue is not None:
            response_queue.close()
            response_queue.join_thread()

    def _start(self) -> bool:
        context = multiprocessing.get_context("spawn")
        request_queue = context.Queue(maxsize=1)
        response_queue = context.Queue(maxsize=1)
        process = context.Process(
            target=_isolated_stat_worker,
            args=(request_queue, response_queue),
            daemon=True,
        )
        try:
            process.start()
        except (OSError, RuntimeError):
            request_queue.close()
            response_queue.close()
            return False
        self._request_queue = request_queue
        self._response_queue = response_queue
        self._process = process
        return True

    def stat(
        self, path: Path, timeout: float = SIDECAR_STAT_TIMEOUT_SECONDS
    ) -> tuple[int, int] | None:
        started = time.monotonic()
        if not self._lock.acquire(timeout=timeout):
            return None
        try:
            if self._process is None or not self._process.is_alive():
                self._stop()
                if not self._start():
                    return None
            self._request_id += 1
            request_id = self._request_id
            try:
                deadline = started + timeout
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop()
                    return None
                self._request_queue.put(
                    (request_id, str(path)), timeout=min(0.25, remaining)
                )
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._stop()
                        return None
                    response = self._response_queue.get(timeout=remaining)
                    if response[0] != request_id:
                        continue
                    return (response[2], response[3]) if response[1] else None
            except (Empty, OSError, EOFError, ValueError):
                self._stop()
                return None
        finally:
            self._lock.release()


_SIDECAR_STAT_WORKER = _SidecarStatWorker()


def _bounded_sidecar_stat(
    path: Path, timeout: float = SIDECAR_STAT_TIMEOUT_SECONDS
) -> tuple[int, int] | None:
    return _SIDECAR_STAT_WORKER.stat(path, timeout)


def normalized_path(path: str) -> str:
    # Japanese Windows commonly renders U+005C as a yen glyph, and copied
    # paths can occasionally contain a literal U+00A5/U+FFE5 instead. Treat
    # both yen variants as Windows separators so valid paths are accepted.
    raw = path.strip().replace("\u00a5", "\\").replace("\uffe5", "\\")
    value = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(value):
        codepoints = " ".join(f"U+{ord(char):04X}" for char in raw)
        logger.warning(
            "library directory rejected raw=%r resolved=%r cwd=%r codepoints=%s",
            path,
            value,
            os.getcwd(),
            codepoints,
        )
        raise ValueError(
            f"Library directory does not exist or is not a directory: {value}"
        )
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


def sidecar_language(path: Path) -> str | None:
    """Extract a real language marker without mistaking release flags for one."""
    for token in reversed(path.stem.replace("_", "-").split(".")):
        candidate = token.strip()
        lowered = candidate.lower()
        if lowered in LANGUAGE_MARKERS:
            continue
        if lowered in LANGUAGE_ALIASES:
            return LANGUAGE_ALIASES[lowered]
        if re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?", candidate):
            base, _, region = candidate.partition("-")
            if len(base) in {2, 3}:
                return f"{base.lower()}-{region.upper()}" if region else base.lower()
    return None


def _sidecar_suffix_tokens(value: str) -> list[str]:
    return [token.strip() for token in re.split(r"[._-]+", value) if token.strip()]


def sidecar_descriptor(
    sidecar_path: str | Path, media_paths: Iterable[str | Path]
) -> str | None:
    sidecar = Path(sidecar_path)
    sidecar_stem = sidecar.stem
    normalized_sidecar = sidecar_stem.casefold()
    matching_stem: str | None = None
    for media_path_value in media_paths:
        media_path = Path(media_path_value)
        if media_path.parent != sidecar.parent:
            continue
        media_stem = media_path.stem
        if not normalized_sidecar.startswith(media_stem.casefold()):
            continue
        remainder = sidecar_stem[len(media_stem) :]
        if remainder and remainder[0] not in ".-_ \t":
            continue
        if matching_stem is None or len(media_stem) > len(matching_stem):
            matching_stem = media_stem
    if matching_stem is None:
        return None

    remainder = sidecar_stem[len(matching_stem) :].lstrip(" ._-\t")
    tokens = _sidecar_suffix_tokens(remainder)
    while tokens and tokens[-1].casefold() in LANGUAGE_MARKERS:
        tokens.pop()
    if tokens:
        candidate = tokens[-1]
        lowered = candidate.casefold()
        if lowered in LANGUAGE_ALIASES or re.fullmatch(
            r"[a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?", candidate
        ):
            tokens.pop()
    while tokens and tokens[-1].casefold() in LANGUAGE_MARKERS:
        tokens.pop()
    descriptor = re.sub(r"\s+", " ", " ".join(tokens)).strip(" ._-\t")
    return descriptor or None


def sidecar_display_title(
    sidecar_path: str | Path,
    language: str | None,
    role: str,
    media_paths: Iterable[str | Path],
) -> str:
    descriptor = sidecar_descriptor(sidecar_path, media_paths)
    resolved_language = language_name(language, role)
    return f"{descriptor} - {resolved_language}" if descriptor else resolved_language


def language_name(language: str | None, role: str) -> str:
    if role == "lyrics":
        return "Lyrics"
    return LANGUAGE_NAMES.get(language or "", "Subtitle")


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
            "id": row[0],
            "name": row[1],
            "type": row[2],
            "directory": row[3],
            "watchEnabled": bool(row[4]),
            "scanIntervalMinutes": row[5],
            "scanState": row[6],
            "scanError": row[7],
            "lastScanStartedAt": row[8],
            "lastScanFinishedAt": row[9],
            "createdAt": row[10],
            "updatedAt": row[11],
        }

    def get(self, library_id: str) -> dict | None:
        rows = self.db.execute(
            "SELECT id,name,type,directory,watch_enabled,scan_interval_minutes,scan_state,scan_error,last_scan_started_at,last_scan_finished_at,created_at,updated_at FROM libraries WHERE id=?",
            (library_id,),
        )
        return self._row(rows[0]) if rows else None

    def sources(self, library_id: str) -> list[str]:
        return [
            row[0]
            for row in self.db.execute(
                "SELECT source_library_id FROM library_sources WHERE library_id=? ORDER BY source_library_id",
                (library_id,),
            )
        ]

    def create(
        self,
        name: str,
        library_type: str,
        directory: str | None,
        watch_enabled: bool = True,
        interval: int = 1440,
        source_ids: Iterable[str] = (),
    ) -> dict:
        name = name.strip()
        if not name or library_type not in LIBRARY_TYPES:
            raise ValueError("A name and supported library type are required.")
        if library_type == "collection":
            directory = None
            source_ids = list(dict.fromkeys(source_ids))
            if not source_ids:
                raise ValueError(
                    "A Collection library needs at least one Movie or TV source library."
                )
        else:
            if not directory:
                raise ValueError("A directory is required for physical libraries.")
            directory = normalized_path(directory)
        interval = max(15, min(43200, int(interval or 1440)))
        library_id = new_id()
        timestamp = now()
        with self.db.transaction() as cursor:
            cursor.execute(
                "SELECT 1 FROM libraries WHERE name=? COLLATE NOCASE", (name,)
            )
            if cursor.fetchone():
                raise ValueError("A library with that name already exists.")
            if directory:
                cursor.execute(
                    "SELECT 1 FROM libraries WHERE directory=?", (directory,)
                )
                if cursor.fetchone():
                    raise ValueError("A library already uses that directory.")
            cursor.execute(
                "INSERT INTO libraries(id,name,type,directory,watch_enabled,scan_interval_minutes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    library_id,
                    name,
                    library_type,
                    directory,
                    int(watch_enabled),
                    interval,
                    timestamp,
                    timestamp,
                ),
            )
            for source_id in source_ids:
                cursor.execute("SELECT type FROM libraries WHERE id=?", (source_id,))
                source = cursor.fetchone()
                if not source or source[0] not in {"movies", "tv_series"}:
                    raise ValueError(
                        "Collections can source only Movie and TV libraries."
                    )
                cursor.execute(
                    "INSERT INTO library_sources(library_id,source_library_id) VALUES(?,?)",
                    (library_id, source_id),
                )
        return self.get(library_id)  # type: ignore[return-value]

    def update(self, library_id: str, values: dict) -> dict:
        current = self.get(library_id)
        if not current:
            raise KeyError("Library not found")
        name = str(values.get("name", current["name"])).strip()
        interval = max(
            15,
            min(
                43200,
                int(
                    values.get("scanIntervalMinutes", current["scanIntervalMinutes"])
                    or 1440
                ),
            ),
        )
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
                cursor.execute(
                    "DELETE FROM library_sources WHERE library_id=?", (library_id,)
                )
                for source_id in source_ids:
                    cursor.execute(
                        "SELECT type FROM libraries WHERE id=?", (source_id,)
                    )
                    source = cursor.fetchone()
                    if not source or source[0] not in {"movies", "tv_series"}:
                        raise ValueError(
                            "Collections can source only Movie and TV libraries."
                        )
                    cursor.execute(
                        "INSERT INTO library_sources(library_id,source_library_id) VALUES(?,?)",
                        (library_id, source_id),
                    )
        return self.get(library_id)  # type: ignore[return-value]

    def delete(self, library_id: str) -> bool:
        from app.library_cleanup import cleanup_library

        return cleanup_library(self.db, library_id)

    def set_scan_state(
        self,
        library_id: str,
        state: str,
        error: str | None = None,
        started: str | None = None,
        finished: str | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE libraries SET scan_state=?,scan_error=?,last_scan_started_at=COALESCE(?,last_scan_started_at),last_scan_finished_at=COALESCE(?,last_scan_finished_at),updated_at=? WHERE id=?",
            (state, error, started, finished, now(), library_id),
        )

    def create_job(self, library_id: str, kind: str) -> dict:
        job_id = new_id()
        timestamp = now()
        self.db.execute(
            "INSERT INTO library_jobs(id,library_id,kind,created_at) VALUES(?,?,?,?)",
            (job_id, library_id, kind, timestamp),
        )
        return self.job(job_id)  # type: ignore[return-value]

    def job(self, job_id: str) -> dict | None:
        rows = self.db.execute(
            "SELECT id,library_id,kind,state,progress_current,progress_total,message,error,error_details,created_at,started_at,finished_at FROM library_jobs WHERE id=?",
            (job_id,),
        )
        if not rows:
            return None
        row = rows[0]
        has_queue = bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrichment_queue'"
            )
        )
        pending_repairs = (
            self.db.execute(
                "SELECT COUNT(*) FROM enrichment_queue WHERE library_id=? AND state IN ('queued','claimed','retry')",
                (row[1],),
            )
            if has_queue
            else []
        )
        failed_repairs = (
            self.db.execute(
                "SELECT COUNT(*) FROM enrichment_queue WHERE library_id=? AND state='failed'",
                (row[1],),
            )
            if has_queue
            else []
        )
        return {
            "id": row[0],
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
            "warningCount": int(failed_repairs[0][0]) if failed_repairs else 0,
            "repairPending": bool(pending_repairs and pending_repairs[0][0]),
        }

    def jobs(self, library_id: str) -> list[dict]:
        return [
            self.job(row[0])
            for row in self.db.execute(
                "SELECT id FROM library_jobs WHERE library_id=? ORDER BY created_at DESC LIMIT 50",
                (library_id,),
            )
            if self.job(row[0])
        ]  # type: ignore[list-item]

    def update_job(self, job_id: str, **values) -> None:
        allowed = {
            "state",
            "progress_current",
            "progress_total",
            "message",
            "error",
            "error_details",
            "started_at",
            "finished_at",
        }
        updates = [(key, value) for key, value in values.items() if key in allowed]
        if not updates:
            return
        fields = ",".join(f"{key}=?" for key, _ in updates)
        self.db.execute(
            f"UPDATE library_jobs SET {fields} WHERE id=?",
            [value for _, value in updates] + [job_id],
        )


class LibraryScanner:
    def __init__(self, store: LibraryStore | None = None):
        self.store = store or LibraryStore()
        self.db = self.store.db
        self._scan_seen_ids: set[str] = set()
        self._scan_created_ids: list[str] = []
        self._scan_delta = {
            "added": set(),
            "changed": set(),
            "content_changed": set(),
            "unchanged": set(),
            "removed": set(),
        }
        self._scan_provider_identity_changed: set[str] = set()
        self._scan_rejected_ids: set[str] = set()
        self._scan_reconciled_ids: set[str] = set()
        self._scan_deferred_roots: set[str] = set()
        self._scan_access_errors: set[Path] = set()
        self._scan_refresh_root_ids: set[str] = set()
        self._scan_complete = False
        self._stage_lock = threading.RLock()
        self._stage = "idle"
        self._stage_context: dict = {}
        self._stage_started = time.monotonic()
        self._last_stage_persisted_at = 0.0
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._publication_lock = threading.Lock()
        self._pending_publication_roots: dict[str, None] = {}
        self._last_publication_at = 0.0

    def _set_stage(
        self, job_id: str, stage: str, *, persist: bool = True, **context
    ) -> None:
        with self._stage_lock:
            self._stage = stage
            self._stage_context = context
            self._stage_started = time.monotonic()
        logger.info(
            "library scan stage start job_id=%s stage=%s context=%s",
            job_id,
            stage,
            context,
        )
        if persist:
            self.store.update_job(job_id, message=stage)
            self._last_stage_persisted_at = time.monotonic()

    def _start_heartbeat(self, library_id: str, job_id: str) -> None:
        self._heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            while not self._heartbeat_stop.wait(15):
                with self._stage_lock:
                    stage = self._stage
                    context = dict(self._stage_context)
                    elapsed = time.monotonic() - self._stage_started
                context_text = " ".join(
                    f"{key}={value}"
                    for key, value in context.items()
                    if value is not None
                )
                message = f"Still working: {stage}"
                if context_text:
                    message += f" [{context_text}]"
                message += f" ({elapsed:.0f}s)"
                logger.warning(
                    "library scan heartbeat library_id=%s job_id=%s stage=%s elapsed_seconds=%.1f",
                    library_id,
                    job_id,
                    stage,
                    elapsed,
                )
                self.store.update_job(job_id, message=message)

        self._heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"zenstream-scan-heartbeat-{job_id[:8]}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_stop:
            self._heartbeat_stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)
        self._heartbeat_stop = None
        self._heartbeat_thread = None

    def scan(
        self,
        library_id: str,
        job_id: str,
        should_terminate: Callable[[], bool] | None = None,
        targets: set[str] | None = None,
    ) -> None:
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
        self.store.update_job(
            job_id, state="running", started_at=started, message="Discovering media"
        )
        self._start_heartbeat(library_id, job_id)
        self._scan_seen_ids = set()
        self._scan_created_ids = []
        self._scan_delta = {
            "added": set(),
            "changed": set(),
            "content_changed": set(),
            "unchanged": set(),
            "removed": set(),
        }
        self._scan_provider_identity_changed = set()
        self._scan_rejected_ids = set()
        self._scan_reconciled_ids = set()
        self._scan_deferred_roots = set()
        self._scan_access_errors = set()
        self._scan_refresh_root_ids = set()
        self._last_stage_persisted_at = 0.0
        self._scan_complete = False
        try:
            self._check_termination(should_terminate)
            self._set_stage(
                job_id,
                f"Discovering {library['type']} roots",
                root=str(root),
                targets=sorted(targets) if targets else None,
            )
            if library["type"] == "movies":
                count = self._scan_movies(
                    library_id, root, job_id, should_terminate, targets
                )
            elif library["type"] == "tv_series":
                count = self._scan_series(
                    library_id,
                    root,
                    job_id,
                    should_terminate,
                    resolve_immediately=True,
                    targets=targets,
                )
            else:
                count = self._scan_music(
                    library_id, root, job_id, should_terminate, targets
                )
            self._scan_complete = True
            if library["type"] == "music":
                self._set_stage(job_id, "Resolving new or changed metadata")
                self._resolve_and_seed(
                    library_id, library["type"], job_id, should_terminate
                )
                self._set_stage(job_id, "Populating changed metadata locales")
                self._fetch_seen_locales(should_terminate)
            self._set_stage(job_id, "Reconciling moved entities")
            self._reconcile_moved_entities(library_id, root, targets=targets)
            self._set_stage(job_id, "Pruning entities without playable media")
            concurrent_reconcile = targets is None and bool(
                self.db.execute(
                    "SELECT 1 FROM library_jobs WHERE library_id=? AND kind='reconcile' AND state IN ('queued','running','terminating') LIMIT 1",
                    (library_id,),
                )
            )
            # A full traversal cannot safely treat its own snapshot as
            # authoritative while a targeted reconcile is mutating the same
            # inventory. Leave cleanup to the root-scoped reconcile instead of
            # deleting newer rows, then let the next complete scan repair any
            # roots that were not part of that target.
            rejected = (
                set()
                if concurrent_reconcile
                else self._prune_rejected_entities(targets=targets)
            )
            self._set_stage(job_id, "Pruning missing entities")
            missing = (
                set()
                if concurrent_reconcile
                else self._prune_missing_entities(library_id, root, targets=targets)
            )
            self._set_stage(job_id, "Refreshing catalog read model")
            removed = rejected | missing
            self._flush_publications()
            self._refresh_catalog_after_cleanup(library_id)
            self._set_stage(job_id, "Pruning local artwork cache")
            LocalArtworkCache(self.db).prune()
            from app.trickplay import TrickplayStore

            if self._scan_delta["content_changed"] and TrickplayStore(
                self.db
            ).queue_pending(library_id):
                self._set_stage(job_id, "Queueing trickplay extraction")
                from app.jobs import scheduler

                scheduler.enqueue_trickplay_extraction()
            from app.intro_outro import IntroOutroStore

            intro_outro = IntroOutroStore(self.db)
            if (
                self._scan_delta["content_changed"]
                and intro_outro.settings()["scanOnAdded"]
                and intro_outro.queue_pending(library_id)
            ):
                self._set_stage(job_id, "Queueing intro/outro detection")
                from app.jobs import scheduler

                scheduler.enqueue_intro_outro_detection()
            finished = now()
            self.store.update_job(
                job_id,
                state="completed",
                progress_current=count,
                progress_total=count,
                finished_at=finished,
                message=f"Indexed {count} entries",
            )
            self.store.set_scan_state(library_id, "ready", finished=finished)
            if removed:
                self._refresh_dependent_collections(library_id)
            self.db.schedule_maintenance(scan_complete=True)
        except JobTerminated:
            self._scan_complete = False
            # A terminated traversal is not authoritative. Remove only rows
            # created by this attempt; previously indexed inventory remains.
            self._remove_created_entities()
            finished = now()
            self.store.update_job(
                job_id,
                state="terminated",
                message="Terminated by administrator",
                error=None,
                finished_at=finished,
            )
            self.store.set_scan_state(library_id, "ready", finished=finished)
        except Exception as error:
            self._scan_complete = False
            self._remove_created_entities()
            details = {
                "libraryId": library_id,
                "jobId": job_id,
                "exception": type(error).__name__,
                "traceback": traceback.format_exc(),
            }
            summary = f"Library scan failed for '{library.get('name', library_id)}': {type(error).__name__}: {error}"
            logger.exception(
                "library scan failed library_id=%s job_id=%s", library_id, job_id
            )
            self.store.update_job(
                job_id,
                state="failed",
                error=summary,
                error_details=json.dumps(details),
                finished_at=now(),
            )
            self.store.set_scan_state(
                library_id, "error", error=summary, finished=now()
            )
            raise
        finally:
            self._stop_heartbeat()

    @staticmethod
    def _check_termination(should_terminate: Callable[[], bool]) -> None:
        if should_terminate():
            raise JobTerminated()

    def _entity(
        self,
        library_id: str,
        parent_id: str | None,
        entity_type: str,
        path: str | None,
        **numbers,
    ) -> str:
        timestamp = now()
        fields = {
            "season_number": None,
            "episode_number": None,
            "episode_end_number": None,
            "disc_number": None,
            "track_number": None,
        }
        fields.update(numbers)
        existing = self.db.execute(
            "SELECT id FROM library_entities WHERE library_id=? AND entity_type=? AND relative_path IS ?",
            (library_id, entity_type, path),
        )
        if existing:
            entity_id = existing[0][0]
            self._scan_delta["unchanged"].add(entity_id)
            before = self.db.execute(
                "SELECT parent_id,season_number,episode_number,episode_end_number,disc_number,track_number FROM library_entities WHERE id=?",
                (entity_id,),
            )
            next_values = (
                parent_id,
                fields["season_number"],
                fields["episode_number"],
                fields["episode_end_number"],
                fields["disc_number"],
                fields["track_number"],
            )
            if before and tuple(before[0]) != next_values:
                self.db.execute(
                    "UPDATE library_entities SET parent_id=?,season_number=?,episode_number=?,episode_end_number=?,disc_number=?,track_number=?,updated_at=? WHERE id=?",
                    (*next_values, timestamp, entity_id),
                )
                self._mark_changed(entity_id)
        else:
            entity_id = new_id()
            self.db.execute(
                "INSERT INTO library_entities(id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,disc_number,track_number,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    library_id,
                    parent_id,
                    entity_type,
                    path,
                    fields["season_number"],
                    fields["episode_number"],
                    fields["episode_end_number"],
                    fields["disc_number"],
                    fields["track_number"],
                    timestamp,
                    timestamp,
                ),
            )
            self._scan_created_ids.append(entity_id)
            self._scan_delta["added"].add(entity_id)
        self._scan_seen_ids.add(entity_id)
        return entity_id

    def _mark_changed(self, entity_id: str, *, content_changed: bool = False) -> None:
        self._scan_delta["changed"].add(entity_id)
        self._scan_delta["unchanged"].discard(entity_id)
        if content_changed:
            self._scan_delta["content_changed"].add(entity_id)

    def _metadata_candidates(self) -> set[str]:
        return (
            set(self._scan_delta["added"])
            | set(self._scan_delta["content_changed"])
            | set(self._scan_provider_identity_changed)
        )

    def _prune_missing_entities(
        self,
        library_id: str,
        root: Path | None = None,
        *,
        targets: set[str] | None = None,
    ) -> set[str]:
        if not self._scan_complete:
            return set()
        legacy_without_library_root = False
        if root is None:
            try:
                rows = self.db.execute(
                    "SELECT directory FROM libraries WHERE id=?", (library_id,)
                )
                root = Path(rows[0][0]) if rows else None
            except Exception:
                root = None
                legacy_without_library_root = True
        rows = self.db.execute(
            "SELECT id,relative_path FROM library_entities WHERE library_id=?",
            (library_id,),
        )
        missing = []
        normalized_targets = {_top_level_key(target) for target in (targets or set())}
        for entity_id, relative_path in rows:
            if entity_id in self._scan_seen_ids:
                continue
            if normalized_targets:
                normalized_path = _path_key(relative_path or "")
                if not any(
                    normalized_path == target
                    or normalized_path.startswith(target + "/")
                    for target in normalized_targets
                ):
                    continue
            if _top_level_key(relative_path or "") in self._scan_deferred_roots:
                continue
            # A complete traversal is required before pruning. Existing paths
            # that were not classifiable are deliberately retained.
            if legacy_without_library_root:
                missing.append(entity_id)
                continue
            if root is None or relative_path is None:
                continue
            try:
                candidate = root / relative_path
                if candidate.exists() or candidate.is_symlink():
                    continue
            except (OSError, ValueError):
                continue
            missing.append(entity_id)
        if not missing:
            return set()
        from app.library_cleanup import cleanup_entities

        closure = self._entity_closure(missing)
        cleanup_entities(self.db, missing)
        self._delete_catalog_rows(closure)
        self._scan_delta["removed"].update(closure)
        return set(closure)

    def _reject_existing_entity(
        self, library_id: str, entity_type: str, relative_path: str
    ) -> None:
        rows = self.db.execute(
            "SELECT id FROM library_entities WHERE library_id=? AND entity_type=? AND relative_path=?",
            (library_id, entity_type, relative_path),
        )
        if rows:
            self._scan_rejected_ids.add(rows[0][0])

    def _defer_root(self, relative_path: str, reason: str) -> None:
        root = _top_level_key(relative_path)
        if not root:
            return
        self._scan_deferred_roots.add(root)
        logger.warning(
            "library scan deferred inaccessible root root=%s reason=%s",
            relative_path,
            reason,
        )

    def _record_access_error(self, path: Path) -> None:
        self._scan_access_errors.add(path)

    def _root_has_access_error(self, root: Path) -> bool:
        root_key = _path_key(root.resolve(strict=False))
        return any(
            (candidate_key := _path_key(path.resolve(strict=False))) == root_key
            or candidate_key.startswith(root_key + "/")
            for path in self._scan_access_errors
        )

    def _entity_closure(self, entity_ids: Iterable[str]) -> list[str]:
        roots = list(dict.fromkeys(entity_ids))
        if not roots:
            return []
        placeholders = ",".join("?" for _ in roots)
        return [
            row[0]
            for row in self.db.execute(
                "WITH RECURSIVE removed(id) AS ("
                f"SELECT id FROM library_entities WHERE id IN ({placeholders}) "
                "UNION ALL SELECT e.id FROM library_entities e JOIN removed r ON e.parent_id=r.id) "
                "SELECT DISTINCT id FROM removed",
                roots,
            )
        ]

    def _delete_catalog_rows(self, entity_ids: Iterable[str]) -> None:
        ids = list(dict.fromkeys(entity_ids))
        if not ids:
            return
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        keyed_tables = {
            "catalog_entity_summary": "entity_id",
            "catalog_item_projection": "entity_id",
            "catalog_user_summary": "entity_id",
            "catalog_item_genres": "entity_id",
            "catalog_search_grams": "entity_id",
            "catalog_collection_summary": "collection_entity_id",
        }
        for offset in range(0, len(ids), 300):
            batch = ids[offset : offset + 300]
            placeholders = ",".join("?" for _ in batch)
            for table, column in keyed_tables.items():
                if table in tables:
                    self.db.execute(
                        f"DELETE FROM {table} WHERE {column} IN ({placeholders})", batch
                    )

    def _prune_rejected_entities(self, targets: set[str] | None = None) -> set[str]:
        rejected = self._scan_rejected_ids - self._scan_reconciled_ids
        if targets is not None and rejected:
            rows = self.db.execute(
                "SELECT id,relative_path FROM library_entities WHERE id IN (%s)"
                % ",".join("?" for _ in rejected),
                list(rejected),
            )
            rejected = {
                entity_id
                for entity_id, relative_path in rows
                if relative_path
                and Path(relative_path).parts
                and _top_level_key(relative_path)
                in {_top_level_key(target) for target in targets}
                and _top_level_key(relative_path)
                not in self._scan_deferred_roots
            }
        elif rejected:
            rows = self.db.execute(
                "SELECT id,relative_path FROM library_entities WHERE id IN (%s)"
                % ",".join("?" for _ in rejected),
                list(rejected),
            )
            rejected = {
                entity_id
                for entity_id, relative_path in rows
                if _top_level_key(relative_path or "")
                not in self._scan_deferred_roots
            }
        closure = self._entity_closure(rejected)
        if not closure:
            return set()
        from app.library_cleanup import cleanup_entities

        cleanup_entities(self.db, list(rejected))
        self._delete_catalog_rows(closure)
        removed = set(closure)
        self._scan_delta["removed"].update(removed)
        self._scan_created_ids = [
            entity_id
            for entity_id in self._scan_created_ids
            if entity_id not in removed
        ]
        self._scan_seen_ids.difference_update(removed)
        self._scan_refresh_root_ids.difference_update(removed)
        return removed

    def _refresh_catalog_after_cleanup(self, library_id: str) -> None:
        from app.catalog_read_model import CatalogReadModel

        model = CatalogReadModel(self.db)
        roots = sorted(self._scan_refresh_root_ids)
        for offset in range(0, len(roots), 300):
            model.refresh_roots(roots[offset : offset + 300])
        if len(roots) != 1 or self._scan_delta.get("removed"):
            model.refresh_roots([], affected_library_ids=[library_id])

    def _publish_root(self, root_id: str) -> None:
        with self._publication_lock:
            self._pending_publication_roots[root_id] = None
            if time.monotonic() - self._last_publication_at < 2.0:
                return
            self._flush_publications_locked()

    def _flush_publications(self) -> None:
        with self._publication_lock:
            self._flush_publications_locked()

    def _flush_publications_locked(self) -> None:
        if not self._pending_publication_roots:
            return
        from app.catalog_read_model import CatalogReadModel
        from app.foreground import active_requests

        if active_requests():
            time.sleep(0.05)
        roots = list(self._pending_publication_roots)
        model = CatalogReadModel(self.db)
        try:
            for offset in range(0, len(roots), 300):
                model.refresh_roots(roots[offset : offset + 300])
            if len(roots) > 1:
                placeholders = ",".join("?" for _ in roots)
                affected_libraries = [
                    row[0]
                    for row in self.db.execute(
                        f"SELECT DISTINCT library_id FROM library_entities WHERE id IN ({placeholders})",
                        roots,
                    )
                ]
                model.refresh_roots([], affected_library_ids=affected_libraries)
        except Exception:
            logger.exception("catalog publication failed roots=%s", len(roots))
            return
        for root_id in roots:
            self._pending_publication_roots.pop(root_id, None)
        self._last_publication_at = time.monotonic()
        if not active_requests():
            self.db.maintain_wal()

    def _refresh_dependent_collections(self, library_id: str) -> None:
        """Re-evaluate affected derived collections without provider enumeration."""
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"library_sources", "collection_members"} <= tables:
            return
        dependent_ids = [
            row[0]
            for row in self.db.execute(
                "SELECT library_id FROM library_sources WHERE source_library_id=?",
                (library_id,),
            )
        ]
        if not dependent_ids:
            return
        collection_ids = [
            row[0]
            for row in self.db.execute(
                "SELECT id FROM library_entities WHERE library_id IN ({}) AND entity_type='collection'".format(
                    ",".join("?" for _ in dependent_ids)
                ),
                dependent_ids,
            )
        ]
        if not collection_ids:
            return
        from app.library_cleanup import cleanup_entities

        empty = [
            collection_id
            for collection_id in collection_ids
            if not self.db.execute(
                "SELECT 1 FROM collection_members WHERE collection_entity_id=? LIMIT 1",
                (collection_id,),
            )
        ]
        if empty:
            closure = self._entity_closure(empty)
            cleanup_entities(self.db, empty)
            self._delete_catalog_rows(closure)
            self._scan_delta["removed"].update(closure)
        surviving = [value for value in collection_ids if value not in set(empty)]
        if surviving or empty:
            from app.catalog_read_model import CatalogReadModel

            model = CatalogReadModel(self.db)
            for offset in range(0, len(surviving), 300):
                model.refresh_roots(surviving[offset : offset + 300])
            if empty or len(surviving) != 1:
                model.refresh_roots([], affected_library_ids=dependent_ids)

    def _entity_fingerprint(self, entity_id: str) -> str | None:
        if "quick_fingerprint" not in {
            row[1] for row in self.db.execute("PRAGMA table_info(media_files)")
        }:
            return None
        rows = self.db.execute(
            "SELECT role,quick_fingerprint FROM media_files WHERE entity_id=? AND role='media' ORDER BY role,relative_path",
            (entity_id,),
        )
        if not rows or any(not row[1] for row in rows):
            return None
        return "|".join(f"{role}:{fingerprint}" for role, fingerprint in rows)

    def _reconcile_moved_entities(
        self,
        library_id: str,
        root: Path,
        targets: set[str] | None = None,
    ) -> None:
        """Match newly discovered leaf entities to vanished paths by unique hash."""
        if "quick_fingerprint" not in {
            row[1] for row in self.db.execute("PRAGMA table_info(media_files)")
        }:
            return
        leaf_types = {"movie", "episode", "track", "release"}
        new_ids = [
            entity_id
            for entity_id in self._scan_created_ids
            if entity_id in self._scan_seen_ids
        ]
        old_rows = self.db.execute(
            "SELECT id,entity_type,relative_path FROM library_entities WHERE library_id=?",
            (library_id,),
        )
        normalized_targets = {_top_level_key(target) for target in (targets or set())}
        old_ids = [
            row[0]
            for row in old_rows
            if row[0] not in self._scan_seen_ids
            and row[1] in leaf_types
            and _top_level_key(row[2] or "") not in self._scan_deferred_roots
            and (
                targets is None
                or bool(row[2])
                and Path(row[2]).parts
                and _top_level_key(row[2]) in normalized_targets
            )
        ]
        old_by_key: dict[tuple[str, str], list[str]] = {}
        new_by_key: dict[tuple[str, str], list[str]] = {}
        for entity_id in old_ids:
            row = self.db.execute(
                "SELECT entity_type FROM library_entities WHERE id=?", (entity_id,)
            )
            fingerprint = self._entity_fingerprint(entity_id)
            if row and fingerprint:
                old_by_key.setdefault((row[0][0], fingerprint), []).append(entity_id)
        for entity_id in new_ids:
            row = self.db.execute(
                "SELECT entity_type FROM library_entities WHERE id=?", (entity_id,)
            )
            fingerprint = self._entity_fingerprint(entity_id)
            if row and fingerprint:
                new_by_key.setdefault((row[0][0], fingerprint), []).append(entity_id)

        for key, old_matches in old_by_key.items():
            new_matches = new_by_key.get(key, [])
            if len(old_matches) != 1 or len(new_matches) != 1:
                continue
            old_id, new_id = old_matches[0], new_matches[0]
            old = self.db.execute(
                "SELECT relative_path,parent_id,season_number,episode_number,episode_end_number,disc_number,track_number FROM library_entities WHERE id=?",
                (old_id,),
            )
            replacement = self.db.execute(
                "SELECT relative_path,parent_id,season_number,episode_number,episode_end_number,disc_number,track_number FROM library_entities WHERE id=?",
                (new_id,),
            )
            if not old or not replacement:
                continue
            old_values = old[0]
            new_values = replacement[0]
            try:
                with self.db.transaction() as cursor:
                    # The stable entity cannot claim the renamed path while
                    # the newly indexed replacement still owns its unique
                    # (library, type, path) tuple.  Vacate it transactionally
                    # before transferring the stable identity.
                    cursor.execute(
                        "UPDATE library_entities SET relative_path=? WHERE id=?",
                        (f"__zenstream_move__/{new_id}", new_id),
                    )
                    cursor.execute(
                        "UPDATE library_entities SET relative_path=?,parent_id=?,season_number=?,episode_number=?,episode_end_number=?,disc_number=?,track_number=?,updated_at=? WHERE id=?",
                        (*new_values, now(), old_id),
                    )
                    tables = {
                        row[0]
                        for row in cursor.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    old_files = (
                        list(
                            cursor.execute(
                                "SELECT id,relative_path,role,language,flags,size,modified_ns,quick_fingerprint FROM media_files WHERE entity_id=? ORDER BY role,relative_path",
                                (old_id,),
                            )
                        )
                        if "media_files" in tables
                        else []
                    )
                    new_files = (
                        list(
                            cursor.execute(
                                "SELECT id,relative_path,role,language,flags,size,modified_ns,quick_fingerprint FROM media_files WHERE entity_id=? ORDER BY role,relative_path",
                                (new_id,),
                            )
                        )
                        if "media_files" in tables
                        else []
                    )
                    # Keep media-file IDs where the moved entity has the same
                    # role inventory. This also keeps existing probe sources
                    # attached to the stable file identity.
                    paired = min(len(old_files), len(new_files))
                    for index in range(paired):
                        old_file, new_file = old_files[index], new_files[index]
                        if old_file[2] != new_file[2]:
                            continue
                        cursor.execute(
                            "UPDATE media_files SET relative_path=?,role=?,language=?,flags=?,size=?,modified_ns=?,quick_fingerprint=? WHERE id=?",
                            (*new_file[1:8], old_file[0]),
                        )
                        if "media_sources" in tables:
                            cursor.execute(
                                "DELETE FROM media_sources WHERE media_file_id=?",
                                (new_file[0],),
                            )
                        cursor.execute(
                            "DELETE FROM media_files WHERE id=?", (new_file[0],)
                        )
                    for old_file in old_files[paired:]:
                        cursor.execute(
                            "DELETE FROM media_files WHERE id=?", (old_file[0],)
                        )
                    for new_file in new_files[paired:]:
                        cursor.execute(
                            "UPDATE media_files SET entity_id=? WHERE id=?",
                            (old_id, new_file[0]),
                        )
                    if "media_sources" in tables:
                        cursor.execute(
                            "UPDATE media_sources SET entity_id=? WHERE entity_id=?",
                            (old_id, new_id),
                        )
                    if "collection_members" in tables:
                        cursor.execute(
                            "UPDATE collection_members SET source_entity_id=? WHERE source_entity_id=?",
                            (old_id, new_id),
                        )
                    if "user_item_state" in tables:
                        cursor.execute(
                            "DELETE FROM user_item_state WHERE entity_id=? AND user_id IN (SELECT user_id FROM user_item_state WHERE entity_id=?)",
                            (new_id, old_id),
                        )
                        cursor.execute(
                            "UPDATE user_item_state SET entity_id=? WHERE entity_id=?",
                            (old_id, new_id),
                        )
                    if "catalog_search" in tables:
                        cursor.execute(
                            "UPDATE catalog_search SET entity_id=? WHERE entity_id=?",
                            (old_id, new_id),
                        )
                    cursor.execute(
                        "DELETE FROM entity_provider_ids WHERE entity_id=?", (new_id,)
                    )
                    if "collection_members" in tables:
                        cursor.execute(
                            "DELETE FROM collection_members WHERE collection_entity_id=? OR source_entity_id=?",
                            (new_id, new_id),
                        )
                    cursor.execute("DELETE FROM library_entities WHERE id=?", (new_id,))
            except Exception:
                logger.exception(
                    "failed to preserve moved entity old_id=%s new_id=%s",
                    old_id,
                    new_id,
                )
                continue
            self._scan_seen_ids.add(old_id)
            self._scan_reconciled_ids.add(old_id)
            if new_id in self._scan_refresh_root_ids:
                self._scan_refresh_root_ids.discard(new_id)
                self._scan_refresh_root_ids.add(old_id)
            self._scan_delta["added"].discard(new_id)
            self._scan_delta["changed"].add(old_id)
            self._scan_delta["unchanged"].discard(old_id)
            self._scan_created_ids = [
                value for value in self._scan_created_ids if value != new_id
            ]

    def _remove_created_entities(self) -> None:
        if not self._scan_created_ids:
            return
        from app.library_cleanup import cleanup_entities

        cleanup_entities(self.db, list(reversed(self._scan_created_ids)))
        self._scan_created_ids = []

    def _needs_metadata(self, entity_id: str) -> bool:
        row = self.db.execute(
            "SELECT match_status FROM library_entities WHERE id=?", (entity_id,)
        )
        if not row or row[0][0] in {"unresolved", "failed"}:
            return True
        return not bool(
            self.db.execute(
                "SELECT 1 FROM entity_provider_ids WHERE entity_id=? LIMIT 1",
                (entity_id,),
            )
        )

    def _fetch_seen_locales(self, should_terminate: Callable[[], bool]) -> None:
        """Populate configured locales only for inventory changes from this scan."""
        from app.metadata_services import MetadataIngestService, metadata_task_results
        from app.providers import MetadataService

        ingest = MetadataIngestService(MetadataService())
        metadata_candidates = self._metadata_candidates()
        rows = (
            self.db.execute(
                "SELECT e.id,e.entity_type,p.provider,p.identifier_type,p.provider_id FROM library_entities e JOIN entity_provider_ids p ON p.entity_id=e.id WHERE e.id IN ({})".format(
                    ",".join("?" * len(metadata_candidates))
                ),
                list(metadata_candidates),
            )
            if metadata_candidates
            else []
        )
        locales = ingest.locales()
        tasks = {}
        for entity_id, entity_type, provider, identifier_type, provider_id in rows:
            if provider not in {"tmdb", "tvdb", "musicbrainz"}:
                continue
            for locale in locales:
                cached = ingest.metadata_service.cache.get(
                    provider, entity_type, str(provider_id), locale
                )
                if cached:
                    # A normal library scan is inventory-driven. Do not
                    # refetch an already cached locale, but do replay its
                    # projection and enrichment so a newly attached entity or
                    # interrupted asset task becomes visible and repairable.
                    cached.pop("_stale", None)
                    ingest.ingest_document(
                        provider,
                        entity_type,
                        str(provider_id),
                        locale,
                        cached,
                    )
                    continue
                tasks.setdefault((provider, entity_type, str(provider_id)), []).append(
                    locale
                )

        def fetch_locales(task):
            (provider, entity_type, provider_id), missing = task
            return ingest.ingest_locales(
                provider, entity_type, provider_id, missing, force=False
            )

        for task, _result, error in metadata_task_results(
            sorted(tasks.items()), fetch_locales, should_terminate
        ):
            self._check_termination(should_terminate)
            if error is not None:
                (provider, entity_type, provider_id), missing = task
                logger.warning(
                    "rescan localized metadata failed entity_type=%s provider=%s provider_id=%s locales=%s: %s",
                    entity_type,
                    provider,
                    provider_id,
                    missing,
                    error,
                )

    def _ids(self, entity_id: str, values: Iterable[tuple[str, str, str]]) -> None:
        from app.providers import PRIMARY_PROVIDER_BY_ENTITY

        row = self.db.execute(
            "SELECT entity_type FROM library_entities WHERE id=?", (entity_id,)
        )
        entity_type = row[0][0] if row else ""
        primary_provider = PRIMARY_PROVIDER_BY_ENTITY.get(entity_type)
        found = False
        for provider, identifier_type, value in values:
            if provider in {"tmdb", "tvdb"} and entity_type in {"movie", "series"}:
                identifier_type = "movie" if entity_type == "movie" else "series"
            self.db.execute(
                "INSERT OR REPLACE INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES(?,?,?,?,?)",
                (
                    entity_id,
                    provider,
                    identifier_type,
                    value,
                    int(provider == primary_provider),
                ),
            )
            found = True
        if found:
            self.db.execute(
                "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='explicit_id',updated_at=? WHERE id=?",
                (now(), entity_id),
            )

    def _replace_ids(
        self, entity_id: str, values: Iterable[tuple[str, str, str]]
    ) -> None:
        """Replace scanner-discovered IDs, preserving the merge-style _ids API."""
        from app.providers import PRIMARY_PROVIDER_BY_ENTITY

        row = self.db.execute(
            "SELECT entity_type FROM library_entities WHERE id=?", (entity_id,)
        )
        entity_type = row[0][0] if row else ""
        primary_provider = PRIMARY_PROVIDER_BY_ENTITY.get(entity_type)
        normalized = []
        for provider, identifier_type, value in values:
            if provider in {"tmdb", "tvdb"} and entity_type in {"movie", "series"}:
                identifier_type = "movie" if entity_type == "movie" else "series"
            normalized.append((provider, identifier_type, str(value)))
        normalized = list(dict.fromkeys(normalized))
        current = [
            tuple(row)
            for row in self.db.execute(
                "SELECT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id=?",
                (entity_id,),
            )
        ]
        # A TVDB refresh is authoritative for the primary series identity, but
        # its remote-ID list is optional.  Do not erase a previously discovered
        # TMDB series link merely because a later filename/NFO pass only found
        # the TVDB ID.
        if entity_type == "series":
            normalized_keys = set(normalized)
            normalized.extend(
                value
                for value in current
                if value[0] == "tmdb" and value not in normalized_keys
            )
            normalized = list(dict.fromkeys(normalized))
        if set(normalized) != set(current):
            self.db.execute(
                "DELETE FROM entity_provider_ids WHERE entity_id=?", (entity_id,)
            )
            for provider, identifier_type, value in normalized:
                self.db.execute(
                    "INSERT OR REPLACE INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES(?,?,?,?,?)",
                    (
                        entity_id,
                        provider,
                        identifier_type,
                        value,
                        int(provider == primary_provider),
                    ),
                )
            if current:
                self._scan_provider_identity_changed.add(entity_id)
                self._mark_changed(entity_id)
        if normalized:
            self.db.execute(
                "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='explicit_id',updated_at=? WHERE id=?",
                (now(), entity_id),
            )

    def _resolve_and_seed(
        self,
        library_id: str,
        library_type: str,
        job_id: str,
        should_terminate: Callable[[], bool],
    ) -> None:
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
            "SELECT id,entity_type,relative_path,season_number,episode_number FROM library_entities WHERE library_id=?{} AND entity_type IN ({}) ORDER BY relative_path".format(
                parent_filter, ",".join("?" * len(entity_types))
            ),
            [library_id, *sorted(entity_types)],
        )
        metadata_candidates = self._metadata_candidates()
        rows = [
            row
            for row in rows
            if row[0] in self._scan_seen_ids
            and row[0] in metadata_candidates
            and self._needs_metadata(row[0])
        ]
        self.store.update_job(
            job_id,
            progress_total=len(rows),
            progress_current=0,
            message="Resolving provider metadata",
        )
        service = MetadataService()
        if library_type == "movies" and rows:
            self._resolve_movies_parallel(library_id, rows, job_id, should_terminate)
            return
        for index, (
            entity_id,
            entity_type,
            relative_path,
            _season,
            _episode,
        ) in enumerate(rows, start=1):
            self._check_termination(should_terminate)
            query, year = _inventory_query(relative_path or "")
            logger.info(
                "metadata root start library_id=%s entity_id=%s type=%s query=%s path=%s index=%s/%s",
                library_id,
                entity_id,
                entity_type,
                query,
                relative_path,
                index,
                len(rows),
            )
            explicit = [
                {"provider": row[0], "id": row[2]}
                for row in self.db.execute(
                    "SELECT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id=?",
                    (entity_id,),
                )
            ]
            try:
                result = service.resolve_inventory_entity(
                    entity_type, query, year, explicit
                )
            except ProviderError:
                self.db.execute(
                    "UPDATE library_entities SET match_status='failed',match_confidence=NULL,match_method='scan_resolution',updated_at=? WHERE id=?",
                    (now(), entity_id),
                )
                logger.exception(
                    "scan resolution failed library_id=%s entity_id=%s entity_type=%s path=%s",
                    library_id,
                    entity_id,
                    entity_type,
                    relative_path,
                )
                self.store.update_job(
                    job_id,
                    progress_current=index,
                    message=f"Metadata failed for {query}; continuing",
                )
                continue
            values = []
            for value in result["providerIds"]:
                identifier_type = (
                    "movie"
                    if entity_type == "movie"
                    else "series"
                    if entity_type == "series"
                    else entity_type
                )
                values.append((value["provider"], identifier_type, value["id"]))
            self._ids(entity_id, values)
            for value in result["providerIds"]:
                logger.info(
                    "metadata root locales start entity_id=%s type=%s provider=%s provider_id=%s",
                    entity_id,
                    entity_type,
                    value["provider"],
                    value["id"],
                )
                self._fetch_configured_locales(
                    service,
                    value["provider"],
                    entity_type,
                    str(value["id"]),
                    required=True,
                    progress=lambda message: self.store.update_job(
                        job_id, message=message
                    ),
                )
            self.db.execute(
                "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='scan_resolution',updated_at=? WHERE id=?",
                (now(), entity_id),
            )
            if entity_type == "series":
                self._derive_tmdb_child_ids(entity_id)
                self._derive_provider_child_ids(entity_id, result["metadata"])
                self._derive_tvdb_episode_ids(entity_id, service)
                self.db.execute(
                    "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='parent_resolution',updated_at=? WHERE parent_id=? AND match_status='unresolved'",
                    (now(), entity_id),
                )
            self.store.update_job(
                job_id, progress_current=index, message=f"Resolved {query}"
            )
            logger.info(
                "metadata root complete library_id=%s entity_id=%s type=%s query=%s index=%s/%s",
                library_id,
                entity_id,
                entity_type,
                query,
                index,
                len(rows),
            )
        self._seed_all_children(library_id, service, job_id, should_terminate)

    def _resolve_movies_parallel(
        self,
        library_id: str,
        rows: list[tuple],
        job_id: str,
        should_terminate: Callable[[], bool],
    ) -> None:
        futures = [
            metadata_root_executor.submit(
                library_id,
                self._resolve_movie_and_publish,
                library_id,
                row,
                job_id,
                should_terminate,
                index,
                len(rows),
            )
            for index, row in enumerate(rows, start=1)
        ]
        self._await_metadata_futures(futures, should_terminate)

    def _await_metadata_futures(
        self, futures: list[Future], should_terminate: Callable[[], bool]
    ) -> None:
        first_error: BaseException | None = None
        for future in as_completed(futures):
            if first_error is None:
                try:
                    self._check_termination(should_terminate)
                    future.result()
                except BaseException as error:
                    first_error = error
                    for pending in futures:
                        pending.cancel()
            else:
                try:
                    future.result()
                except BaseException:
                    pass
        if first_error is not None:
            raise first_error

    def _resolve_movie_and_publish(
        self,
        library_id: str,
        row: tuple,
        job_id: str,
        should_terminate: Callable[[], bool],
        index: int,
        total: int,
    ) -> None:
        self._resolve_movie_row(library_id, row, job_id, should_terminate, index, total)
        self._extract_and_reproject(row[0], "movie", should_terminate)
        self._publish_root(row[0])

    def _extract_and_reproject(
        self,
        entity_id: str,
        entity_type: str,
        should_terminate: Callable[[], bool],
    ) -> None:
        if entity_type not in {"movie", "episode"}:
            return
        try:
            from app.metadata_services import reproject_entity_artwork
            from app.screen_extractor import extract_entity

            extract_entity(
                self.db,
                entity_id,
                entity_type,
                should_terminate=should_terminate,
            )
            reproject_entity_artwork(self.db, entity_id)
        except Exception as error:
            logger.warning(
                "screen extractor fallback failed entity_id=%s type=%s error=%s",
                entity_id,
                entity_type,
                error,
            )

    def _queue_metadata_repair(
        self,
        entity_id: str,
        library_id: str,
        source_job_id: str,
        error: str,
        locales: Iterable[str] | None = None,
    ) -> None:
        if not self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrichment_queue'"
        ):
            return
        if locales is None:
            from app.models.metadata import MetadataLanguageSettings

            locales = MetadataLanguageSettings().get()
        timestamp = now()
        with self.db.transaction() as cursor:
            for locale in dict.fromkeys(locales):
                cursor.execute(
                    "INSERT INTO enrichment_queue(id,entity_id,library_id,kind,locale,priority,state,attempts,next_attempt_at,lease_owner,lease_expires_at,source_job_id,error,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,10,'retry',1,NULL,NULL,NULL,?,?,?,?) "
                    "ON CONFLICT(entity_id,kind,locale) DO UPDATE SET state='retry',priority=MAX(enrichment_queue.priority,excluded.priority),attempts=enrichment_queue.attempts+1,next_attempt_at=NULL,lease_owner=NULL,lease_expires_at=NULL,source_job_id=excluded.source_job_id,error=excluded.error,updated_at=excluded.updated_at",
                    (
                        str(uuid.uuid4()),
                        entity_id,
                        library_id,
                        "metadata",
                        locale,
                        source_job_id,
                        error,
                        timestamp,
                        timestamp,
                    ),
                )

    def _resolve_movie_row(
        self,
        library_id: str,
        row: tuple,
        job_id: str,
        should_terminate: Callable[[], bool],
        index: int,
        total: int,
    ) -> None:
        from app.providers import MetadataService, ProviderError

        entity_id, entity_type, relative_path, _season, _episode = row
        self._check_termination(should_terminate)
        query, year = _inventory_query(relative_path or "")
        logger.info(
            "metadata movie start library_id=%s entity_id=%s query=%s index=%s/%s",
            library_id,
            entity_id,
            query,
            index,
            total,
        )
        service = MetadataService()
        explicit = [
            {"provider": value[0], "id": value[2]}
            for value in self.db.execute(
                "SELECT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY is_primary DESC,provider",
                (entity_id,),
            )
        ]
        try:
            provider_ids = explicit
            if not provider_ids:
                result = service.resolve_inventory_entity(entity_type, query, year, [])
                provider_ids = result["providerIds"]
                self._ids(
                    entity_id,
                    [
                        (value["provider"], "movie", value["id"])
                        for value in provider_ids
                    ],
                )
            supported = [
                value for value in provider_ids if value["provider"] in {"tmdb", "tvdb"}
            ]
            if not supported:
                raise ValueError(f"No supported metadata identity for movie '{query}'")
            required_provider = (
                "tmdb"
                if any(value["provider"] == "tmdb" for value in supported)
                else supported[0]["provider"]
            )
            for value in supported:
                self._fetch_configured_locales(
                    service,
                    value["provider"],
                    entity_type,
                    str(value["id"]),
                    required=value["provider"] == required_provider,
                )
            self.db.execute(
                "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='scan_resolution',updated_at=? WHERE id=?",
                (now(), entity_id),
            )
            message = f"Resolved {query}"
            logger.info(
                "metadata movie complete library_id=%s entity_id=%s query=%s index=%s/%s",
                library_id,
                entity_id,
                query,
                index,
                total,
            )
        except (ProviderError, ValueError, OSError) as error:
            self.db.execute(
                "UPDATE library_entities SET match_status='failed',match_confidence=NULL,match_method='scan_resolution',updated_at=? WHERE id=?",
                (now(), entity_id),
            )
            logger.exception(
                "metadata movie failed; continuing library_id=%s entity_id=%s query=%s error=%s",
                library_id,
                entity_id,
                query,
                error,
            )
            self._queue_metadata_repair(
                entity_id, library_id, job_id, f"{type(error).__name__}: {error}"
            )
            message = f"Metadata failed for {query}; continuing"
        except Exception as error:
            self.db.execute(
                "UPDATE library_entities SET match_status='failed',match_confidence=NULL,match_method='scan_resolution',updated_at=? WHERE id=?",
                (now(), entity_id),
            )
            logger.exception(
                "unexpected metadata movie failure; continuing library_id=%s entity_id=%s query=%s error=%s",
                library_id,
                entity_id,
                query,
                error,
            )
            self._queue_metadata_repair(
                entity_id, library_id, job_id, f"{type(error).__name__}: {error}"
            )
            message = f"Metadata failed for {query}; continuing"
        self.store.update_job(job_id, progress_current=index, message=message)

    def _resolve_series_root(
        self,
        library_id: str,
        series_id: str,
        relative_path: str,
        service,
        job_id: str,
        should_terminate: Callable[[], bool],
    ) -> dict | None:
        """Resolve one discovered series before processing its seasons."""
        from app.providers import ProviderError

        self._check_termination(should_terminate)
        logger.info(
            "metadata series start library_id=%s series_id=%s path=%s",
            library_id,
            series_id,
            relative_path,
        )
        result = None
        # Revisit matched TVDB roots during an affected scan so the TVDB
        # remote-ID list can add the optional TMDB secondary identity.
        has_tmdb_identity = bool(
            self.db.execute(
                "SELECT 1 FROM entity_provider_ids WHERE entity_id=? AND provider='tmdb' AND identifier_type='series' LIMIT 1",
                (series_id,),
            )
        )
        if self._needs_metadata(series_id) or not has_tmdb_identity:
            query, year = _inventory_query(relative_path or "")
            explicit = [
                {"provider": row[0], "id": row[2]}
                for row in self.db.execute(
                    "SELECT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id=?",
                    (series_id,),
                )
            ]
            try:
                logger.info(
                    "metadata series match start series_id=%s query=%s",
                    series_id,
                    query,
                )
                result = service.resolve_inventory_entity(
                    "series", query, year, explicit
                )
            except ProviderError as error:
                self.db.execute(
                    "UPDATE library_entities SET match_status='failed',match_confidence=NULL,match_method='scan_resolution',updated_at=? WHERE id=?",
                    (now(), series_id),
                )
                raise ValueError(
                    f"Metadata resolution failed for series '{query}' at '{relative_path}': {error}"
                ) from error
            self._ids(
                series_id,
                [
                    (value["provider"], "series", value["id"])
                    for value in result["providerIds"]
                ],
            )
            self.db.execute(
                "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='scan_resolution',updated_at=? WHERE id=?",
                (now(), series_id),
            )
            for value in result["providerIds"]:
                logger.info(
                    "metadata series locales start series_id=%s provider=%s provider_id=%s",
                    series_id,
                    value["provider"],
                    value["id"],
                )
                self._fetch_configured_locales(
                    service,
                    value["provider"],
                    "series",
                    str(value["id"]),
                    required=value["provider"] == "tvdb",
                    progress=lambda message: self.store.update_job(
                        job_id, message=message
                    ),
                )
            logger.info("metadata series match complete series_id=%s", series_id)
            result = result or {"metadata": None}
        if not result:
            provider_rows = self.db.execute(
                "SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY is_primary DESC,provider",
                (series_id,),
            )
            for provider, provider_id in provider_rows:
                self._fetch_configured_locales(
                    service,
                    provider,
                    "series",
                    str(provider_id),
                    required=False,
                    progress=lambda message: self.store.update_job(
                        job_id, message=message
                    ),
                )
        self.store.update_job(job_id, message=f"Resolved series ({series_id})")
        logger.info("metadata series root complete series_id=%s", series_id)
        return result["metadata"] if result else None

    def _resolve_season_metadata(
        self,
        library_id: str,
        series_id: str,
        season_id: str,
        service,
        job_id: str,
        should_terminate: Callable[[], bool],
        series_metadata: dict | None = None,
        tvdb_identity: dict | None = None,
    ) -> None:
        """Attach provider IDs and fetch one season and all its episodes."""
        self._check_termination(should_terminate)
        season_row = self.db.execute(
            "SELECT season_number,relative_path FROM library_entities WHERE id=? AND entity_type='season'",
            (season_id,),
        )
        if not season_row:
            return
        season_number, season_path = season_row[0]
        provider_rows = self.db.execute(
            "SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY is_primary DESC,provider",
            (series_id,),
        )
        provider_ids = {row[0]: str(row[1]) for row in provider_rows}

        if provider_ids.get("tmdb"):
            self._derive_tmdb_child_ids(series_id, season_id=season_id)

        if provider_ids.get("tvdb"):
            season_provider_id = None
            if tvdb_identity:
                season_provider_id = next(
                    (
                        value["providerId"]
                        for value in tvdb_identity.get("seasons", [])
                        if int(value.get("seasonNumber", -1)) == int(season_number)
                    ),
                    None,
                )
            if not season_provider_id and series_metadata:
                season_provider_id = next(
                    (
                        str(value.get("id"))
                        for value in series_metadata.get("children", []) or []
                        if value.get("type") == "season"
                        and int(value.get("season", -1)) == int(season_number)
                        and value.get("id") is not None
                    ),
                    None,
                )
            if season_provider_id:
                self._ids(
                    season_id,
                    [("tvdb", "season", str(season_provider_id))],
                )
                self._derive_tvdb_episode_ids(series_id, service, season_id=season_id)

        self._seed_all_children(
            library_id,
            service,
            job_id,
            should_terminate,
            season_id=season_id,
        )
        logger.info(
            "metadata season complete series_id=%s season_id=%s season_number=%s path=%s",
            series_id,
            season_id,
            season_number,
            season_path,
        )

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
                "SELECT id,entity_type,season_number,episode_number FROM library_entities WHERE parent_id IN ({}) AND entity_type='episode' ORDER BY season_number,episode_number".format(
                    ",".join("?" * len(season_ids))
                ),
                season_ids,
            )
        by_provider = {row[0]: row[1] for row in provider_rows}
        if not by_provider.get(primary_provider):
            raise ProviderError(f"Resolved series {series_id} has no TVDB ID")
        from app.metadata_services import MetadataIngestService

        ingest = MetadataIngestService(service, background_assets=False)
        locales = ingest.locales()
        for provider, provider_id in by_provider.items():
            if provider not in {"tvdb", "tmdb"}:
                continue
            if provider == "tvdb" and (
                hasattr(service, "fetch_locales") or hasattr(service, "fetch")
            ):
                ingest.ingest_locales(
                    provider, "series", provider_id, locales, force=False
                )
            aggregate = None
            for locale in locales:
                try:
                    current = service.aggregate_series(provider, provider_id, locale)
                    if current.get("series"):
                        ingest.ingest_document(
                            provider,
                            "series",
                            provider_id,
                            locale,
                            current["series"],
                        )
                    aggregate = aggregate or current
                except Exception as error:
                    if locale == "en" and provider == primary_provider:
                        raise
                    logger.warning(
                        "series aggregation failed series_id=%s provider=%s locale=%s: %s",
                        series_id,
                        provider,
                        locale,
                        error,
                    )
                    continue
                if locale != "en":
                    continue
                aggregate = current
            if not aggregate:
                continue
            mapped_seasons = {}
            for metadata in aggregate.get("seasons", []):
                child_number = next(
                    (
                        value.get("season")
                        for value in metadata.get("children", [])
                        if value.get("type") == "episode"
                    ),
                    None,
                )
                if child_number is None:
                    # Season metadata itself does not always echo its number;
                    # provider IDs for TMDB encode it, while TVDB payloads do.
                    provider_value = str(metadata.get("providerId") or "")
                    child_number = (
                        provider_value.rsplit(":", 1)[-1]
                        if provider == "tmdb" and ":" in provider_value
                        else None
                    )
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
                    episode_child = next(
                        (
                            value
                            for value in metadata.get("children", [])
                            if value.get("type") == "episode"
                        ),
                        None,
                    )
                    season_value = (
                        episode_child.get("season")
                        if episode_child
                        else metadata.get("seasonNumber")
                    )
                    episode_value = (
                        episode_child.get("episode")
                        if episode_child
                        else metadata.get("episodeNumber")
                    )
                    if season_value is None or episode_value is None:
                        continue
                    key = (int(season_value), int(episode_value))
                mapped_episodes[key] = metadata
            for child_id, entity_type, season_number, episode_number in episodes:
                metadata = mapped_episodes.get(
                    (int(season_number), int(episode_number))
                )
                if not metadata:
                    continue
                self._ids(
                    child_id, [(provider, "episode", str(metadata["providerId"]))]
                )
                self._persist_normalized_ids(child_id, "episode", metadata)

    def _seed_all_children(
        self,
        library_id: str,
        service,
        job_id: str,
        should_terminate: Callable[[], bool],
        parent_id: str | None = None,
        season_id: str | None = None,
    ) -> None:
        """Fetch common metadata and IDs for every season, episode, release, and track."""
        from app.metadata_services import MetadataIngestService

        ingest = MetadataIngestService(service, background_assets=False)
        if season_id:
            rows = self.db.execute(
                "SELECT id,entity_type,relative_path,parent_id,season_number,episode_number FROM library_entities WHERE library_id=? AND (id=? OR parent_id=?) ORDER BY CASE WHEN entity_type='season' THEN 0 ELSE 1 END, episode_number IS NULL, episode_number, relative_path COLLATE NOCASE",
                (library_id, season_id, season_id),
            )
        elif parent_id:
            rows = self.db.execute(
                "SELECT id,entity_type,relative_path,parent_id,season_number,episode_number FROM library_entities WHERE library_id=? AND (parent_id=? OR parent_id IN (SELECT id FROM library_entities WHERE parent_id=? AND entity_type='season')) ORDER BY length(relative_path),relative_path",
                (library_id, parent_id, parent_id),
            )
        else:
            rows = self.db.execute(
                "SELECT id,entity_type,relative_path,parent_id,season_number,episode_number FROM library_entities WHERE library_id=? AND parent_id IS NOT NULL ORDER BY length(relative_path),relative_path",
                (library_id,),
            )
        metadata_candidates = self._metadata_candidates()

        def needs_localized_metadata(row: tuple) -> bool:
            entity_id, entity_type = row[0], row[1]
            if entity_id not in metadata_candidates:
                return False
            if (
                entity_id in self._scan_created_ids
                or entity_id in self._scan_provider_identity_changed
                or entity_id in self._scan_delta["content_changed"]
            ):
                return True
            if self._needs_metadata(entity_id):
                return True
            provider_rows = self.db.execute(
                "SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=?",
                (entity_id,),
            )
            priorities = {
                "season": ["tvdb", "tmdb"],
                "episode": ["tvdb", "tmdb"],
                "release": ["musicbrainz"],
                "track": ["musicbrainz"],
            }.get(entity_type, [row[0] for row in provider_rows])
            for provider in priorities:
                provider_id = next(
                    (value[1] for value in provider_rows if value[0] == provider),
                    None,
                )
                if not provider_id:
                    continue
                if any(
                    not self.db.execute(
                        "SELECT 1 FROM metadata_cache WHERE provider=? AND entity_type=? AND provider_id=? AND locale=? LIMIT 1",
                        (provider, entity_type, str(provider_id), locale),
                    )
                    for locale in ingest.locales()
                ):
                    return True
            return False

        def needs_artwork_reconciliation(row: tuple) -> bool:
            if row[1] not in {"movie", "episode"} or row[0] not in self._scan_seen_ids:
                return False
            if needs_localized_metadata(row):
                return True
            # A ready Screen Extractor asset can exist without a selected
            # catalog row (the historical publication bug). Revisit entities
            # missing a Primary selection so incremental scans repair them.
            locales = ingest.locales()
            if not self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_artwork_selection'"
            ):
                return False
            return bool(
                self.db.execute(
                    "SELECT 1 FROM catalog_artwork_selection "
                    "WHERE entity_id=? AND image_type='Primary' "
                    "GROUP BY entity_id HAVING COUNT(DISTINCT locale)<?",
                    (row[0], len(locales)),
                )
            )

        rows = [row for row in rows if needs_artwork_reconciliation(row)]
        self.store.update_job(
            job_id,
            progress_total=len(rows),
            progress_current=0,
            message="Seeding child metadata",
        )
        for index, (
            entity_id,
            entity_type,
            relative_path,
            row_parent_id,
            season_number,
            episode_number,
        ) in enumerate(rows, start=1):
            self._check_termination(should_terminate)
            logger.info(
                "metadata child start library_id=%s entity_id=%s type=%s provider_path=%s index=%s/%s",
                library_id,
                entity_id,
                entity_type,
                relative_path,
                index,
                len(rows),
            )
            provider_rows = self.db.execute(
                "SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY is_primary DESC,provider",
                (entity_id,),
            )
            if not provider_rows:
                if entity_type in {"season", "episode"} and (parent_id or season_id):
                    logger.warning(
                        "No TVDB provider ID was aggregated; leaving file unresolved type=%s path=%s",
                        entity_type,
                        relative_path,
                    )
                    self.store.update_job(
                        job_id,
                        progress_current=index,
                        message=f"Skipped unresolved {entity_type} {relative_path}",
                    )
                    self._extract_and_reproject(entity_id, entity_type, should_terminate)
                    continue
                query, year = _inventory_query(relative_path or "")
                try:
                    result = service.resolve_inventory_entity(
                        entity_type, query, year, []
                    )
                    self._ids(
                        entity_id,
                        [
                            (value["provider"], entity_type, value["id"])
                            for value in result["providerIds"]
                        ],
                    )
                    provider_rows = [
                        (value["provider"], value["id"])
                        for value in result["providerIds"]
                    ]
                except Exception as error:
                    self.db.execute(
                        "UPDATE library_entities SET match_status='failed',match_method='scan_resolution',updated_at=? WHERE id=?",
                        (now(), entity_id),
                    )
                    logger.exception(
                        "child resolution failed library_id=%s entity_id=%s type=%s path=%s",
                        library_id,
                        entity_id,
                        entity_type,
                        relative_path,
                    )
                    failure = (
                        f"Metadata resolution failed for {entity_type} "
                        f"'{relative_path}': {type(error).__name__}: {error}"
                    )
                    self._queue_metadata_repair(entity_id, library_id, job_id, failure)
                    self.store.update_job(
                        job_id,
                        progress_current=index,
                        message=f"Metadata failed for {entity_type} {relative_path}; continuing",
                    )
                    self._extract_and_reproject(entity_id, entity_type, should_terminate)
                    continue
            priorities = {
                "season": ["tvdb", "tmdb"],
                "episode": ["tvdb", "tmdb"],
                "release": ["musicbrainz"],
                "track": ["musicbrainz"],
            }.get(entity_type, [row[0] for row in provider_rows])
            required = priorities[0] if priorities else None
            fetched = False
            required_succeeded = False
            errors = []
            for provider in priorities:
                provider_id = next(
                    (row[1] for row in provider_rows if row[0] == provider), None
                )
                if not provider_id:
                    continue
                locales = ingest.locales()
                self.store.update_job(
                    job_id,
                    message=(
                        f"Fetching {provider} {entity_type} metadata "
                        f"{relative_path} ({index}/{len(rows)}, {len(locales)} locales)"
                    ),
                )
                logger.info(
                    "metadata child locale batch start entity_id=%s type=%s provider=%s provider_id=%s locales=%s",
                    entity_id,
                    entity_type,
                    provider,
                    provider_id,
                    locales,
                )
                try:
                    normalized_by_locale = ingest.ingest_locales(
                        provider, entity_type, provider_id, locales, force=False
                    )
                    for locale, normalized in normalized_by_locale.items():
                        fetched = True
                        if provider == required:
                            required_succeeded = True
                        self._persist_normalized_ids(entity_id, entity_type, normalized)
                        self._persist_child_ids(entity_id, normalized)
                        logger.info(
                            "metadata child locale complete entity_id=%s type=%s provider=%s provider_id=%s locale=%s",
                            entity_id,
                            entity_type,
                            provider,
                            provider_id,
                            locale,
                        )
                except Exception as error:
                    errors.append(
                        f"{provider}/{','.join(locales)}: {type(error).__name__}: {error}"
                    )
                    logger.warning(
                        "child metadata seed failed entity_id=%s type=%s provider=%s provider_id=%s locales=%s: %s",
                        entity_id,
                        entity_type,
                        provider,
                        provider_id,
                        locales,
                        error,
                    )
            if not fetched or (required and not required_succeeded):
                failure = (
                    f"Metadata resolution failed for {entity_type} "
                    f"'{relative_path}': required provider {required or 'provider'} "
                    f"could not be seeded; {'; '.join(errors) or 'no usable provider metadata'}"
                )
                self.db.execute(
                    "UPDATE library_entities SET match_status='failed',match_confidence=NULL,match_method='scan_child_resolution',updated_at=? WHERE id=?",
                    (now(), entity_id),
                )
                self._queue_metadata_repair(
                    entity_id, library_id, job_id, failure, ingest.locales()
                )
                logger.warning(
                    "metadata child failed; continuing entity_id=%s type=%s index=%s/%s error=%s",
                    entity_id,
                    entity_type,
                    index,
                    len(rows),
                    failure,
                )
                self.store.update_job(
                    job_id,
                    progress_current=index,
                    message=f"Metadata failed for {entity_type} {relative_path}; continuing",
                )
                self._extract_and_reproject(entity_id, entity_type, should_terminate)
                continue
            self.db.execute(
                "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='scan_child_resolution',updated_at=? WHERE id=?",
                (now(), entity_id),
            )
            self._extract_and_reproject(entity_id, entity_type, should_terminate)
            self.store.update_job(
                job_id,
                progress_current=index,
                message=f"Seeded {entity_type} {relative_path}",
            )
            logger.info(
                "metadata child complete entity_id=%s type=%s index=%s/%s",
                entity_id,
                entity_type,
                index,
                len(rows),
            )

    @staticmethod
    def _fetch_configured_locales(
        service,
        provider: str,
        entity_type: str,
        provider_id: str,
        required: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        from app.metadata_services import MetadataIngestService

        if provider not in {"tmdb", "tvdb", "musicbrainz"}:
            return

        ingest = MetadataIngestService(service, background_assets=False)
        locales = ingest.locales()
        if progress:
            progress(
                f"Fetching {provider} {entity_type} {provider_id} metadata ({len(locales)} locales)"
            )
        logger.info(
            "metadata locale batch start provider=%s entity_type=%s provider_id=%s locales=%s",
            provider,
            entity_type,
            provider_id,
            locales,
        )
        try:
            ingest.ingest_locales(
                provider, entity_type, provider_id, locales, force=False
            )
            if progress:
                progress(
                    f"Cached {provider} {entity_type} {provider_id} metadata ({len(locales)} locales)"
                )
            logger.info(
                "metadata locale batch complete provider=%s entity_type=%s provider_id=%s locales=%s",
                provider,
                entity_type,
                provider_id,
                locales,
            )
        except Exception as error:
            logger.warning(
                "localized metadata batch fetch failed provider=%s entity_type=%s provider_id=%s locales=%s: %s",
                provider,
                entity_type,
                provider_id,
                locales,
                error,
            )
            if required:
                raise ValueError(
                    f"No metadata locale could be fetched for {provider} {entity_type} {provider_id}: {type(error).__name__}: {error}"
                ) from error

    def _persist_normalized_ids(
        self, entity_id: str, entity_type: str, normalized: dict
    ) -> None:
        values = []
        for value in normalized.get("ids", []) or []:
            if value.get("provider") and value.get("id"):
                values.append(
                    (
                        value["provider"],
                        value.get("identifierType") or entity_type,
                        str(value["id"]),
                    )
                )
        self._ids(entity_id, values)

    def _persist_child_ids(self, parent_id: str, normalized: dict) -> None:
        """Attach provider child IDs to inventory children by stable numbers."""
        tracks = [
            value for value in normalized.get("tracks", []) or [] if value.get("id")
        ]
        if not tracks:
            return
        children = self.db.execute(
            "SELECT id,track_number,disc_number FROM library_entities WHERE parent_id=? AND entity_type='track' ORDER BY disc_number,track_number,relative_path",
            (parent_id,),
        )
        for index, (entity_id, track_number, disc_number) in enumerate(children):
            candidate = next(
                (
                    value
                    for value in tracks
                    if track_number is not None
                    and int(value.get("position") or 0) == int(track_number)
                ),
                None,
            )
            candidate = candidate or (tracks[index] if index < len(tracks) else None)
            if candidate and candidate.get("id"):
                self._ids(
                    entity_id, [("musicbrainz", "recording", str(candidate["id"]))]
                )

    def _derive_tmdb_child_ids(
        self, series_id: str, season_id: str | None = None
    ) -> None:
        provider_rows = self.db.execute(
            "SELECT provider_id FROM entity_provider_ids WHERE entity_id=? AND provider='tmdb'",
            (series_id,),
        )
        if not provider_rows:
            return
        seasons = (
            self.db.execute(
                "SELECT id,entity_type,season_number,episode_number FROM library_entities WHERE id=? AND parent_id=? AND entity_type='season'",
                (season_id, series_id),
            )
            if season_id
            else self.db.execute(
                "SELECT id,entity_type,season_number,episode_number FROM library_entities WHERE parent_id=? AND entity_type='season'",
                (series_id,),
            )
        )
        season_ids = [row[0] for row in seasons]
        children = list(seasons)
        if season_ids:
            children.extend(
                self.db.execute(
                    "SELECT id,entity_type,season_number,episode_number FROM library_entities WHERE parent_id IN ({}) AND entity_type='episode'".format(
                        ",".join("?" * len(season_ids))
                    ),
                    season_ids,
                )
            )
        for child_id, entity_type, season_number, episode_number in children:
            if entity_type == "season":
                provider_id = f"{provider_rows[0][0]}:{season_number}"
            elif (
                entity_type == "episode"
                and season_number is not None
                and episode_number is not None
            ):
                provider_id = f"{provider_rows[0][0]}:{season_number}:{episode_number}"
            else:
                continue
            self._ids(child_id, [("tmdb", entity_type, provider_id)])
            self.db.execute(
                "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='parent_resolution',updated_at=? WHERE id=?",
                (now(), child_id),
            )

    def _derive_provider_child_ids(self, series_id: str, metadata: dict) -> None:
        if metadata.get("provider") == "tmdb":
            return
        seasons = self.db.execute(
            "SELECT id,entity_type,season_number,episode_number FROM library_entities WHERE parent_id=? AND entity_type='season'",
            (series_id,),
        )
        season_ids = [row[0] for row in seasons]
        children = list(seasons)
        if season_ids:
            children.extend(
                self.db.execute(
                    "SELECT id,entity_type,season_number,episode_number FROM library_entities WHERE parent_id IN ({}) AND entity_type='episode'".format(
                        ",".join("?" * len(season_ids))
                    ),
                    season_ids,
                )
            )
        for child_id, entity_type, season_number, episode_number in children:
            for value in metadata.get("children", []) or []:
                if value.get("type") != entity_type or int(
                    value.get("season", -1)
                ) != int(season_number if season_number is not None else -2):
                    continue
                if entity_type == "episode" and int(value.get("episode", -1)) != int(
                    episode_number if episode_number is not None else -2
                ):
                    continue
                self._ids(
                    child_id,
                    [(metadata.get("provider", ""), entity_type, str(value["id"]))],
                )
                break

    def _derive_tvdb_episode_ids(
        self,
        series_id: str,
        service,
        season_id: str | None = None,
    ) -> None:
        """Fetch TVDB season details and attach exact TVDB episode IDs."""
        seasons = (
            self.db.execute(
                "SELECT id,season_number,relative_path FROM library_entities WHERE id=? AND parent_id=? AND entity_type='season'",
                (season_id, series_id),
            )
            if season_id
            else self.db.execute(
                "SELECT id,season_number,relative_path FROM library_entities WHERE parent_id=? AND entity_type='season' ORDER BY season_number",
                (series_id,),
            )
        )
        for season_id, season_number, season_path in seasons:
            provider_rows = self.db.execute(
                "SELECT provider_id FROM entity_provider_ids WHERE entity_id=? AND provider='tvdb'",
                (season_id,),
            )
            if not provider_rows:
                raise ValueError(
                    f"TVDB season ID could not be resolved for season {season_number} at '{season_path}'"
                )
            season_provider_id = str(provider_rows[0][0])
            try:
                fetch_identity = getattr(service, "fetch_for_identity", None)
                normalized = (
                    fetch_identity("tvdb", "season", season_provider_id)
                    if fetch_identity
                    else service.fetch(
                        "tvdb", "season", season_provider_id, "en", force=True
                    )
                )
            except Exception as error:
                raise ValueError(
                    f"TVDB season details failed for season {season_number} at '{season_path}' (ID {season_provider_id}): {type(error).__name__}: {error}"
                ) from error
            self._persist_normalized_ids(season_id, "season", normalized)
            episodes = self.db.execute(
                "SELECT id,episode_number,relative_path FROM library_entities WHERE parent_id=? AND entity_type='episode' ORDER BY episode_number,relative_path",
                (season_id,),
            )
            tvdb_children = [
                value
                for value in normalized.get("children", []) or []
                if value.get("type") == "episode" and value.get("id") is not None
            ]
            for episode_id, episode_number, episode_path in episodes:
                match = next(
                    (
                        value
                        for value in tvdb_children
                        if int(value.get("season", season_number)) == int(season_number)
                        and int(value.get("episode", -1)) == int(episode_number)
                    ),
                    None,
                )
                if not match:
                    logger.warning(
                        "TVDB episode ID could not be resolved; leaving file unresolved season=%s episode=%s path=%s",
                        season_number,
                        episode_number,
                        episode_path,
                    )
                    self.db.execute(
                        "DELETE FROM entity_provider_ids WHERE entity_id=? AND identifier_type='episode'",
                        (episode_id,),
                    )
                    continue
                self._ids(episode_id, [("tvdb", "episode", str(match["id"]))])

    def _files(
        self,
        entity_id: str,
        root: Path,
        files: Iterable[Path | tuple[Path, os.stat_result | None]],
        job_id: str | None = None,
    ) -> dict:
        """Reconcile media rows in place and return a scan delta."""
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(media_files)")}
        has_fingerprint = "quick_fingerprint" in columns
        select_fingerprint = ",quick_fingerprint" if has_fingerprint else ""
        existing_rows = self.db.execute(
            f"SELECT id,relative_path,role,language,flags,size,modified_ns{select_fingerprint} FROM media_files WHERE entity_id=?",
            (entity_id,),
        )
        existing = {(row[1], row[2]): row for row in existing_rows}
        seen = set()
        result = {
            "added": 0,
            "updated": 0,
            "removed": 0,
            "unchanged": 0,
            "content_changed": False,
        }
        for file_entry in files:
            if isinstance(file_entry, tuple):
                path, discovered_stat = file_entry
            else:
                path, discovered_stat = file_entry, None
            role = media_role(path)
            if not role:
                continue
            if discovered_stat is None and path.is_symlink():
                relative_path = relative(str(root), str(path))
                if (relative_path, role) in existing:
                    seen.add((relative_path, role))
                continue
            if job_id:
                self._set_stage(
                    job_id,
                    f"Inspecting {path.name}",
                    persist=False,
                    entityId=entity_id,
                    path=str(path),
                )
            file_started = time.monotonic()
            logger.debug(
                "library scan file stat start entity_id=%s path=%s role=%s",
                entity_id,
                path,
                role,
            )
            if discovered_stat is not None:
                file_size = discovered_stat.st_size
                modified_ns = discovered_stat.st_mtime_ns
            elif role in {"subtitle", "lyrics"}:
                sidecar_stat = _bounded_sidecar_stat(path)
                if sidecar_stat is None:
                    relative_path = relative(str(root), str(path))
                    key = (relative_path, role)
                    if key in existing:
                        seen.add(key)
                    logger.warning(
                        "library scan sidecar stat deferred entity_id=%s path=%s duration_seconds=%.1f",
                        entity_id,
                        path,
                        time.monotonic() - file_started,
                    )
                    continue
                file_size, modified_ns = sidecar_stat
            else:
                try:
                    file_stat = path.stat()
                    file_size, modified_ns = file_stat.st_size, file_stat.st_mtime_ns
                except OSError:
                    # An existing row is retained when a scan cannot stat the
                    # path.  A transient permission/mount failure must never
                    # be interpreted as confirmed deletion.
                    relative_path = relative(str(root), str(path))
                    if (relative_path, role) in existing:
                        seen.add((relative_path, role))
                    logger.warning(
                        "library scan file stat failed entity_id=%s path=%s duration_seconds=%.1f",
                        entity_id,
                        path,
                        time.monotonic() - file_started,
                    )
                    continue
            logger.debug(
                "library scan file stat complete entity_id=%s path=%s size=%s modified_ns=%s duration_seconds=%.1f",
                entity_id,
                path,
                file_size,
                modified_ns,
                time.monotonic() - file_started,
            )
            language = (
                sidecar_language(path) if role in {"subtitle", "lyrics"} else None
            )
            relative_path = relative(str(root), str(path))
            key = (relative_path, role)
            seen.add(key)
            old = existing.get(key)
            old_fingerprint = old[7] if old and has_fingerprint else None
            if old and old[5] == file_size and old[6] == modified_ns:
                result["unchanged"] += 1
                continue
            if role in {"subtitle", "lyrics"}:
                if old:
                    self.db.execute(
                        f"UPDATE media_files SET language=?,flags=?,size=?,modified_ns=?{',quick_fingerprint=NULL' if has_fingerprint else ''} WHERE id=?",
                        (language, None, file_size, modified_ns, old[0]),
                    )
                    result["updated"] += 1
                elif has_fingerprint:
                    self.db.execute(
                        "INSERT INTO media_files(id,entity_id,relative_path,role,language,flags,size,modified_ns,quick_fingerprint) VALUES(?,?,?,?,?,?,?,?,NULL)",
                        (
                            new_id(),
                            entity_id,
                            relative_path,
                            role,
                            language,
                            None,
                            file_size,
                            modified_ns,
                        ),
                    )
                    result["added"] += 1
                else:
                    self.db.execute(
                        "INSERT INTO media_files(id,entity_id,relative_path,role,language,flags,size,modified_ns) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            new_id(),
                            entity_id,
                            relative_path,
                            role,
                            language,
                            None,
                            file_size,
                            modified_ns,
                        ),
                    )
                    result["added"] += 1
                continue
            fingerprint_started = time.monotonic()
            if job_id:
                self._set_stage(
                    job_id,
                    f"Fingerprinting {path.name} ({file_size} bytes)",
                    persist=False,
                    entityId=entity_id,
                    path=str(path),
                    size=file_size,
                )
            logger.debug(
                "library scan file fingerprint start entity_id=%s path=%s size=%s",
                entity_id,
                path,
                file_size,
            )
            try:
                quick_fingerprint, bytes_read = _quick_fingerprint(path, file_size)
            except OSError:
                logger.warning(
                    "library scan file fingerprint deferred entity_id=%s path=%s duration_seconds=%.1f",
                    entity_id,
                    path,
                    time.monotonic() - fingerprint_started,
                )
                continue
            logger.debug(
                "library scan file fingerprint complete entity_id=%s path=%s bytes_read=%s duration_seconds=%.1f",
                entity_id,
                path,
                bytes_read,
                time.monotonic() - fingerprint_started,
            )
            if old:
                content_changed = (
                    old_fingerprint != quick_fingerprint if has_fingerprint else True
                )
                self.db.execute(
                    f"UPDATE media_files SET language=?,flags=?,size=?,modified_ns=?{',quick_fingerprint=?' if has_fingerprint else ''} WHERE id=?",
                    (
                        [
                            language,
                            None,
                            file_size,
                            modified_ns,
                            quick_fingerprint,
                            old[0],
                        ]
                        if has_fingerprint
                        else [language, None, file_size, modified_ns, old[0]]
                    ),
                )
                result["updated"] += 1
                if content_changed and role == "media":
                    result["content_changed"] = True
            else:
                if has_fingerprint:
                    self.db.execute(
                        "INSERT INTO media_files(id,entity_id,relative_path,role,language,flags,size,modified_ns,quick_fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            new_id(),
                            entity_id,
                            relative_path,
                            role,
                            language,
                            None,
                            file_size,
                            modified_ns,
                            quick_fingerprint,
                        ),
                    )
                else:
                    self.db.execute(
                        "INSERT INTO media_files(id,entity_id,relative_path,role,language,flags,size,modified_ns) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            new_id(),
                            entity_id,
                            relative_path,
                            role,
                            language,
                            None,
                            file_size,
                            modified_ns,
                        ),
                    )
                result["added"] += 1
                if role == "media":
                    result["content_changed"] = True
        for key, old in existing.items():
            if key in seen:
                continue
            self.db.execute("DELETE FROM media_files WHERE id=?", (old[0],))
            result["removed"] += 1
            if old[2] == "media":
                result["content_changed"] = True
        if has_fingerprint:
            self._materialize_local_artwork(entity_id, root)
        if result["added"] or result["removed"] or result["content_changed"]:
            self._mark_changed(entity_id, content_changed=result["content_changed"])
        # Probe after the file rows are reconciled so playback never depends
        # on a stale source row. A same-fingerprint timestamp touch does not probe.
        if result["content_changed"]:
            from app.playback import PlaybackManager

            probe_started = time.monotonic()
            if job_id:
                self._set_stage(
                    job_id,
                    "Probing changed media",
                    persist=False,
                    entityId=entity_id,
                )
            logger.debug(
                "library scan probe start entity_id=%s files_added=%s files_updated=%s files_removed=%s",
                entity_id,
                result["added"],
                result["updated"],
                result["removed"],
            )
            PlaybackManager().probe_entity(entity_id)
            logger.debug(
                "library scan probe complete entity_id=%s duration_seconds=%.1f",
                entity_id,
                time.monotonic() - probe_started,
            )
        return result

    def _materialize_local_artwork(self, entity_id: str, root: Path) -> None:
        cache = LocalArtworkCache(self.db)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(media_files)")}
        select_blur_hash = ",image_blur_hash" if "image_blur_hash" in columns else ""
        for values in self.db.execute(
            f"SELECT id,relative_path,quick_fingerprint{select_blur_hash} FROM media_files WHERE entity_id=? AND role='image'",
            (entity_id,),
        ):
            file_id, relative_path, content_hash, *stored = values
            stored_blur_hash = stored[0] if stored else None
            target = cache.path(content_hash)
            if target is None:
                continue
            source = root / relative_path
            try:
                resolved_root = root.resolve()
                resolved_source = source.resolve(strict=True)
                resolved_source.relative_to(resolved_root)
            except (OSError, RuntimeError, ValueError):
                continue
            if source.is_symlink():
                continue
            if not target.is_file() or not target.stat().st_size:
                if not resolved_source.is_file():
                    continue
                try:
                    cache.materialize(resolved_source, content_hash)
                except Exception as error:
                    logger.warning(
                        "local artwork WebP encoding failed entity_id=%s path=%s error=%s",
                        entity_id,
                        relative_path,
                        error,
                    )
                    continue
            if "image_blur_hash" not in columns or stored_blur_hash:
                continue
            try:
                self.db.execute(
                    "UPDATE media_files SET image_blur_hash=? WHERE id=?",
                    (blurhash_for_image(target), file_id),
                )
            except Exception as error:
                logger.warning(
                    "local artwork BlurHash encoding failed entity_id=%s path=%s error=%s",
                    entity_id,
                    relative_path,
                    error,
                )

    @staticmethod
    def _video_state(
        path: Path, file_stat: os.stat_result | None = None
    ) -> str:
        """Classify a video candidate without mistaking access failure for absence."""

        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            return "unsupported"
        try:
            if path.is_symlink():
                return "unsupported"
        except OSError:
            return "inaccessible"
        try:
            value = file_stat if file_stat is not None else path.stat()
        except OSError:
            return "inaccessible"
        return "supported" if stat.S_ISREG(value.st_mode) else "unsupported"

    def _walk_file_entries(self, directory: Path):
        def traversal_error(error):
            self._record_access_error(
                Path(getattr(error, "filename", None) or directory)
            )

        for current, _directories, filenames in os.walk(
            directory, onerror=traversal_error
        ):
            current_path = Path(current)
            for name in filenames:
                path = current_path / name
                # Do not index symlinks or reparse points.  A path that
                # resolves outside the library root is treated as inaccessible
                # and retained in the existing inventory for a later scan.
                try:
                    if path.is_symlink():
                        yield path, None
                        continue
                except OSError:
                    if path.suffix.lower() in VIDEO_EXTENSIONS:
                        self._record_access_error(path)
                    yield path, None
                    continue
                stat_started = time.monotonic()
                try:
                    file_stat = path.stat()
                except OSError:
                    if path.suffix.lower() in VIDEO_EXTENSIONS:
                        self._record_access_error(path)
                    yield path, None
                    continue
                stat_seconds = time.monotonic() - stat_started
                if stat_seconds >= 1.0:
                    logger.warning(
                        "library scan enumeration stat slow path=%s duration_seconds=%.1f",
                        path,
                        stat_seconds,
                    )
                yield path, file_stat

    @staticmethod
    def _target_entries(root: Path, targets: set[str] | None) -> list[Path]:
        if targets is None:
            return list(root.iterdir())
        entries = []
        for target in sorted(targets):
            candidate = root / target
            if candidate.exists() or candidate.is_symlink():
                entries.append(candidate)
        return entries

    @classmethod
    def _is_supported_video(
        cls, path: Path, file_stat: os.stat_result | None = None
    ) -> bool:
        return cls._video_state(path, file_stat) == "supported"

    def _series_episode_plan(
        self,
        root: Path,
        series_dir: Path,
        should_terminate: Callable[[], bool],
        children: list[Path] | None = None,
    ) -> list[
        tuple[
            Path,
            int,
            list[
                tuple[
                    int,
                    str,
                    Path,
                    int,
                    int | None,
                    list[tuple[Path, os.stat_result | None]],
                ]
            ],
        ]
    ]:
        children = children if children is not None else list(series_dir.iterdir())
        season_dirs = [
            path
            for path in children
            if path.is_dir()
            and (SEASON_RE.match(path.name) or path.name.lower() == "specials")
        ]
        child_video_states = [self._video_state(path) for path in children]
        if any(state == "inaccessible" for state in child_video_states):
            self._record_access_error(series_dir)
        if any(state == "supported" for state in child_video_states):
            season_dirs.append(series_dir)
        season_dirs.sort(
            key=lambda path: (
                0
                if path.name.lower() == "specials"
                else int(SEASON_RE.match(path.name).group(1))
                if SEASON_RE.match(path.name)
                else 1,
                1 if path == series_dir else 0,
                path.name.casefold(),
            )
        )
        plan = []
        for season_dir in season_dirs:
            self._check_termination(should_terminate)
            match = SEASON_RE.match(season_dir.name)
            season_folder_number = (
                int(match.group(1))
                if match
                else (0 if season_dir.name.lower() == "specials" else 1)
            )
            if season_dir == series_dir:
                episode_entries = []
                for path in children:
                    try:
                        value = path.stat()
                    except OSError:
                        if path.suffix.lower() in VIDEO_EXTENSIONS:
                            self._record_access_error(path)
                        episode_entries.append((path, None))
                        continue
                    if stat.S_ISREG(value.st_mode):
                        episode_entries.append((path, value))
            else:
                episode_entries = []
                try:
                    for candidate in self._walk_file_entries(season_dir):
                        self._check_termination(should_terminate)
                        episode_entries.append(candidate)
                except OSError:
                    self._record_access_error(season_dir)
            files_by_parent: dict[Path, list[tuple[Path, os.stat_result | None]]] = {}
            for entry in episode_entries:
                files_by_parent.setdefault(entry[0].parent, []).append(entry)
            episode_records = []
            for media, media_stat in episode_entries:
                self._check_termination(should_terminate)
                if not self._is_supported_video(media, media_stat):
                    continue
                episode_match = EPISODE_RE.search(media.stem)
                guessed = guess_media(media) if not episode_match else {}
                if not episode_match and not (
                    guessed.get("season") and guessed.get("episode")
                ):
                    continue
                filename_season_number = (
                    int(episode_match.group("season"))
                    if episode_match
                    else int(guessed["season"])
                )
                guessed_episode = guessed.get("episode")
                episode_number = (
                    int(episode_match.group("episode"))
                    if episode_match
                    else int(
                        guessed_episode
                        if not isinstance(guessed_episode, list)
                        else guessed_episode[0]
                    )
                )
                end_number = (
                    int(episode_match.group("end"))
                    if episode_match and episode_match.group("end")
                    else None
                )
                episode_records.append(
                    (
                        episode_number,
                        relative(str(root), str(media)).casefold(),
                        media,
                        filename_season_number,
                        end_number,
                        [
                            sidecar_entry
                            for sidecar_entry in files_by_parent.get(media.parent, [])
                            if sidecar_entry[0] == media
                            or (
                                sidecar_entry[0].stem.startswith(media.stem)
                                and sidecar_entry[0].suffix.lower()
                                not in VIDEO_EXTENSIONS
                            )
                        ],
                    )
                )
            episode_records.sort(key=lambda value: (value[0], value[1]))
            if episode_records:
                plan.append((season_dir, season_folder_number, episode_records))
        return plan

    def _scan_movies(
        self,
        library_id: str,
        root: Path,
        job_id: str,
        should_terminate: Callable[[], bool],
        targets: set[str] | None = None,
    ) -> int:
        self._set_stage(
            job_id,
            "Enumerating movie roots",
            root=str(root),
            targets=sorted(targets) if targets else None,
        )
        enumeration_started = time.monotonic()
        entries = [
            path
            for path in self._target_entries(root, targets)
            if path.is_dir() or path.suffix.lower() in VIDEO_EXTENSIONS
        ]
        logger.info(
            "library scan root enumeration complete library_id=%s job_id=%s type=movies root=%s entries=%s duration_seconds=%.1f",
            library_id,
            job_id,
            root,
            len(entries),
            time.monotonic() - enumeration_started,
        )
        self.store.update_job(job_id, progress_total=len(entries))
        count = 0
        for entry in entries:
            self._check_termination(should_terminate)
            if entry.is_dir():
                files = []
                try:
                    for candidate in self._walk_file_entries(entry):
                        self._check_termination(should_terminate)
                        files.append(candidate)
                except OSError:
                    self._record_access_error(entry)
            else:
                try:
                    files = [(entry, entry.stat())]
                except OSError:
                    self._record_access_error(entry)
                    files = []
            relative_path = relative(str(root), str(entry))
            if self._root_has_access_error(entry):
                self._defer_root(relative_path, "media path could not be inspected")
                self.store.update_job(
                    job_id,
                    progress_current=count,
                    message=f"Deferred {entry.name}: media path inaccessible",
                )
                continue
            if not any(
                self._is_supported_video(path, file_stat) for path, file_stat in files
            ):
                self._reject_existing_entity(library_id, "movie", relative_path)
                self.store.update_job(
                    job_id,
                    progress_current=count,
                    message=f"Skipped {entry.name}: no playable video",
                )
                continue
            entity = self._entity(library_id, None, "movie", relative_path)
            discovered_ids = list(provider_ids(entry.name))
            for nfo in (
                path for path, _file_stat in files if path.suffix.lower() == ".nfo"
            ):
                discovered_ids.extend(parse_nfo_ids(nfo))
            if discovered_ids:
                self._replace_ids(entity, discovered_ids)
            file_delta = self._files(
                entity,
                root,
                files,
                job_id=job_id,
            )
            if not self.db.execute(
                "SELECT 1 FROM media_files WHERE entity_id=? AND role='media' LIMIT 1",
                (entity,),
            ):
                self._scan_rejected_ids.add(entity)
                continue
            self._scan_refresh_root_ids.add(entity)
            requires_materialization = (
                entity in self._scan_created_ids
                or file_delta["content_changed"]
                or entity in self._scan_provider_identity_changed
            )
            if requires_materialization:
                self._set_stage(
                    job_id,
                    f"Fetching metadata and artwork for {entry.name}",
                    entityId=entity,
                    path=str(entry),
                )
                future = metadata_root_executor.submit(
                    library_id,
                    self._resolve_movie_and_publish,
                    library_id,
                    (entity, "movie", relative(str(root), str(entry)), None, None),
                    job_id,
                    should_terminate,
                    count + 1,
                    len(entries),
                )
                self._await_metadata_futures([future], should_terminate)
            else:
                self._publish_root(entity)
            count += 1
            self.store.update_job(
                job_id, progress_current=count, message=f"Indexed {entry.name}"
            )
        self._scan_complete = True
        return count

    def _scan_series(
        self,
        library_id: str,
        root: Path,
        job_id: str,
        should_terminate: Callable[[], bool],
        resolve_immediately: bool = False,
        targets: set[str] | None = None,
    ) -> int:
        from app.providers import MetadataService

        self._set_stage(
            job_id,
            "Enumerating TV series roots",
            root=str(root),
            targets=sorted(targets) if targets else None,
        )
        enumeration_started = time.monotonic()
        series_dirs = sorted(
            (path for path in self._target_entries(root, targets) if path.is_dir()),
            key=lambda path: path.name.casefold(),
        )
        logger.info(
            "library scan root enumeration complete library_id=%s job_id=%s type=tv_series root=%s entries=%s duration_seconds=%.1f",
            library_id,
            job_id,
            root,
            len(series_dirs),
            time.monotonic() - enumeration_started,
        )
        self.store.update_job(job_id, progress_total=len(series_dirs))
        episode_count = 0
        series_count = 0
        # Defer provider-client construction until a root has passed the
        # playable-episode preflight; empty/unclassifiable roots do no
        # metadata work at all.
        service = None
        for series_index, series_dir in enumerate(series_dirs, start=1):
            self._check_termination(should_terminate)
            series_started = time.monotonic()
            self._set_stage(
                job_id,
                f"Indexing series {series_index}/{len(series_dirs)}: {series_dir.name}",
                seriesIndex=series_index,
                total=len(series_dirs),
                path=str(series_dir),
            )
            logger.info(
                "library scan series start library_id=%s job_id=%s series_index=%s series_total=%s path=%s",
                library_id,
                job_id,
                series_index,
                len(series_dirs),
                series_dir,
            )
            series_children = []
            try:
                for child in series_dir.iterdir():
                    self._check_termination(should_terminate)
                    series_children.append(child)
            except OSError as error:
                self._record_access_error(series_dir)
                self._defer_root(
                    relative(str(root), str(series_dir)),
                    f"series directory could not be enumerated: {error}",
                )
                self.store.update_job(
                    job_id,
                    progress_current=series_index,
                    message=f"Deferred {series_dir.name}: directory inaccessible",
                )
                continue
            episode_plan = self._series_episode_plan(
                root, series_dir, should_terminate, series_children
            )
            series_relative_path = relative(str(root), str(series_dir))
            if self._root_has_access_error(series_dir):
                self._defer_root(series_relative_path, "episode path could not be inspected")
                self.store.update_job(
                    job_id,
                    progress_current=series_index,
                    message=f"Deferred {series_dir.name}: media path inaccessible",
                )
                continue
            if not episode_plan:
                self._reject_existing_entity(library_id, "series", series_relative_path)
                self.store.update_job(
                    job_id,
                    progress_current=series_index,
                    message=f"Skipped {series_dir.name}: no playable episodes",
                )
                continue
            if resolve_immediately and service is None:
                service = MetadataService()
            series = self._entity(library_id, None, "series", series_relative_path)
            series_ids = provider_ids(series_dir.name)
            if series_ids:
                self._replace_ids(series, series_ids)
            series_metadata = None
            accepted_series_episodes = 0
            accepted_seasons: list[tuple[Path, int, str]] = []
            for season_dir, season_folder_number, episode_records in episode_plan:
                logger.info(
                    "library scan season start library_id=%s job_id=%s series_id=%s path=%s",
                    library_id,
                    job_id,
                    series,
                    season_dir,
                )
                season = self._entity(
                    library_id,
                    series,
                    "season",
                    relative(str(root), str(season_dir)),
                    season_number=season_folder_number,
                )
                accepted_season_episodes = 0
                for (
                    episode_number,
                    _relative_media_path,
                    media,
                    filename_season_number,
                    end_number,
                    episode_files,
                ) in episode_records:
                    self._check_termination(should_terminate)
                    # The directory establishes the season hierarchy. Keep the
                    # filename season only for loose episodes stored directly
                    # under the series directory.
                    episode_season_number = (
                        filename_season_number
                        if season_dir == series_dir
                        else season_folder_number
                    )
                    episode = self._entity(
                        library_id,
                        season,
                        "episode",
                        relative(str(root), str(media)),
                        season_number=episode_season_number,
                        episode_number=episode_number,
                        episode_end_number=end_number,
                    )
                    episode_ids = provider_ids(media.name)
                    if episode_ids:
                        self._replace_ids(episode, episode_ids)
                    self._files(
                        episode,
                        root,
                        episode_files,
                        job_id=job_id,
                    )
                    if not self.db.execute(
                        "SELECT 1 FROM media_files WHERE entity_id=? AND role='media' LIMIT 1",
                        (episode,),
                    ):
                        self._scan_rejected_ids.add(episode)
                        continue
                    accepted_season_episodes += 1
                    accepted_series_episodes += 1
                    episode_count += 1
                    if episode_count == 1 or episode_count % 10 == 0:
                        self.store.update_job(
                            job_id,
                            message=f"Scanning {series_dir.name}: {episode_count} episodes",
                        )
                        logger.info(
                            "library scan series progress library_id=%s job_id=%s series=%s episodes=%s current_path=%s",
                            library_id,
                            job_id,
                            series_dir.name,
                            episode_count,
                            media,
                        )
                logger.info(
                    "library scan season complete library_id=%s job_id=%s series_id=%s path=%s episodes_total=%s",
                    library_id,
                    job_id,
                    series,
                    season_dir,
                    episode_count,
                )
                if not accepted_season_episodes:
                    self._scan_rejected_ids.add(season)
                    continue
                accepted_seasons.append((season_dir, season_folder_number, season))
            root_video_stems = {
                path.stem
                for path in series_children
                if path.suffix.lower() in VIDEO_EXTENSIONS
            }
            self._files(
                series,
                root,
                [
                    path
                    for path in series_children
                    if path.is_file()
                    and path.suffix.lower() not in VIDEO_EXTENSIONS
                    and not any(
                        path.stem.startswith(video_stem)
                        for video_stem in root_video_stems
                    )
                ],
                job_id=job_id,
            )
            unseen_descendants = self.db.execute(
                "WITH RECURSIVE descendants(id) AS ("
                "SELECT id FROM library_entities WHERE parent_id=? "
                "UNION ALL SELECT e.id FROM library_entities e JOIN descendants d ON e.parent_id=d.id) "
                "SELECT id FROM descendants",
                (series,),
            )
            self._scan_rejected_ids.update(
                row[0]
                for row in unseen_descendants
                if row[0] not in self._scan_seen_ids
            )
            if not accepted_series_episodes:
                self._scan_rejected_ids.add(series)
                continue
            if service and series in self._metadata_candidates():
                self._set_stage(
                    job_id,
                    f"Starting metadata for {series_dir.name}",
                    seriesId=series,
                    path=str(series_dir),
                )
                try:
                    series_metadata = self._resolve_series_root(
                        library_id,
                        series,
                        series_relative_path,
                        service,
                        job_id,
                        should_terminate,
                    )
                except JobTerminated:
                    raise
                except Exception as error:
                    logger.exception(
                        "series root metadata failed library_id=%s series_id=%s error=%s",
                        library_id,
                        series,
                        error,
                    )
            tvdb_provider_id = next(
                (
                    row[0]
                    for row in self.db.execute(
                        "SELECT provider_id FROM entity_provider_ids WHERE entity_id=? AND provider='tvdb'",
                        (series,),
                    )
                ),
                None,
            )
            tvdb_identity = None
            if service:
                season_candidates = self._metadata_candidates()
                for season_dir, season_folder_number, season in accepted_seasons:
                    season_rows = self.db.execute(
                        "SELECT id FROM library_entities WHERE id=? OR parent_id=?",
                        (season, season),
                    )
                    if not any(
                        row[0] in season_candidates and self._needs_metadata(row[0])
                        for row in season_rows
                    ):
                        continue
                    if tvdb_identity is None and tvdb_provider_id:
                        try:
                            tvdb_identity = service.series_child_ids(
                                "tvdb", str(tvdb_provider_id)
                            )
                        except Exception as error:
                            logger.warning(
                                "TVDB child identity discovery failed series_id=%s provider_id=%s: %s",
                                series,
                                tvdb_provider_id,
                                error,
                            )
                    self._set_stage(
                        job_id,
                        f"Resolving season {season_folder_number} for {series_dir.name}",
                        seriesId=series,
                        seasonId=season,
                        path=str(season_dir),
                    )
                    try:
                        self._resolve_season_metadata(
                            library_id,
                            series,
                            season,
                            service,
                            job_id,
                            should_terminate,
                            series_metadata=series_metadata,
                            tvdb_identity=tvdb_identity,
                        )
                    except JobTerminated:
                        raise
                    except Exception as error:
                        logger.exception(
                            "season metadata failed; continuing library_id=%s series_id=%s season_id=%s path=%s error=%s",
                            library_id,
                            series,
                            season,
                            season_dir,
                            error,
                        )
                        self.store.update_job(
                            job_id,
                            message=f"Metadata failed for season {season_folder_number}; continuing",
                        )
            self._scan_refresh_root_ids.add(series)
            self._publish_root(series)
            series_count += 1
            self.store.update_job(
                job_id,
                progress_current=series_index,
                message=f"Indexed {series_dir.name} ({episode_count} episodes)",
            )
            logger.info(
                "library scan series complete library_id=%s job_id=%s series_id=%s path=%s episodes_total=%s duration_seconds=%.1f",
                library_id,
                job_id,
                series,
                series_dir,
                episode_count,
                time.monotonic() - series_started,
            )
        self._scan_complete = True
        return series_count

    def _scan_music(
        self,
        library_id: str,
        root: Path,
        job_id: str,
        should_terminate: Callable[[], bool],
        targets: set[str] | None = None,
    ) -> int:
        album_dirs = []

        def traversal_error(error):
            raise error

        scan_roots = [root] if targets is None else self._target_entries(root, targets)
        for scan_root in scan_roots:
            if not scan_root.is_dir():
                continue
            for directory, _, filenames in os.walk(scan_root, onerror=traversal_error):
                if any(
                    Path(name).suffix.lower() in AUDIO_EXTENSIONS for name in filenames
                ):
                    album_dirs.append(Path(directory))
        self.store.update_job(job_id, progress_total=len(album_dirs))
        count = 0
        artists: dict[str, str] = {}
        for album_dir in album_dirs:
            self._check_termination(should_terminate)
            artist_name = (
                album_dir.relative_to(root).parts[0]
                if album_dir.relative_to(root).parts
                else album_dir.name
            )
            artist = artists.get(artist_name)
            if not artist:
                artist = self._entity(library_id, None, "artist", artist_name)
                artists[artist_name] = artist
            release = self._entity(
                library_id, artist, "release", relative(str(root), str(album_dir))
            )
            tracks = [
                path
                for path in album_dir.iterdir()
                if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
            ]
            for track in sorted(tracks):
                self._check_termination(should_terminate)
                tags = parse_audio_tags(track)
                track_number = _int_tag(tags.get("TRACKNUMBER") or tags.get("TRACK"))
                disc_number = _int_tag(tags.get("DISCNUMBER") or tags.get("DISC"))
                entity = self._entity(
                    library_id,
                    release,
                    "track",
                    relative(str(root), str(track)),
                    disc_number=disc_number,
                    track_number=track_number,
                )
                music_ids = _music_ids(tags)
                if music_ids:
                    self._replace_ids(entity, music_ids)
                self._files(
                    entity,
                    root,
                    [track]
                    + [
                        sidecar
                        for sidecar in track.parent.iterdir()
                        if sidecar.is_file()
                        and sidecar.stem.startswith(track.stem)
                        and sidecar != track
                    ],
                )
                count += 1
            self._files(
                release,
                root,
                [
                    path
                    for path in album_dir.iterdir()
                    if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
                ],
            )
            self.store.update_job(
                job_id, progress_current=count, message=f"Indexed {album_dir.name}"
            )
        self._scan_complete = True
        return count

    def derive_collection(
        self,
        library_id: str,
        job_id: str,
        should_terminate: Callable[[], bool] | None = None,
    ) -> None:
        should_terminate = should_terminate or (lambda: False)
        library = self.store.get(library_id)
        if not library:
            raise ValueError("Library not found")
        sources = self.store.sources(library_id)
        self.store.set_scan_state(library_id, "scanning", started=now(), error=None)
        self.store.update_job(
            job_id, state="running", started_at=now(), message="Deriving collections"
        )
        self._scan_seen_ids = set()
        self._scan_created_ids = []
        self._scan_delta = {
            "added": set(),
            "changed": set(),
            "unchanged": set(),
            "removed": set(),
        }
        self._scan_refresh_root_ids = set()
        self._scan_complete = False
        try:
            from app.library_cleanup import cleanup_entities
            from app.providers import MetadataService, ProviderError, TVDBClient

            service = MetadataService()
            client = service.client("tvdb")
            if not isinstance(client, TVDBClient):
                raise ProviderError("TheTVDB is not configured")
            source_rows = (
                self.db.execute(
                    "SELECT e.id,e.entity_type,p.provider_id FROM library_entities e JOIN entity_provider_ids p ON p.entity_id=e.id WHERE e.library_id IN ({}) AND p.provider='tvdb' AND p.identifier_type IN ('series','movie')".format(
                        ",".join("?" * len(sources))
                    ),
                    sources,
                )
                if sources
                else []
            )
            by_id = {(row[1], str(row[2])): row[0] for row in source_rows}
            lists: dict[str, dict] = {}
            # Lists are paged; stop at the first short page to avoid unbounded calls.
            for page in range(50):
                self._check_termination(should_terminate)
                page_values = client.lists(page)
                for value in page_values:
                    if value.get("isOfficial"):
                        lists[str(value.get("id"))] = value
                if len(page_values) < 100:
                    break
            self.store.update_job(job_id, progress_total=len(lists))
            discovered: dict[str, dict] = {}
            for list_id, base in lists.items():
                self._check_termination(should_terminate)
                try:
                    payload = client.list_details(list_id)
                except ProviderError:
                    raise
                data = payload.get("data", payload)
                members = []
                for entity in data.get("entities", []) or []:
                    key = (
                        ("movie", str(entity.get("movieId")))
                        if entity.get("movieId")
                        else ("series", str(entity.get("seriesId")))
                        if entity.get("seriesId")
                        else None
                    )
                    if key and key in by_id:
                        members.append(by_id[key])
                members = list(dict.fromkeys(members))
                if len(members) < 2:
                    continue
                title = base.get("name") or data.get("name") or f"Collection {list_id}"
                discovered[list_id] = {"members": members, "title": title, "data": data}

            # Provider enumeration is complete at this point. Only now mutate
            # the collection inventory, so a partial/failing provider response
            # cannot erase the previous catalog.
            from app.metadata_services import MetadataIngestService

            ingest = MetadataIngestService(service)
            count = 0
            for list_id, value in discovered.items():
                self._check_termination(should_terminate)
                collection = self._entity(
                    library_id, None, "collection", f"tvdb-list-{list_id}"
                )
                self._scan_refresh_root_ids.add(collection)
                self._replace_ids(collection, [("tvdb", "collection", list_id)])
                current_members = [
                    (row[0], row[1])
                    for row in self.db.execute(
                        "SELECT source_entity_id,position FROM collection_members WHERE collection_entity_id=? ORDER BY position,source_entity_id",
                        (collection,),
                    )
                ]
                next_members = [
                    (source_entity, position)
                    for position, source_entity in enumerate(value["members"])
                ]
                if current_members != next_members:
                    self.db.execute(
                        "DELETE FROM collection_members WHERE collection_entity_id=?",
                        (collection,),
                    )
                    for source_entity, position in next_members:
                        self.db.execute(
                            "INSERT INTO collection_members(collection_entity_id,source_entity_id,position) VALUES(?,?,?)",
                            (collection, source_entity, position),
                        )
                    self._mark_changed(collection)
                collection_locales = ingest.locales()
                try:
                    ingest.ingest_locales(
                        "tvdb",
                        "collection",
                        list_id,
                        collection_locales,
                        force=False,
                    )
                except Exception:
                    for locale in collection_locales:
                        normalized = {
                            "title": value["title"],
                            "overview": value["data"].get("overview"),
                            "provider": "tvdb",
                            "providerId": list_id,
                            "images": [],
                        }
                        service.cache.put(
                            "tvdb", "collection", list_id, locale, normalized
                        )
                count += 1
                self.store.update_job(
                    job_id, progress_current=count, message=f"Derived {value['title']}"
                )
            stale = [
                row[0]
                for row in self.db.execute(
                    "SELECT e.id FROM library_entities e WHERE e.library_id=? AND e.entity_type='collection' AND e.id NOT IN ({})".format(
                        ",".join("?" * len(self._scan_seen_ids))
                        if self._scan_seen_ids
                        else "SELECT NULL"
                    ),
                    [library_id, *self._scan_seen_ids]
                    if self._scan_seen_ids
                    else [library_id],
                )
            ]
            if stale:
                cleanup_entities(self.db, stale)
                self._scan_delta["removed"].update(stale)
            self._refresh_catalog_after_cleanup(library_id)
            self._scan_complete = True
            self.store.update_job(
                job_id,
                state="completed",
                progress_current=count,
                progress_total=len(lists),
                finished_at=now(),
                message=f"Derived {count} official collections",
            )
            self.store.set_scan_state(library_id, "ready", finished=now())
        except JobTerminated:
            finished = now()
            self.store.update_job(
                job_id,
                state="terminated",
                message="Terminated by administrator",
                error=None,
                finished_at=finished,
            )
            self.store.set_scan_state(library_id, "ready", finished=finished)
        except Exception as error:
            summary = f"Collection derivation failed for library '{library_id}': {type(error).__name__}: {error}"
            details = {
                "libraryId": library_id,
                "jobId": job_id,
                "operation": "collection_derivation",
                "exception": type(error).__name__,
                "traceback": traceback.format_exc(),
            }
            logger.exception(
                "collection derivation failed library_id=%s job_id=%s",
                library_id,
                job_id,
            )
            self.store.update_job(
                job_id,
                state="failed",
                error=summary,
                error_details=json.dumps(details),
                finished_at=now(),
            )
            self.store.set_scan_state(
                library_id, "error", error=summary, finished=now()
            )
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
    def __init__(self, runtime: LibraryRuntime, library_id: str):
        self.runtime = runtime
        self.library_id = library_id

    def on_any_event(
        self, event
    ):  # watchdog emits separate create/modify/delete/move events
        # Directory events are important: a newly-created movie/series root
        # has no file event until its children arrive, and deleting a root
        # must remove the previously indexed inventory.  The scanner still
        # applies supported-media/admission filtering at reconciliation time.
        self.runtime.request_reconcile(
            self.library_id,
            getattr(event, "src_path", None),
            getattr(event, "dest_path", None),
        )


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
        # Compatibility buffers are only used when an older test/database has
        # not yet run migration 0024. Normal installations always use the
        # durable table below.
        self._reconcile_due: dict[str, float] = {}
        self._reconcile_targets: dict[str, set[str]] = {}
        self._job_targets: dict[str, set[str]] = {}
        self._job_target_revisions: dict[str, dict[str, int]] = {}
        self._active_jobs: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_lock = threading.RLock()
        self._root_locks: dict[tuple[str, str], threading.Lock] = {}
        self._root_locks_guard = threading.RLock()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._recover_active_jobs()
        self.stop_event.clear()
        self._configure_watchers()
        self.thread = threading.Thread(
            target=self._run, name="zenstream-library-jobs", daemon=True
        )
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

    def enqueue(
        self,
        library_id: str,
        kind: str = "scan",
        targets: set[str] | None = None,
    ) -> dict | None:
        # Full inventory work and watcher reconciliation are independent lanes.
        # There is at most one active job in each lane for a library, while a
        # full scan and a targeted reconcile may run together on disjoint roots.
        lane_kinds = (
            ("reconcile",)
            if kind == "reconcile"
            else (
                "scan",
                "collection_rebuild",
            )
        )
        with self.store.db.transaction() as cursor:
            # Filesystem events and the repair timer can race library deletion.
            # Check the parent row in the same transaction as the job insert so
            # a deleted library is simply ignored instead of violating the FK.
            cursor.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,))
            if not cursor.fetchone():
                return None
            cursor.execute(
                "SELECT id FROM library_jobs WHERE library_id=? AND kind IN (?,?) AND state IN ('queued','running','terminating') ORDER BY created_at DESC LIMIT 1",
                (library_id, lane_kinds[0], lane_kinds[-1]),
            )
            existing = cursor.fetchone()
            if existing:
                job_id = existing[0]
            else:
                job_id = new_id()
                cursor.execute(
                    "INSERT INTO library_jobs(id,library_id,kind,created_at) VALUES(?,?,?,?)",
                    (job_id, library_id, kind, now()),
                )
        if targets and kind == "reconcile":
            # Legacy callers may still pass an explicit target set.  Durable
            # watcher events use the table directly; this path keeps the
            # public enqueue API compatible for manual targeted scans/tests.
            self._job_targets[job_id] = set(targets)
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
                self.store.update_job(
                    job_id, state="terminating", message="Termination requested"
                )
            else:
                self.store.update_job(
                    job_id,
                    state="terminated",
                    message="Terminated by administrator",
                    error=None,
                    finished_at=finished,
                )
        with self.condition:
            self.condition.notify_all()
        return self.store.job(job_id)

    def terminate_library(self, library_id: str, timeout: float = 30.0) -> bool:
        """Stop all inventory jobs for a library before relationship cleanup.

        Deleting the parent row while a scanner is still writing entities can
        race SQLite foreign-key enforcement.  Cancellation is cooperative,
        so wait for workers to leave the active set and refuse deletion if a
        provider call does not return within the bounded timeout.
        """
        jobs = [
            job
            for job in self.store.jobs(library_id)
            if job and job["state"] in ACTIVE_JOB_STATES
        ]
        for job in jobs:
            self.terminate(job["id"])
        deadline = time.monotonic() + timeout
        while True:
            with self._active_lock:
                active = {
                    job_id
                    for job_id in self._active_jobs
                    if (self.store.job(job_id) or {}).get("libraryId") == library_id
                }
            if not active:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            with self.condition:
                self.condition.wait(timeout=min(0.25, remaining))

    def _recover_active_jobs(self) -> None:
        """Re-queue interrupted inventory jobs after an Orchestrator restart."""
        rows = self.store.db.execute(
            "SELECT id,library_id,state FROM library_jobs WHERE state IN ('queued','running','terminating') ORDER BY created_at DESC"
        )
        by_lane: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for job_id, library_id, state in rows:
            kind_row = self.store.db.execute(
                "SELECT kind FROM library_jobs WHERE id=?", (job_id,)
            )
            kind = kind_row[0][0] if kind_row else "scan"
            lane = "reconcile" if kind == "reconcile" else "full"
            by_lane.setdefault((library_id, lane), []).append((job_id, state))
        timestamp = now()
        with self.store.db.transaction() as cursor:
            touched_libraries: set[str] = set()
            for (library_id, _lane), jobs in by_lane.items():
                touched_libraries.add(library_id)
                keep_id = next(
                    (job_id for job_id, state in jobs if state != "terminating"),
                    None,
                )
                for job_id, state in jobs:
                    if job_id == keep_id:
                        cursor.execute(
                            "UPDATE library_jobs SET state='queued',progress_current=0,progress_total=0,message='Queued again after Orchestrator restart',error=NULL,error_details=NULL,started_at=NULL,finished_at=NULL WHERE id=?",
                            (job_id,),
                        )
                    else:
                        message = (
                            "Terminated during Orchestrator restart"
                            if state == "terminating"
                            else "Superseded by the active library job"
                        )
                        cursor.execute(
                            "UPDATE library_jobs SET state='terminated',message=?,error=NULL,finished_at=? WHERE id=?",
                            (message, timestamp, job_id),
                        )
            for library_id in touched_libraries:
                cursor.execute(
                    "UPDATE libraries SET scan_state='idle',scan_error=NULL,updated_at=? WHERE id=?",
                    (timestamp, library_id),
                )

    def request_reconcile(self, library_id: str, *paths: str | None) -> None:
        """Persist and debounce watcher changes into top-level reconciles."""
        library = self.store.get(library_id)
        if not library:
            return
        root = Path(library["directory"])
        targets: set[str] = set()
        for value in paths:
            if not value:
                continue
            try:
                relative_path = Path(value).relative_to(root)
            except ValueError:
                continue
            if relative_path.parts:
                targets.add(relative_path.parts[0])
            else:
                # An event on the library directory itself has no target root
                # to scope. Queue a full traversal as the safe fallback.
                self.enqueue(library_id, "scan")
        if not targets:
            return
        deadline = time.time() + 5
        timestamp = now()
        has_queue = bool(
            self.store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_reconcile_targets'"
            )
        )
        if not has_queue:
            queued = self._reconcile_targets.setdefault(library_id, set())
            for target in targets:
                queued.difference_update(
                    {
                        existing
                        for existing in queued
                        if _top_level_key(existing) == _top_level_key(target)
                    }
                )
                queued.add(target)
            self._reconcile_due[library_id] = time.monotonic() + 5
            with self.condition:
                self.condition.notify_all()
            return
        with self.store.db.transaction() as cursor:
            cursor.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,))
            if not cursor.fetchone():
                return
            existing_rows = cursor.execute(
                "SELECT top_level_root,revision FROM library_reconcile_targets WHERE library_id=?",
                (library_id,),
            ).fetchall()
            for target in targets:
                match = next(
                    (
                        row
                        for row in existing_rows
                        if _top_level_key(row[0]) == _top_level_key(target)
                    ),
                    None,
                )
                if match:
                    cursor.execute(
                        """
                        UPDATE library_reconcile_targets
                        SET top_level_root=?, debounce_until=?, event_count=event_count+1,
                            revision=revision+1, last_seen_at=?
                        WHERE library_id=? AND top_level_root=?
                        """,
                        (target, deadline, timestamp, library_id, match[0]),
                    )
                    existing_rows = [
                        (
                            target if row[0] == match[0] else row[0],
                            row[1] + 1 if row[0] == match[0] else row[1],
                        )
                        for row in existing_rows
                    ]
                else:
                    cursor.execute(
                        """
                        INSERT INTO library_reconcile_targets
                            (library_id,top_level_root,debounce_until,event_count,revision,first_seen_at,last_seen_at)
                        VALUES(?,?,?,1,1,?,?)
                        """,
                        (library_id, target, deadline, timestamp, timestamp),
                    )
                    existing_rows.append((target, 1))
        with self.condition:
            self.condition.notify_all()

    def _root_lock(self, library_id: str, root: str) -> threading.Lock:
        key = (library_id, _top_level_key(root))
        with self._root_locks_guard:
            return self._root_locks.setdefault(key, threading.Lock())

    def _acquire_roots(self, library_id: str, roots: set[str]):
        locks = [
            self._root_lock(library_id, root)
            for root in sorted(roots, key=_top_level_key)
        ]
        for lock in locks:
            lock.acquire()
        return locks

    def _aggregate_scan_state(self, library_id: str) -> None:
        active = self.store.db.execute(
            "SELECT 1 FROM library_jobs WHERE library_id=? AND state IN ('queued','running','terminating') LIMIT 1",
            (library_id,),
        )
        if active:
            self.store.set_scan_state(library_id, "scanning", error=None)

    def _configure_watchers(self) -> None:
        if Observer is None:
            return
        observer = Observer()
        for library in self.store.list():
            directory = library.get("directory")
            if (
                not library.get("watchEnabled")
                or not directory
                or not os.path.isdir(directory)
            ):
                continue
            try:
                observer.schedule(
                    _LibraryChangeHandler(self, library["id"]),
                    directory,
                    recursive=True,
                )
                self._watch_paths.add(directory)
            except OSError:
                continue
        if self._watch_paths:
            observer.start()
            self.observer = observer

    def _schedule_repairs(self) -> None:
        now_epoch = time.time()
        for library in self.store.list():
            if library["type"] == "collection" or not library.get(
                "scanIntervalMinutes"
            ):
                continue
            finished = library.get("lastScanFinishedAt")
            try:
                due = (
                    not finished
                    or datetime.fromisoformat(finished).timestamp()
                    + library["scanIntervalMinutes"] * 60
                    <= now_epoch
                )
            except (TypeError, ValueError, OSError):
                due = True
            unresolved = self.store.db.execute(
                "SELECT COUNT(*) FROM library_entities WHERE library_id=? AND match_status IN ('unresolved','failed')",
                (library["id"],),
            )[0][0]
            due = due or (bool(unresolved) and not finished)
            if due:
                self.enqueue(library["id"], "scan")

    def _run(self):
        while not self.stop_event.is_set():
            has_queue = bool(
                self.store.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_reconcile_targets'"
                )
            )
            if has_queue:
                due_rows = self.store.db.execute(
                    "SELECT DISTINCT library_id FROM library_reconcile_targets WHERE debounce_until<=?",
                    (time.time(),),
                )
                for (library_id,) in due_rows:
                    self.enqueue(library_id, "reconcile")
            else:
                due = [
                    library_id
                    for library_id, deadline in self._reconcile_due.items()
                    if time.monotonic() >= deadline
                ]
                for library_id in due:
                    self.enqueue(
                        library_id,
                        "reconcile",
                        self._reconcile_targets.get(library_id, set()).copy(),
                    )
                    self._reconcile_due.pop(library_id, None)
            rows = self.store.db.execute(
                "SELECT id,library_id,kind FROM library_jobs WHERE state='queued' ORDER BY created_at LIMIT 1"
            )
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
                    cursor.execute(
                        "UPDATE library_jobs SET state='running',started_at=?,message='Starting scan' WHERE id=? AND state='queued'",
                        (now(), job_id),
                    )
                    claimed = cursor.rowcount == 1
                if not claimed:
                    self._active_jobs.discard(job_id)
                    self._cancel_events.pop(job_id, None)
                    continue
            threading.Thread(
                target=self._execute_job,
                args=(job_id, library_id, kind),
                name=f"zenstream-library-{job_id[:8]}",
                daemon=True,
            ).start()

    def _execute_job(self, job_id: str, library_id: str, kind: str) -> None:
        locks: list[threading.Lock] = []
        try:
            if kind in {"scan", "reconcile", "collection_rebuild"}:
                targets = self._job_targets.pop(job_id, None)
                target_revisions: dict[str, int] = {}
                if kind == "reconcile" and targets is None:
                    has_queue = bool(
                        self.store.db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_reconcile_targets'"
                        )
                    )
                    if has_queue:
                        rows = self.store.db.execute(
                            "SELECT top_level_root,revision FROM library_reconcile_targets WHERE library_id=? AND debounce_until<=? ORDER BY top_level_root",
                            (library_id, time.time()),
                        )
                        targets_by_key: dict[str, str] = {}
                        for row in rows:
                            targets_by_key[_top_level_key(row[0])] = row[0]
                        targets = set(targets_by_key.values())
                        target_revisions = {row[0]: int(row[1]) for row in rows}
                    else:
                        targets = self._reconcile_targets.pop(library_id, set())
                    self._job_target_revisions[job_id] = target_revisions
                if kind == "reconcile" and not targets:
                    # A newer watcher event may have postponed every target
                    # after the job was queued. Complete this stale job without
                    # invoking a scanner or unscoped cleanup.
                    self.store.update_job(
                        job_id,
                        state="completed",
                        progress_current=0,
                        progress_total=0,
                        finished_at=now(),
                        message="No due watcher targets",
                    )
                    return
                if kind == "reconcile" and targets:
                    locks = self._acquire_roots(library_id, targets)
                # A scan owns mutable traversal state and diagnostics.  Do not
                # share one scanner between concurrent library workers: a movie
                # scan could otherwise overwrite a TV scan's stage, delta, and
                # heartbeat message.
                scanner = LibraryScanner(self.store)
                scanner.scan(
                    library_id,
                    job_id,
                    self._cancel_events[job_id].is_set,
                    targets=targets if kind == "reconcile" else None,
                )
            else:
                self.store.update_job(
                    job_id,
                    state="failed",
                    error=f"Unsupported job kind: {kind}",
                    finished_at=now(),
                )
        except Exception:
            # The scanner records the durable error; keep the worker alive for later jobs.
            logger.exception(
                "library worker failed job_id=%s library_id=%s kind=%s",
                job_id,
                library_id,
                kind,
            )
        finally:
            for lock in reversed(locks):
                lock.release()
            revisions = getattr(self, "_job_target_revisions", {}).pop(job_id, {})
            if revisions:
                with self.store.db.transaction() as cursor:
                    for target, revision in revisions.items():
                        cursor.execute(
                            "DELETE FROM library_reconcile_targets WHERE library_id=? AND top_level_root=? AND revision=?",
                            (library_id, target, revision),
                        )
            with self._active_lock:
                self._active_jobs.discard(job_id)
                self._cancel_events.pop(job_id, None)
            self._aggregate_scan_state(library_id)
            with self.condition:
                self.condition.notify_all()


runtime = LibraryRuntime()
