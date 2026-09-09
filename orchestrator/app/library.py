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
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import Future, as_completed
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty

from app.config import Config
from app.images import LocalArtworkCache, blurhash_for_image
from app.language_registry import language_options, normalize_track_language
from app.local_metadata import (
    NFO_EXTENSIONS,
    parse_nfo_ids,
    parse_nfo_metadata,
)
from app.logging_config import get_logger
from app.metadata_domain import clean_music_title, music_filename_parts
from app.progress import WholeJobProgress
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
LANGUAGE_NAMES = {
    str(option["value"]): str(option["label"]) for option in language_options()
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
WATCHER_RECONCILE_DEBOUNCE_SECONDS = 5.0
WATCHER_RECONCILE_FLUSH_INTERVAL_SECONDS = 1.0
MUSIC_FULL_RECONCILE_TARGET = "__zenstream_music_full__"
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


def _audio_inventory_fingerprint(file_size: int, modified_ns: int) -> str:
    """Create a no-I/O first-admission fingerprint for audio media.

    A full audio quick fingerprint reads up to two MiB per file.  On a large
    music library that is an unnecessary multi-gigabyte read because Mutagen
    already opens the file for tags and later probes it for duration.  Size and
    mtime still let the next watcher event trigger a real content check.
    """
    return hashlib.sha256(
        f"audio-inventory:{int(file_size)}:{int(modified_ns)}".encode("ascii")
    ).hexdigest()


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


_AUDIO_TAG_ALIASES = {
    "NAM": "TITLE",
    "TALB": "ALBUM",
    "ALBUM": "ALBUM",
    "ALB": "ALBUM",
    "TPE1": "ARTIST",
    "ARTIST": "ARTIST",
    "ART": "ARTIST",
    "ARTISTS": "ARTIST",
    "PERFORMER": "ARTIST",
    "PERFORMERS": "ARTIST",
    "PERFORMER NAME": "ARTIST",
    "VOCALIST": "ARTIST",
    "SINGER": "ARTIST",
    "TPE2": "ALBUMARTIST",
    "ALBUMARTIST": "ALBUMARTIST",
    "ALBUMARTISTS": "ALBUMARTIST",
    "ALBUM ARTIST": "ALBUMARTIST",
    "ALBUM ARTISTS": "ALBUMARTIST",
    "AART": "ALBUMARTIST",
    "TIT2": "TITLE",
    "TITLE": "TITLE",
    "DAY": "DATE",
    "TDRC": "DATE",
    "TYER": "DATE",
    "TDRL": "DATE",
    "TDOR": "DATE",
    "DATE": "DATE",
    "YEAR": "DATE",
    "DURATION": "DURATIONSECONDS",
    "DURATIONSECONDS": "DURATIONSECONDS",
    "LENGTH": "DURATIONSECONDS",
    "TRCK": "TRACKNUMBER",
    "TRKN": "TRACKNUMBER",
    "TRACK": "TRACKNUMBER",
    "TRACKNUMBER": "TRACKNUMBER",
    "TPOS": "DISCNUMBER",
    "DISK": "DISCNUMBER",
    "DISC": "DISCNUMBER",
    "DISCNUMBER": "DISCNUMBER",
    "MUSICBRAINZ ARTIST ID": "MUSICBRAINZ_ARTISTID",
    "MUSICBRAINZ ARTISTID": "MUSICBRAINZ_ARTISTID",
    "MUSICBRAINZ ALBUM ARTIST ID": "MUSICBRAINZ_ALBUMARTISTID",
    "MUSICBRAINZ ALBUM ARTISTID": "MUSICBRAINZ_ALBUMARTISTID",
    "MUSICBRAINZ ALBUM ID": "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ ALBUMID": "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ RELEASE ID": "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ RELEASEID": "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ RELEASE GROUP ID": "MUSICBRAINZ_RELEASEGROUPID",
    "MUSICBRAINZ RELEASE GROUPID": "MUSICBRAINZ_RELEASEGROUPID",
    "MUSICBRAINZ RELEASEGROUPID": "MUSICBRAINZ_RELEASEGROUPID",
    "MUSICBRAINZ TRACK ID": "MUSICBRAINZ_TRACKID",
    "MUSICBRAINZ TRACKID": "MUSICBRAINZ_TRACKID",
    "MUSICBRAINZ RECORDING ID": "MUSICBRAINZ_TRACKID",
    "MUSICBRAINZ RECORDINGID": "MUSICBRAINZ_TRACKID",
    "MUSICBRAINZ RELEASE TRACK ID": "MUSICBRAINZ_RELEASETRACKID",
    "MUSICBRAINZ RELEASE TRACKID": "MUSICBRAINZ_RELEASETRACKID",
    "MUSICBRAINZ RELEASETRACKID": "MUSICBRAINZ_RELEASETRACKID",
    "MUSICBRAINZ WORK ID": "MUSICBRAINZ_WORKID",
    "MUSICBRAINZ WORKID": "MUSICBRAINZ_WORKID",
    "ALBUM TYPE": "ALBUMTYPE",
    "ALBUMTYPE": "ALBUMTYPE",
    "ALBUM TYPES": "ALBUMTYPES",
    "ALBUMTYPES": "ALBUMTYPES",
    "ALBUM SECONDARY TYPES": "ALBUMSECONDARYTYPES",
    "ALBUMSECONDARYTYPES": "ALBUMSECONDARYTYPES",
    "RELEASE TYPE": "ALBUMTYPE",
    "RELEASETYPE": "ALBUMTYPE",
    "RELEASE TYPES": "ALBUMTYPES",
    "RELEASETYPES": "ALBUMTYPES",
    "MUSICBRAINZ ALBUM TYPE": "ALBUMTYPE",
    "MUSICBRAINZ ALBUMTYPE": "ALBUMTYPE",
    "MUSICBRAINZ ALBUM TYPES": "ALBUMTYPES",
    "MUSICBRAINZ ALBUMTYPES": "ALBUMTYPES",
    "MUSICBRAINZ RELEASE TYPE": "ALBUMTYPE",
    "MUSICBRAINZ RELEASETYPE": "ALBUMTYPE",
    "MUSICBRAINZ RELEASE TYPES": "ALBUMTYPES",
    "MUSICBRAINZ RELEASETYPES": "ALBUMTYPES",
    "ALBUM VERSION": "ALBUMVERSION",
    "ALBUMVERSION": "ALBUMVERSION",
    "RELEASE VERSION": "ALBUMVERSION",
    "RELEASEVERSION": "ALBUMVERSION",
    "MUSICBRAINZ ALBUM VERSION": "ALBUMVERSION",
    "MUSICBRAINZ ALBUMVERSION": "ALBUMVERSION",
}
_AUDIO_ID_TAGS = {
    "MUSICBRAINZ_ARTISTID",
    "MUSICBRAINZ_ALBUMARTISTID",
    "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ_RELEASEGROUPID",
    "MUSICBRAINZ_TRACKID",
    "MUSICBRAINZ_RELEASETRACKID",
    "MUSICBRAINZ_WORKID",
}
_AUDIO_MULTI_TAGS = {"ARTIST", "ALBUMARTIST", "ALBUMTYPES", "ALBUMSECONDARYTYPES"}
_AUDIO_ARTIST_TAG_PRIORITIES = {
    # Mutagen exposes structured Vorbis/MP4 artist lists as plural fields.
    # They must win over a joined scalar alias regardless of tag iteration
    # order.
    "ARTISTS": 0,
    "PERFORMERS": 0,
    "PERFORMER NAME": 0,
    "VOCALIST": 0,
    "SINGER": 0,
    "TPE1": 1,
    "ARTIST": 1,
    "ART": 1,
    "PERFORMER": 1,
}
_AUDIO_ALBUM_ARTIST_TAG_PRIORITIES = {
    "ALBUMARTISTS": 0,
    "ALBUM ARTISTS": 0,
    "TPE2": 1,
    "ALBUMARTIST": 1,
    "ALBUM ARTIST": 1,
    "AART": 1,
}

MUSIC_TAG_SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class AudioInventory:
    tags: dict[str, str]
    probe: dict | None = None


class AudioTags(dict[str, str]):
    def __init__(self, tags: dict[str, str], probe: dict | None = None):
        super().__init__(tags)
        self.probe = probe


@dataclass(frozen=True)
class MusicFileObservation:
    relative_path: str
    file_stat: os.stat_result
    probe: dict | None
    changed: bool
    previous_group_key: tuple[str, ...] | None = None
    cached_entity_id: str | None = None


def _music_tag_fingerprint(tags: dict[str, str]) -> str:
    payload = json.dumps(
        tags,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _music_group_key_text(key: tuple[str, ...]) -> str:
    return json.dumps(list(key), ensure_ascii=False, separators=(",", ":"))


def _music_group_key_from_text(value: str | None) -> tuple[str, ...] | None:
    try:
        decoded = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, list):
        return None
    return tuple(str(part) for part in decoded)


def _audio_raw_tag_key(raw_key: object) -> str:
    key = str(raw_key).strip().upper()
    if ":" in key:
        # Mutagen exposes MP4 freeform tags as
        # ``----:com.apple.iTunes:MusicBrainz ...``. The final component is
        # the semantic tag name; the preceding namespace is not part of the
        # catalog field.
        key = key.rsplit(":", 1)[-1].strip()
    key = key.replace("©", "")
    key = re.sub(r"[._-]+", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    return key


def _audio_tag_key(raw_key: object) -> str:
    key = _audio_raw_tag_key(raw_key)
    return _AUDIO_TAG_ALIASES.get(key, key.replace(" ", "_"))


def _audio_tag_values(raw_value: object) -> list[str]:
    value = getattr(raw_value, "text", raw_value)
    if isinstance(value, (list, tuple)):
        values = value
    else:
        values = [value]
    result = []
    for item in values:
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="replace")
        text = str(item or "").strip()
        if text:
            result.append(text)
    return list(dict.fromkeys(result))


def _normalize_audio_tags(audio: object) -> dict[str, str]:
    if audio is None or not getattr(audio, "tags", None):
        return {}
    collected: dict[str, list[tuple[str, list[str]]]] = {}
    for raw_key, raw_value in audio.tags.items():
        raw_name = _audio_raw_tag_key(raw_key)
        key = _AUDIO_TAG_ALIASES.get(raw_name, raw_name.replace(" ", "_"))
        values = _audio_tag_values(raw_value)
        if not values:
            continue
        collected.setdefault(key, []).append((raw_name, values))
    tags: dict[str, str] = {}
    for key, entries in collected.items():
        if key in _AUDIO_ID_TAGS or key in _AUDIO_MULTI_TAGS:
            if key == "ARTIST":
                priority = min(
                    _AUDIO_ARTIST_TAG_PRIORITIES.get(raw_name, 2)
                    for raw_name, _ in entries
                )
                selected = [
                    values
                    for raw_name, values in entries
                    if _AUDIO_ARTIST_TAG_PRIORITIES.get(raw_name, 2) == priority
                ]
            elif key == "ALBUMARTIST":
                priority = min(
                    _AUDIO_ALBUM_ARTIST_TAG_PRIORITIES.get(raw_name, 2)
                    for raw_name, _ in entries
                )
                selected = [
                    values
                    for raw_name, values in entries
                    if _AUDIO_ALBUM_ARTIST_TAG_PRIORITIES.get(raw_name, 2) == priority
                ]
            else:
                selected = [values for _, values in entries]
            values = list(
                dict.fromkeys(
                    value for entry_values in selected for value in entry_values
                )
            )
            if values:
                tags[key] = ";".join(values)
        else:
            # Aliases are allowed to coexist in a file. Keep the first
            # usable scalar deterministically instead of letting the
            # final Mutagen tag overwrite it.
            tags[key] = entries[0][1][0]
    if "DURATIONSECONDS" not in tags:
        length = getattr(getattr(audio, "info", None), "length", None)
        if length is not None:
            try:
                if float(length) >= 0:
                    tags["DURATIONSECONDS"] = str(float(length))
            except (TypeError, ValueError):
                pass
    return tags


def parse_audio_inventory(path: Path) -> AudioInventory:
    try:
        from app.media_probe import audio_probe_from_mutagen
        from mutagen import File

        audio = File(path, easy=False)
        return AudioInventory(
            _normalize_audio_tags(audio),
            audio_probe_from_mutagen(audio, path),
        )
    except Exception:
        return AudioInventory({})


def parse_audio_tags(path: Path) -> dict[str, str]:
    inventory = parse_audio_inventory(path)
    return AudioTags(inventory.tags, inventory.probe)


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
    if suffix in NFO_EXTENSIONS:
        return "metadata"
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
        normalized = normalize_track_language(candidate)
        if normalized:
            return normalized
    return None


def _sidecar_suffix_tokens(value: str) -> list[str]:
    # Periods delimit descriptor, marker, and language components. Keep
    # hyphens intact because they are part of BCP-47 language tags such as
    # ``zh-TW`` and may also be part of a descriptor.
    return [token.strip() for token in value.split(".") if token.strip()]


def sidecar_media_path(
    sidecar_path: str | Path, media_paths: Iterable[str | Path]
) -> Path | None:
    """Return the longest media path whose filename owns this sidecar."""
    sidecar = Path(sidecar_path)
    sidecar_stem = sidecar.stem
    normalized_sidecar = sidecar_stem.casefold()
    matches: list[Path] = []
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
        matches.append(media_path)
    return max(matches, key=lambda path: len(path.stem)) if matches else None


def sidecar_descriptor(
    sidecar_path: str | Path, media_paths: Iterable[str | Path]
) -> str | None:
    sidecar = Path(sidecar_path)
    sidecar_stem = sidecar.stem
    matching_media = sidecar_media_path(sidecar, media_paths)
    if matching_media is None:
        return None
    matching_stem = matching_media.stem

    remainder = sidecar_stem[len(matching_stem) :].lstrip(" ._-\t")
    tokens = _sidecar_suffix_tokens(remainder)
    while tokens and tokens[-1].casefold() in LANGUAGE_MARKERS:
        tokens.pop()
    if tokens:
        candidate = tokens[-1]
        lowered = candidate.casefold()
        if normalize_track_language(candidate):
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
        self._progress: dict[str, WholeJobProgress] = {}

    def _library_sort_order_sql(self) -> str:
        """Return the sort-order column when the current schema supports it."""
        if not hasattr(self, "_library_columns"):
            self._library_columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(libraries)")
            }
        return "sort_order" if "sort_order" in self._library_columns else "0"

    @staticmethod
    def _normalize_sort_order(value, default: int = 0) -> int:
        if value is None or value == "":
            return default
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("sortOrder must be an integer.") from error

    def begin_progress(self, job_id: str, kind: str) -> None:
        if not hasattr(self, "_progress"):
            self._progress = {}
        self._progress[job_id] = WholeJobProgress(kind)

    def end_progress(self, job_id: str) -> None:
        getattr(self, "_progress", {}).pop(job_id, None)

    def list(self) -> list[dict]:
        sort_order = self._library_sort_order_sql()
        rows = self.db.execute(
            f"SELECT id,name,type,{sort_order} AS sort_order,directory,watch_enabled,scan_interval_minutes,scan_state,scan_error,last_scan_started_at,last_scan_finished_at,created_at,updated_at FROM libraries ORDER BY COALESCE({sort_order},0) DESC,name COLLATE NOCASE,id"
        )
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row[0],
            "name": row[1],
            "type": row[2],
            "sortOrder": int(row[3] or 0),
            "directory": row[4],
            "watchEnabled": bool(row[5]),
            "scanIntervalMinutes": row[6],
            "scanState": row[7],
            "scanError": row[8],
            "lastScanStartedAt": row[9],
            "lastScanFinishedAt": row[10],
            "createdAt": row[11],
            "updatedAt": row[12],
        }

    def get(self, library_id: str) -> dict | None:
        sort_order = self._library_sort_order_sql()
        rows = self.db.execute(
            f"SELECT id,name,type,{sort_order} AS sort_order,directory,watch_enabled,scan_interval_minutes,scan_state,scan_error,last_scan_started_at,last_scan_finished_at,created_at,updated_at FROM libraries WHERE id=?",
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
        sort_order: int | None = None,
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
        supports_sort_order = self._library_sort_order_sql() == "sort_order"
        if sort_order is not None:
            sort_order = self._normalize_sort_order(sort_order)
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
            if supports_sort_order:
                if sort_order is None:
                    row = cursor.execute(
                        "SELECT MIN(sort_order) FROM libraries"
                    ).fetchone()
                    sort_order = int(row[0]) - 1 if row and row[0] is not None else 0
                cursor.execute(
                    "INSERT INTO libraries(id,name,type,sort_order,directory,watch_enabled,scan_interval_minutes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        library_id,
                        name,
                        library_type,
                        sort_order,
                        directory,
                        int(watch_enabled),
                        interval,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
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

    def move(self, library_id: str, direction: str) -> dict:
        if direction not in {"up", "down"}:
            raise ValueError("direction must be 'up' or 'down'.")
        current = self.get(library_id)
        if not current:
            raise KeyError("Library not found")
        if self._library_sort_order_sql() != "sort_order":
            raise ValueError("Library ordering is unavailable until migrations finish.")

        with self.db.transaction() as cursor:
            rows = cursor.execute(
                "SELECT id FROM libraries ORDER BY COALESCE(sort_order,0) DESC,name COLLATE NOCASE,id"
            ).fetchall()
            ordered_ids = [row[0] for row in rows]
            index = ordered_ids.index(library_id)
            target = index - 1 if direction == "up" else index + 1
            if target < 0 or target >= len(ordered_ids):
                return current
            ordered_ids[index], ordered_ids[target] = (
                ordered_ids[target],
                ordered_ids[index],
            )
            timestamp = now()
            for position, ordered_id in enumerate(ordered_ids):
                cursor.execute(
                    "UPDATE libraries SET sort_order=?,updated_at=? WHERE id=?",
                    (len(ordered_ids) - position, timestamp, ordered_id),
                )
        return self.get(library_id)  # type: ignore[return-value]

    def update(self, library_id: str, values: dict) -> dict:
        current = self.get(library_id)
        if not current:
            raise KeyError("Library not found")
        sort_order = self._normalize_sort_order(
            values.get("sortOrder", current.get("sortOrder", 0))
        )
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
        if "sort_order" in self._library_columns:
            self.db.execute(
                "UPDATE libraries SET name=?,sort_order=?,directory=?,watch_enabled=?,scan_interval_minutes=?,updated_at=? WHERE id=?",
                (
                    name,
                    sort_order,
                    directory,
                    watch_enabled,
                    interval,
                    now(),
                    library_id,
                ),
            )
        else:
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
        try:
            columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(library_jobs)")
            }
        except Exception:
            columns = set()
        detail_columns = [
            name if name in columns else f"NULL AS {name}"
            for name in (
                "progress_phase",
                "progress_label",
                "progress_stage_current",
                "progress_stage_total",
                "progress_stage_unit",
                "progress_current_item",
            )
        ]
        rows = self.db.execute(
            "SELECT id,library_id,kind,state,progress_current,progress_total,message,error,error_details,created_at,started_at,finished_at,"
            + ",".join(detail_columns)
            + " FROM library_jobs WHERE id=?",
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
        detail_values = row[12:18]
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
            "progressDetail": (
                {
                    "phase": detail_values[0] or "processing",
                    "label": detail_values[1] or "Working",
                    "current": detail_values[2],
                    "total": detail_values[3],
                    "unit": detail_values[4],
                    "item": detail_values[5],
                }
                if any(value is not None for value in detail_values)
                else None
            ),
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
        tracker = getattr(self, "_progress", {}).get(job_id)
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
            "progress_phase",
            "progress_label",
            "progress_stage_current",
            "progress_stage_total",
            "progress_stage_unit",
            "progress_current_item",
        }
        try:
            columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(library_jobs)")
            }
            allowed = {key for key in allowed if key in columns}
        except Exception:
            pass
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
            "metadata_changed": set(),
            "artwork_changed": set(),
            "unchanged": set(),
            "removed": set(),
        }
        self._scan_provider_identity_changed: set[str] = set()
        self._scan_lastfm_attempted_ids: set[str] = set()
        self._scan_rejected_ids: set[str] = set()
        self._scan_reconciled_ids: set[str] = set()
        self._scan_deferred_roots: set[str] = set()
        self._scan_access_errors: set[Path] = set()
        self._scan_refresh_root_ids: set[str] = set()
        self._music_local_metadata: dict[str, dict] = {}
        self._local_nfo_sources: dict[str, tuple[str, Path, list]] = {}
        self._music_pending_release_ids: dict[str, set[str]] = {}
        self._music_release_conflicts: set[str] = set()
        self._music_file_observations: dict[str, MusicFileObservation] = {}
        self._music_dirty_group_keys: set[tuple[str, ...]] = set()
        self._music_dirty_release_ids: set[str] = set()
        self._music_directory_cache: dict[Path, list[Path] | None] = {}
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
        self._music_inventory_available: bool | None = None

    def _has_table(self, name: str) -> bool:
        return bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            )
        )

    def _music_inventory_enabled(self) -> bool:
        if self._music_inventory_available is None:
            self._music_inventory_available = self._has_table("music_file_inventory")
        return self._music_inventory_available

    def _music_inventory_rows(
        self,
        library_id: str,
        targets: set[str] | None = None,
    ) -> dict[str, tuple]:
        if not self._music_inventory_enabled():
            return {}
        query = (
            "SELECT path_key,relative_path,entity_id,size,modified_ns,"
            "tag_snapshot_version,tag_fingerprint,tag_payload,group_key "
            "FROM music_file_inventory WHERE library_id=?"
        )
        params: list[str] = [library_id]
        if targets:
            clauses = []
            for target in sorted({_top_level_key(value) for value in targets}):
                escaped = (
                    target.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                clauses.append("(path_key=? OR path_key LIKE ? ESCAPE '\\')")
                params.extend((target, f"{escaped}/%"))
            query += " AND (" + " OR ".join(clauses) + ")"
        rows = self.db.execute(query + " ORDER BY path_key", params)
        return {str(row[0]): tuple(row[1:]) for row in rows}

    def _music_inventory_lookup(
        self,
        library_id: str,
        path_key: str,
        file_stat: os.stat_result,
    ) -> tuple[dict[str, str], tuple[str, ...], bool, str | None] | None:
        if not self._music_inventory_enabled():
            return None
        rows = self.db.execute(
            "SELECT entity_id,size,modified_ns,tag_snapshot_version,tag_payload,group_key "
            "FROM music_file_inventory WHERE library_id=? AND path_key=?",
            (library_id, path_key),
        )
        if not rows:
            return None
        entity_id, size, modified_ns, version, payload, group_value = rows[0]
        previous_group = _music_group_key_from_text(group_value)
        if previous_group is None:
            return None
        try:
            tags = json.loads(payload or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(tags, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in tags.items()
        ):
            return None
        unchanged = (
            int(size or 0) == int(file_stat.st_size)
            and int(modified_ns or 0) == int(file_stat.st_mtime_ns)
            and int(version or 0) == MUSIC_TAG_SNAPSHOT_VERSION
        )
        return tags, previous_group, unchanged, str(entity_id) if entity_id else None

    def _music_inventory_upsert(
        self,
        library_id: str,
        root: Path,
        path: Path,
        file_stat: os.stat_result,
        tags: dict[str, str],
        group_key: tuple[str, ...],
        entity_id: str | None = None,
    ) -> None:
        if not self._music_inventory_enabled():
            return
        relative_path = relative(str(root), str(path))
        path_key = _path_key(relative_path)
        encoded_tags = json.dumps(
            tags,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.db.execute(
            "INSERT INTO music_file_inventory("
            "library_id,path_key,relative_path,entity_id,size,modified_ns,"
            "tag_snapshot_version,tag_fingerprint,tag_payload,group_key,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(library_id,path_key) DO UPDATE SET "
            "relative_path=excluded.relative_path,"
            "entity_id=COALESCE(excluded.entity_id,music_file_inventory.entity_id),"
            "size=excluded.size,modified_ns=excluded.modified_ns,"
            "tag_snapshot_version=excluded.tag_snapshot_version,"
            "tag_fingerprint=excluded.tag_fingerprint,"
            "tag_payload=excluded.tag_payload,group_key=excluded.group_key,"
            "updated_at=excluded.updated_at",
            (
                library_id,
                path_key,
                relative_path,
                entity_id,
                int(file_stat.st_size),
                int(file_stat.st_mtime_ns),
                MUSIC_TAG_SNAPSHOT_VERSION,
                _music_tag_fingerprint(tags),
                encoded_tags,
                _music_group_key_text(group_key),
                now(),
            ),
        )

    def _music_inventory_prune(
        self,
        library_id: str,
        current_paths: set[str],
        previous_rows: dict[str, tuple],
        targets: set[str] | None,
    ) -> None:
        if not self._music_inventory_enabled():
            return
        normalized_deferred = {
            _top_level_key(_path_key(value)) for value in self._scan_deferred_roots
        }
        for path_key, row in previous_rows.items():
            if path_key in current_paths:
                continue
            relative_path = str(row[0] or "")
            top_level = _top_level_key(_path_key(relative_path))
            if top_level in normalized_deferred:
                continue
            if targets and top_level not in {
                _top_level_key(target) for target in targets
            }:
                continue
            self.db.execute(
                "DELETE FROM music_file_inventory WHERE library_id=? AND path_key=?",
                (library_id, path_key),
            )

    def _music_directory_files(self, directory: Path) -> list[Path] | None:
        cached = self._music_directory_cache.get(directory)
        if directory in self._music_directory_cache:
            return list(cached) if cached is not None else None
        try:
            files = [path for path in directory.iterdir() if path.is_file()]
        except OSError:
            self._music_directory_cache[directory] = None
            return None
        self._music_directory_cache[directory] = files
        return list(files)

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
            current = context.get("current")
            total = context.get("total")
            self.store.update_job(
                job_id,
                message=(
                    stage
                    if current is None or total is None
                    else f"{stage} · {current}/{total}"
                ),
                progress_phase=(
                    "finalization"
                    if any(
                        token in stage.casefold()
                        for token in ("prun", "refresh", "queue", "reconcil")
                    )
                    else "processing"
                ),
                progress_label=stage,
                progress_stage_current=current,
                progress_stage_total=total,
                progress_stage_unit=context.get("unit"),
                progress_current_item=context.get("item"),
            )
            self._last_stage_persisted_at = time.monotonic()

    def _update_stage_progress(
        self,
        job_id: str,
        *,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
        **context,
    ) -> None:
        """Update a running stage without resetting its heartbeat timer.

        Music libraries can contain many more roots/files than movie or TV
        libraries.  Updating the stage context in place keeps the job useful
        while a large root is being enumerated, without making every progress
        update look like a brand-new stage.
        """
        with self._stage_lock:
            stage = self._stage
            merged_context = dict(self._stage_context)
            merged_context.update(context)
            if current is not None:
                merged_context["current"] = current
            if total is not None:
                merged_context["total"] = total
            self._stage_context = merged_context
        values = {
            "progress_current": current,
            "progress_total": total,
            "progress_phase": "processing",
            "progress_label": stage,
            "progress_stage_current": current,
            "progress_stage_total": total,
            "progress_stage_unit": merged_context.get("unit"),
            "progress_current_item": merged_context.get("item"),
        }
        if message is not None:
            values["message"] = message
        else:
            values["message"] = (
                stage
                if current is None or total is None
                else f"{stage} · {current}/{total}"
            )
        self.store.update_job(job_id, **values)

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
                self.store.update_job(
                    job_id,
                    message=message,
                    progress_phase="processing",
                    progress_label=stage,
                    progress_stage_current=context.get("current"),
                    progress_stage_total=context.get("total"),
                    progress_stage_unit=context.get("unit"),
                    progress_current_item=context.get("item"),
                )

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
        progress_kind = (
            "reconcile" if targets is not None else f"scan:{library['type']}"
        )
        self.store.begin_progress(job_id, progress_kind)
        started = now()
        reconcile_root_label = {
            "tv_series": "series",
            "movies": "movie",
            "music": "music",
        }.get(library["type"], library["type"])
        self.store.set_scan_state(library_id, "scanning", started=started, error=None)
        self.store.update_job(
            job_id,
            state="running",
            started_at=started,
            message=(
                f"Reconciling changed {reconcile_root_label} roots ({len(targets)} roots)"
                if targets is not None
                else "Discovering media"
            ),
        )
        self._start_heartbeat(library_id, job_id)
        self._scan_seen_ids = set()
        self._scan_created_ids = []
        self._scan_delta = {
            "added": set(),
            "changed": set(),
            "content_changed": set(),
            "metadata_changed": set(),
            "artwork_changed": set(),
            "unchanged": set(),
            "removed": set(),
        }
        self._scan_provider_identity_changed = set()
        self._scan_lastfm_attempted_ids = set()
        self._scan_rejected_ids = set()
        self._scan_reconciled_ids = set()
        self._scan_deferred_roots = set()
        self._scan_access_errors = set()
        self._scan_refresh_root_ids = set()
        self._music_local_metadata = {}
        self._local_nfo_sources = {}
        self._music_pending_release_ids = {}
        self._music_release_conflicts = set()
        self._music_file_observations = {}
        self._music_dirty_group_keys = set()
        self._music_dirty_release_ids = set()
        self._music_directory_cache = {}
        self._music_inventory_available = None
        self._last_stage_persisted_at = 0.0
        self._scan_complete = False
        try:
            self._check_termination(should_terminate)
            stage_context = {"root": str(root)}
            if targets is not None:
                stage_context["targetCount"] = len(targets)
                # A watcher can coalesce thousands of top-level music roots.
                # Keep the detailed list for small reconciles only; dumping it
                # into the heartbeat makes the administrator job unreadable
                # and needlessly inflates every progress update.
                if len(targets) <= 8:
                    stage_context["targets"] = sorted(targets)
            self._set_stage(
                job_id,
                f"Discovering {library['type']} roots",
                **stage_context,
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
                self._set_stage(job_id, "Populating changed metadata locales")
                self._fetch_seen_locales(should_terminate)
                self._set_stage(job_id, "Repairing music release context")
                from app.metadata_services import repair_music_track_contexts

                repair_music_track_contexts(
                    self.db,
                    library_id,
                    should_terminate,
                    release_ids=getattr(self, "_music_dirty_release_ids", None),
                )
                self._check_termination(should_terminate)
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
            bazarr_mapping_changed = bool(
                self._scan_delta["content_changed"] or removed
            )
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
            self._refresh_calendar_links(library_id, job_id)
            finished = now()
            completion_message = (
                f"Indexed {count} entries; reconciled {len(targets)} changed roots"
                if targets is not None
                else f"Indexed {count} entries"
            )
            self.store.update_job(
                job_id,
                state="completed",
                progress_current=count,
                progress_total=count,
                finished_at=finished,
                message=completion_message,
            )
            self.store.set_scan_state(library_id, "ready", finished=finished)
            if library["type"] in {"tv_series", "movies"} and bazarr_mapping_changed:
                try:
                    from app.jobs import scheduler

                    scheduler.enqueue_bazarr_sync()
                except Exception:
                    logger.warning(
                        "could not queue Bazarr mapping sync library_id=%s",
                        library_id,
                        exc_info=True,
                    )
            if removed:
                self._refresh_dependent_collections(library_id)
            self.db.schedule_maintenance(scan_complete=True)
            if not getattr(
                self.store, "runtime", None
            ) or not self.store.runtime.notifications_suppressed(library_id):
                try:
                    from app.notifications import NotificationService

                    admission_candidates = set(self._scan_delta["added"]) | set(
                        self._scan_delta["content_changed"]
                    )
                    NotificationService(self.db).record_admissions(admission_candidates)
                except Exception:
                    # Notification persistence must not turn an otherwise complete
                    # catalog scan into a failed media admission.
                    logger.warning(
                        "could not record catalog admissions library_id=%s",
                        library_id,
                        exc_info=True,
                    )
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
            self.store.end_progress(job_id)
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

    def _mark_changed(
        self,
        entity_id: str,
        *,
        content_changed: bool = False,
        metadata_changed: bool = False,
        artwork_changed: bool = False,
    ) -> None:
        self._scan_delta["changed"].add(entity_id)
        self._scan_delta["unchanged"].discard(entity_id)
        if content_changed:
            self._scan_delta["content_changed"].add(entity_id)
        if metadata_changed:
            self._scan_delta.setdefault("metadata_changed", set()).add(entity_id)
        if artwork_changed:
            self._scan_delta.setdefault("artwork_changed", set()).add(entity_id)

    def _metadata_candidates(self) -> set[str]:
        return (
            set(self._scan_delta["added"])
            | set(self._scan_delta["content_changed"])
            | set(self._scan_delta.get("metadata_changed", set()))
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
            "catalog_root_search_grams": "entity_id",
            "catalog_artwork_selection": "entity_id",
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
                and _top_level_key(relative_path) not in self._scan_deferred_roots
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
                if _top_level_key(relative_path or "") not in self._scan_deferred_roots
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

    @staticmethod
    def _refresh_calendar_links(library_id: str, job_id: str) -> None:
        """Keep persisted calendar/catalog relationships in sync with a scan."""
        try:
            from app.calendar import CalendarSyncService

            changed = CalendarSyncService().reconcile_catalog_links(library_id)
            logger.info(
                "calendar catalog links reconciled library_id=%s job_id=%s changed_events=%s",
                library_id,
                job_id,
                changed,
            )
        except Exception:
            # Calendar relationship maintenance must not turn a successful
            # catalog admission into a failed library scan.
            logger.warning(
                "could not reconcile calendar catalog links library_id=%s job_id=%s",
                library_id,
                job_id,
                exc_info=True,
            )

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

    @staticmethod
    def _nfo_file_for_entity(
        anchor: Path,
        entity_type: str,
        files: Iterable[Path | tuple[Path, os.stat_result | None]],
    ) -> Path | None:
        """Choose one deterministic sidecar NFO for an indexed entity."""
        candidates = sorted(
            {
                Path(value[0] if isinstance(value, tuple) else value)
                for value in files
                if Path(
                    value[0] if isinstance(value, tuple) else value
                ).suffix.casefold()
                in NFO_EXTENSIONS
            },
            key=lambda value: (str(value.parent).casefold(), value.name.casefold()),
        )
        if not candidates:
            return None
        anchor = Path(anchor)
        anchor_directory = anchor.parent if anchor.suffix else anchor
        same_directory = [
            value for value in candidates if value.parent == anchor_directory
        ]
        preferred = {
            "movie": ("movie.nfo", "movie.xml"),
            "series": ("tvshow.nfo", "series.nfo", "show.nfo"),
            "season": ("season.nfo", "season.xml"),
            "episode": ("episodedetails.nfo", "episode.nfo"),
            "artist": ("artist.nfo", "artist.xml"),
            "release": ("album.nfo", "release.nfo", "album.xml"),
            "track": ("track.nfo", "recording.nfo"),
        }.get(entity_type, ())
        for name in preferred:
            match = next(
                (value for value in same_directory if value.name.casefold() == name),
                None,
            )
            if match:
                return match
        anchor_name = (
            anchor.stem.casefold() if anchor.suffix else anchor.name.casefold()
        )
        match = next(
            (
                value
                for value in same_directory
                if value.stem.casefold() in {anchor_name, "".join(anchor_name.split())}
            ),
            None,
        )
        if match:
            return match
        # A directory can contain several entity sidecars (for example an
        # album NFO next to track NFOs).  Assigning the first filename makes
        # the result depend on filesystem order and can copy one entity's
        # metadata to another.  Unconventional filenames are intentionally
        # left unresolved instead of being guessed.
        return None

    def _local_nfo_document(self, entity_id: str) -> dict | None:
        if not self._has_table("metadata_cache"):
            return None
        rows = self.db.execute(
            "SELECT payload FROM metadata_cache WHERE provider='local' "
            "AND entity_type=(SELECT entity_type FROM library_entities WHERE id=?) "
            "AND provider_id=? AND locale='' ORDER BY rowid DESC LIMIT 1",
            (entity_id, entity_id),
        )
        if not rows:
            return None
        try:
            value = json.loads(rows[0][0] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or not value.get("_localNfo"):
            return None
        value.pop("_imageLanguageSchema", None)
        value.pop("_metadataLocale", None)
        value.pop("_stale", None)
        return value

    def _write_local_nfo_cache(
        self, entity_id: str, entity_type: str, document: dict
    ) -> None:
        if not self._has_table("metadata_cache"):
            return
        from app.models.metadata import IMAGE_LANGUAGE_SCHEMA

        payload = deepcopy(document)
        payload["_imageLanguageSchema"] = IMAGE_LANGUAGE_SCHEMA
        payload["_metadataLocale"] = ""
        encoded = json.dumps(payload, ensure_ascii=False)
        timestamp = now()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
        existing = self.db.execute(
            "SELECT rowid FROM metadata_cache WHERE provider='local' AND entity_type=? "
            "AND provider_id=? AND locale='' ORDER BY rowid DESC LIMIT 1",
            (entity_type, entity_id),
        )
        if existing:
            self.db.execute(
                "UPDATE metadata_cache SET payload=?,fetched_at=?,expires_at=? WHERE rowid=?",
                (encoded, timestamp, expires_at, existing[0][0]),
            )
        else:
            self.db.execute(
                "INSERT INTO metadata_cache(provider,entity_type,provider_id,locale,payload,fetched_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                ("local", entity_type, entity_id, "", encoded, timestamp, expires_at),
            )

    def _project_local_nfo(
        self, entity_id: str, entity_type: str, document: dict
    ) -> None:
        try:
            from app.metadata_services import MetadataSearchProjection
            from app.models.metadata import MetadataLanguageSettings

            locales = list(MetadataLanguageSettings().get()) or ["en"]
            projection = MetadataSearchProjection(self.db)
            for locale in locales:
                projection.project(
                    "local",
                    entity_type,
                    entity_id,
                    locale,
                    document,
                    replace_metadata=True,
                    target_entity_id=entity_id,
                )
        except Exception:
            logger.debug(
                "local NFO projection deferred entity_id=%s type=%s",
                entity_id,
                entity_type,
                exc_info=True,
            )

    def _persist_nfo_metadata(
        self,
        entity_id: str,
        entity_type: str,
        anchor: Path,
        files: Iterable[Path | tuple[Path, os.stat_result | None]],
        *,
        base_document: dict | None = None,
    ) -> bool:
        """Persist one sidecar document and project it over provider metadata."""
        file_values = list(files)
        sources = getattr(self, "_local_nfo_sources", {})
        self._local_nfo_sources = sources
        sources[entity_id] = (
            entity_type,
            Path(anchor),
            file_values,
        )
        nfo = self._nfo_file_for_entity(anchor, entity_type, file_values)
        previous = self._local_nfo_document(entity_id)
        if nfo is None:
            nfo_candidates = [
                Path(value[0] if isinstance(value, tuple) else value)
                for value in file_values
                if Path(
                    value[0] if isinstance(value, tuple) else value
                ).suffix.casefold()
                in NFO_EXTENSIONS
            ]
            if nfo_candidates and entity_type in {"artist", "release", "track"}:
                self._log_music_conflict(
                    entity_id,
                    entity_type,
                    "local_nfo_ambiguous",
                    "A local NFO sidecar could not be assigned to this music entity by a conventional filename",
                    {
                        "anchor": str(anchor),
                        "candidates": sorted(str(path.name) for path in nfo_candidates),
                    },
                )
            if previous is None:
                return False
            fallback = base_document
            if fallback is None and entity_type in {"artist", "release", "track"}:
                fallback = self._music_local_metadata.get(entity_id)
            if fallback and isinstance(fallback, dict):
                document = deepcopy(fallback)
                document["provider"] = "local"
                document["providerId"] = entity_id
                document["ids"] = [
                    {
                        "provider": "local",
                        "identifierType": entity_type,
                        "id": entity_id,
                    }
                ]
                document.pop("_localNfo", None)
                self._write_local_nfo_cache(entity_id, entity_type, document)
                self._project_local_nfo(entity_id, entity_type, document)
                if entity_type in {"artist", "release", "track"}:
                    self._music_local_metadata[entity_id] = document
                return True
            self.db.execute(
                "DELETE FROM metadata_cache WHERE provider='local' AND entity_type=? AND provider_id=?",
                (entity_type, entity_id),
            )
            self.db.execute(
                "DELETE FROM entity_provider_ids WHERE entity_id=? AND provider='local'",
                (entity_id,),
            )
            self._project_local_nfo(entity_id, entity_type, {})
            self._mark_changed(entity_id, metadata_changed=True)
            return True

        nfo_document = parse_nfo_metadata(nfo, entity_type)
        if not nfo_document:
            return False
        if base_document is None and entity_type in {"artist", "release", "track"}:
            base_document = self._music_local_metadata.get(entity_id)
        document = deepcopy(base_document) if isinstance(base_document, dict) else {}
        for key, value in nfo_document.items():
            if key in {"provider", "providerId", "ids"}:
                continue
            if key in {"images", "extraImages"} and not value:
                continue
            document[key] = deepcopy(value)
        document["provider"] = "local"
        document["providerId"] = entity_id
        nfo_ids = parse_nfo_ids(nfo, entity_type)
        document["ids"] = [
            {
                "provider": "local",
                "identifierType": entity_type,
                "id": entity_id,
            }
        ] + [
            {"provider": provider, "identifierType": identifier_type, "id": value}
            for provider, identifier_type, value in nfo_ids
        ]
        document["_localNfo"] = True
        changed = previous != document
        if nfo_ids:
            self._ids(entity_id, nfo_ids)
        self.db.execute(
            "INSERT OR REPLACE INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES(?,?,?,?,0)",
            (entity_id, "local", entity_type, entity_id),
        )
        self._write_local_nfo_cache(entity_id, entity_type, document)
        self._project_local_nfo(entity_id, entity_type, document)
        if entity_type in {"artist", "release", "track"}:
            self._music_local_metadata[entity_id] = document
        if changed:
            self._mark_changed(entity_id, metadata_changed=True)
        return changed

    def _repersist_nfo_metadata(self, entity_ids: Iterable[str]) -> None:
        """Reapply sidecars after a provider projection has completed."""
        sources = getattr(self, "_local_nfo_sources", {})
        for entity_id in dict.fromkeys(str(value) for value in entity_ids):
            source = sources.get(entity_id)
            if not source:
                continue
            entity_type, anchor, files = source
            self._persist_nfo_metadata(
                entity_id,
                entity_type,
                anchor,
                files,
                base_document=self._music_local_metadata.get(entity_id),
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
            if provider not in {"tmdb", "tvdb", "musicbrainz", "lastfm"}:
                continue
            if provider == "lastfm":
                try:
                    if not ingest.metadata_service.credentials.get("lastfm"):
                        continue
                except (AttributeError, RuntimeError, ValueError):
                    continue
            if provider == "musicbrainz":
                expected_identifier = {
                    "artist": "artist",
                    "release": "release",
                    "track": "recording",
                }.get(entity_type)
                if expected_identifier != identifier_type:
                    # Release-group, release-track, and work IDs are
                    # supporting identities. They are not fetchable catalog
                    # documents and must never be sent to /recording/{id}.
                    continue
                provider_locales = ingest.provider_locales(provider, entity_type)
                provider_locale = provider_locales[0]
                cached = ingest.metadata_service.cache.get(
                    provider, entity_type, str(provider_id), provider_locale
                )
                if cached:
                    if entity_id in self._music_local_metadata:
                        continue
                    cached.pop("_stale", None)
                    ingest.ingest_document(
                        provider,
                        entity_type,
                        str(provider_id),
                        provider_locale,
                        cached,
                        target_entity_id=entity_id,
                    )
                    continue
                task_key = (
                    (provider, entity_type, str(provider_id), str(entity_id))
                    if entity_type in {"artist", "release", "track"}
                    else (provider, entity_type, str(provider_id))
                )
                tasks.setdefault(task_key, []).extend(provider_locales)
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
                        target_entity_id=entity_id,
                    )
                    continue
                task_key = (
                    (provider, entity_type, str(provider_id), str(entity_id))
                    if entity_type in {"artist", "release", "track"}
                    else (provider, entity_type, str(provider_id))
                )
                tasks.setdefault(task_key, []).append(locale)

        def fetch_locales(task):
            task_key, missing = task
            provider, entity_type, provider_id = task_key[:3]
            return ingest.ingest_locales(
                provider,
                entity_type,
                provider_id,
                missing,
                force=False,
                **({"target_entity_id": task_key[3]} if len(task_key) > 3 else {}),
            )

        for task, _result, error in metadata_task_results(
            sorted(tasks.items()), fetch_locales, should_terminate
        ):
            self._check_termination(should_terminate)
            if error is not None:
                task_key, missing = task
                provider, entity_type, provider_id = task_key[:3]
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
            "SELECT entity_type,match_method FROM library_entities WHERE id=?",
            (entity_id,),
        )
        entity_type = row[0][0] if row else ""
        if (
            row
            and entity_type in {"artist", "release", "track"}
            and row[0][1] == "manual"
        ):
            # An administrator match is an explicit override. Scanner/NFO
            # discovery may enrich it, but must not replace its identity.
            return
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
            "SELECT entity_type,match_method FROM library_entities WHERE id=?",
            (entity_id,),
        )
        entity_type = row[0][0] if row else ""
        if (
            row
            and entity_type in {"artist", "release", "track"}
            and row[0][1] == "manual"
        ):
            # Preserve administrator-selected identities during inventory
            # refresh; source tags remain available as local evidence.
            return
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
        if entity_type in {
            "movie",
            "series",
            "season",
            "episode",
            "artist",
            "release",
            "track",
        }:
            normalized_keys = set(normalized)
            normalized.extend(
                value
                for value in current
                if value[0] == "local" and value not in normalized_keys
            )
            normalized = list(dict.fromkeys(normalized))
        if entity_type in {"artist", "release", "track"}:
            normalized_keys = set(normalized)
            normalized.extend(
                value
                for value in current
                if value[0] == "lastfm" and value not in normalized_keys
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

    def _music_provider_document(
        self, entity_id: str, entity_type: str, provider_id: str | None
    ) -> dict | None:
        document = self._music_local_metadata.get(entity_id)
        if not document:
            return None
        value = deepcopy(document)
        if provider_id:
            provider_id = str(provider_id)
            value["provider"] = "musicbrainz"
            value["providerId"] = provider_id
            value["ids"] = [
                {
                    "provider": "musicbrainz",
                    "identifierType": (
                        "recording" if entity_type == "track" else entity_type
                    ),
                    "id": provider_id,
                }
            ]
            if entity_type == "track":
                value["tracks"] = [
                    {
                        "id": provider_id,
                        "title": value.get("title"),
                        "position": value.get("trackNumber"),
                        "disc": value.get("discNumber"),
                        "durationSeconds": value.get("durationSeconds"),
                    }
                ]
        return value

    def _seed_music_provider_document(
        self,
        entity_id: str,
        entity_type: str,
        provider_id: str | None,
        ingest,
    ) -> bool:
        """Project local music metadata without blocking on MusicBrainz."""
        if not provider_id:
            return False
        document = self._music_provider_document(entity_id, entity_type, provider_id)
        if document is None:
            return False
        locales = ingest.locales()
        complete_batch = len(locales) == 1
        try:
            for locale in locales:
                ingest.ingest_document(
                    "musicbrainz",
                    entity_type,
                    str(provider_id),
                    locale,
                    deepcopy(document),
                    force_assets=False,
                    complete_batch=complete_batch,
                    target_entity_id=entity_id,
                )
        except Exception:
            logger.exception(
                "local music metadata projection failed entity_id=%s type=%s provider_id=%s",
                entity_id,
                entity_type,
                provider_id,
            )
            return False
        return True

    def _music_release_documents(
        self,
        release_id: str,
        release_documents: dict[str, dict[str, dict]],
        service,
        locales: list[str],
    ) -> dict[str, dict]:
        cached = release_documents.get(release_id)
        if cached is not None:
            return cached
        provider_rows = self.db.execute(
            "SELECT provider_id FROM entity_provider_ids "
            "WHERE entity_id=? AND provider='musicbrainz' "
            "ORDER BY CASE WHEN identifier_type='release' THEN 0 ELSE 1 END, is_primary DESC, provider_id",
            (release_id,),
        )
        if not provider_rows:
            release_documents[release_id] = {}
            return {}
        cache = getattr(service, "cache", None)
        get_cached = getattr(cache, "get", None)
        if get_cached is None:
            release_documents[release_id] = {}
            return {}
        values: dict[str, dict] = {}
        for locale in locales:
            try:
                document = get_cached(
                    "musicbrainz", "release", str(provider_rows[0][0]), ""
                )
                if document is None and locale:
                    document = get_cached(
                        "musicbrainz", "release", str(provider_rows[0][0]), locale
                    )
            except Exception:
                document = None
            if isinstance(document, dict):
                document = deepcopy(document)
                document.pop("_stale", None)
                values[locale] = document
        release_documents[release_id] = values
        return values

    def _music_track_documents(
        self,
        entity_id: str,
        release_id: str | None,
        provider_id: str | None,
        release_documents: dict[str, dict[str, dict]],
        service,
        locales: list[str],
    ) -> dict[str, dict]:
        if not provider_id:
            return {}
        release_documents_by_locale: dict[str, dict] = {}
        if release_id:
            release_documents_by_locale = self._music_release_documents(
                release_id, release_documents, service, locales
            )
        local = self._music_provider_document(entity_id, "track", provider_id)
        track_number = local.get("trackNumber") if local else None
        disc_number = local.get("discNumber") if local else None
        values: dict[str, dict] = {}
        for locale in locales:
            release_document = release_documents_by_locale.get(locale)
            candidate = None
            for track in (release_document or {}).get("tracks", []) or []:
                if not isinstance(track, dict):
                    continue
                if str(track.get("id") or "") == str(provider_id):
                    candidate = track
                    break
            cached_track = None
            # A recording ID is not a release-position hint. If it is not
            # present in this parent release, do not substitute the release's
            # track at the same position; the caller may have deliberately
            # rejected that explicit ID as a context mismatch.
            if candidate is None and release_document is None and local is None:
                cache = getattr(service, "cache", None)
                get_cached = getattr(cache, "get", None)
                if get_cached is not None:
                    try:
                        cached_track = get_cached(
                            "musicbrainz", "track", str(provider_id), ""
                        )
                        if cached_track is None and locale:
                            cached_track = get_cached(
                                "musicbrainz", "track", str(provider_id), locale
                            )
                    except Exception:
                        cached_track = None
                cached_tracks = (
                    cached_track.get("tracks", [])
                    if isinstance(cached_track, dict)
                    else []
                )
                if cached_tracks and isinstance(cached_tracks[0], dict):
                    candidate = deepcopy(cached_tracks[0])
            if candidate is not None and (
                release_document is not None or isinstance(cached_track, dict)
            ):
                value = deepcopy(release_document or cached_track)
                value["title"] = clean_music_title(
                    candidate.get("title") or (local.get("title") if local else None)
                )
                parent_album = (release_document or {}).get("title")
                parent_album_id = (release_document or {}).get("providerId")
                if parent_album:
                    value["album"] = parent_album
                elif isinstance(local, dict) and local.get("album"):
                    value["album"] = local["album"]
                else:
                    value.pop("album", None)
                if parent_album_id:
                    value["albumId"] = parent_album_id
                else:
                    # A recording cache hit does not establish this track's
                    # release relationship. Do not carry its first_release.
                    value.pop("albumId", None)
                value["provider"] = "musicbrainz"
                value["providerId"] = str(provider_id)
                value["ids"] = [
                    {
                        "provider": "musicbrainz",
                        "identifierType": "recording",
                        "id": str(provider_id),
                    }
                ]
                value["discNumber"] = candidate.get("disc") or disc_number
                value["trackNumber"] = candidate.get("position") or track_number
                value["durationSeconds"] = candidate.get("durationSeconds")
                value["tracks"] = [candidate]
                local_artists = (
                    local.get("artists") if isinstance(local, dict) else None
                )
                candidate_artists = candidate.get("artists")
                if not isinstance(candidate_artists, list):
                    candidate_artists = candidate.get("contributingArtists")
                track_artists = (
                    self._merge_music_artist_credits(local_artists, candidate_artists)
                    if isinstance(local_artists, list) and local_artists
                    else candidate_artists
                )
                if isinstance(track_artists, list) and track_artists:
                    value["artists"] = deepcopy(track_artists)
                    value["contributingArtists"] = deepcopy(track_artists)
                # Track artwork is inherited from the release by catalog
                # serialization. Avoid one image/credit asset job per track.
                value["images"] = []
                value["extraImages"] = []
                value["credits"] = []
                values[locale] = value
            elif local is not None:
                values[locale] = deepcopy(local)
        return values

    def _resolve_and_seed(
        self,
        library_id: str,
        library_type: str,
        job_id: str,
        should_terminate: Callable[[], bool],
    ) -> None:
        """Resolve top-level inventory entities and seed English/common metadata."""
        from app.metadata_services import MetadataIngestService
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
        ingest = (
            MetadataIngestService(service, background_assets=False)
            if library_type == "music"
            else None
        )
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
                {
                    "provider": row[0],
                    "identifierType": row[1],
                    "id": row[2],
                }
                for row in self.db.execute(
                    "SELECT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id=?",
                    (entity_id,),
                )
            ]
            if library_type == "music" and not explicit:
                # Untagged local music is still valid inventory. Do not turn
                # every artist folder into a MusicBrainz title search; the
                # metadata repair job can enrich it later if desired.
                self.db.execute(
                    "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_metadata',updated_at=? WHERE id=?",
                    (now(), entity_id),
                )
                self.store.update_job(
                    job_id,
                    progress_current=index,
                    message=f"Kept local metadata for {query}",
                )
                continue
            music_provider_id = next(
                (
                    str(value["id"])
                    for value in explicit
                    if value.get("provider") == "musicbrainz"
                    and value.get("id")
                    and value.get("identifierType")
                    == {
                        "artist": "artist",
                        "release": "release",
                        "track": "recording",
                    }.get(entity_type)
                ),
                None,
            )
            try:
                result = service.resolve_inventory_entity(
                    entity_type, query, year, explicit
                )
            except ProviderError:
                if library_type == "music":
                    seeded = bool(
                        ingest
                        and self._seed_music_provider_document(
                            entity_id, entity_type, music_provider_id, ingest
                        )
                    )
                    if seeded or entity_id in self._music_local_metadata:
                        self.db.execute(
                            "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_metadata',updated_at=? WHERE id=?",
                            (now(), entity_id),
                        )
                    if music_provider_id:
                        self._queue_metadata_repair(
                            entity_id,
                            library_id,
                            job_id,
                            "MusicBrainz unavailable; local music metadata retained",
                            ingest.locales() if ingest else None,
                        )
                        self.store.update_job(
                            job_id,
                            progress_current=index,
                            message=f"Kept local metadata for {query}; queued repair",
                        )
                        continue
                    if entity_id in self._music_local_metadata:
                        self.store.update_job(
                            job_id,
                            progress_current=index,
                            message=f"Kept local metadata for {query}",
                        )
                        continue
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
                try:
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
                except Exception as error:
                    if library_type != "music":
                        raise
                    seeded = bool(
                        ingest
                        and self._seed_music_provider_document(
                            entity_id, entity_type, str(value["id"]), ingest
                        )
                    )
                    if seeded or entity_id in self._music_local_metadata:
                        self.db.execute(
                            "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_metadata',updated_at=? WHERE id=?",
                            (now(), entity_id),
                        )
                    self._queue_metadata_repair(
                        entity_id,
                        library_id,
                        job_id,
                        f"MusicBrainz metadata unavailable: {type(error).__name__}: {error}",
                        ingest.locales() if ingest else None,
                    )
                    logger.warning(
                        "music root metadata deferred entity_id=%s provider_id=%s error=%s",
                        entity_id,
                        value["id"],
                        error,
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
        if entity_type not in {"movie", "episode", "artist", "release", "track"}:
            return
        try:
            from app.metadata_services import reproject_entity_artwork

            if entity_type in {"movie", "episode"}:
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

    def _music_identity_keys(
        self, root: Path, path: Path, tags: dict[str, str]
    ) -> list[tuple[str, str]]:
        primary, fallback = _music_album_identity_values(root, path, tags)
        values = [("provider" if primary[0] == "id" else "tag", primary)]
        if fallback != primary:
            values.append(("tag", fallback))
        return [(_music_identity_key_text(key), source) for source, key in values]

    def _music_identity_entity(
        self, library_id: str, entity_type: str, identity_key: tuple[str, ...] | str
    ) -> str | None:
        if not self._has_table("music_identity_keys"):
            return None
        key_text = (
            identity_key
            if isinstance(identity_key, str)
            else _music_identity_key_text(identity_key)
        )
        rows = self.db.execute(
            "SELECT entity_id FROM music_identity_keys "
            "WHERE library_id=? AND entity_type=? AND identity_key=? LIMIT 1",
            (library_id, entity_type, key_text),
        )
        return str(rows[0][0]) if rows else None

    def _persist_music_identity_keys(
        self,
        entity_id: str,
        library_id: str,
        entity_type: str,
        keys: Iterable[tuple[str, str]],
    ) -> None:
        if not self._has_table("music_identity_keys"):
            return
        timestamp = now()
        with self.db.transaction() as cursor:
            for identity_key, source in dict(keys).items():
                try:
                    cursor.execute(
                        "INSERT OR IGNORE INTO music_identity_keys "
                        "(entity_id,library_id,entity_type,identity_key,identity_source,identity_version,updated_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            entity_id,
                            library_id,
                            entity_type,
                            identity_key,
                            source,
                            1,
                            timestamp,
                        ),
                    )
                    cursor.execute(
                        "UPDATE music_identity_keys SET identity_source=?,identity_version=1,updated_at=? "
                        "WHERE entity_id=? AND entity_type=? AND identity_key=?",
                        (source, timestamp, entity_id, entity_type, identity_key),
                    )
                except Exception:
                    # A release key may already belong to another entity when
                    # an old scan produced duplicates. Keep the existing
                    # owner and surface the conflict during the next group.
                    logger.warning(
                        "music identity key could not be persisted entity_id=%s key=%s",
                        entity_id,
                        identity_key,
                        exc_info=True,
                    )

    def _log_music_conflict(
        self,
        entity_id: str,
        entity_type: str,
        code: str,
        reason: str,
        evidence: dict | None = None,
        *,
        severity: str = "warning",
        job_id: str | None = None,
    ) -> None:
        log = logger.error if severity == "error" else logger.warning
        log(
            "music metadata conflict entity_id=%s entity_type=%s code=%s "
            "job_id=%s reason=%s evidence=%s",
            entity_id,
            entity_type,
            code,
            job_id,
            reason,
            evidence if isinstance(evidence, dict) else {},
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
        has_local_nfo = self._local_nfo_document(entity_id) is not None
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
                if has_local_nfo:
                    self.db.execute(
                        "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_nfo',updated_at=? WHERE id=?",
                        (now(), entity_id),
                    )
                    message = f"Kept local NFO metadata for {query}"
                    self.store.update_job(
                        job_id, progress_current=index, message=message
                    )
                    return
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
            if has_local_nfo:
                self.db.execute(
                    "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_nfo',updated_at=? WHERE id=?",
                    (now(), entity_id),
                )
                self.store.update_job(
                    job_id,
                    progress_current=index,
                    message=f"Kept local NFO metadata for {query}; provider unavailable",
                )
                return
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
            if has_local_nfo:
                self.db.execute(
                    "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_nfo',updated_at=? WHERE id=?",
                    (now(), entity_id),
                )
                self.store.update_job(
                    job_id,
                    progress_current=index,
                    message=f"Kept local NFO metadata for {query}; provider unavailable",
                )
                return
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
        has_local_nfo = self._local_nfo_document(series_id) is not None
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
                if has_local_nfo:
                    self.db.execute(
                        "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_nfo',updated_at=? WHERE id=?",
                        (now(), series_id),
                    )
                    self.store.update_job(
                        job_id,
                        message=f"Kept local NFO metadata for series {series_id}",
                    )
                    return None
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
                if provider == "local":
                    continue
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
        release_documents: dict[str, dict[str, dict]] | None = None,
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
            provider_identity_rows = self.db.execute(
                "SELECT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id=?",
                (entity_id,),
            )
            expected_identifier = {
                "release": "release",
                "track": "recording",
            }.get(entity_type)
            provider_rows = [
                (provider, provider_id)
                for provider, identifier_type, provider_id in provider_identity_rows
                if provider != "musicbrainz"
                or expected_identifier is None
                or identifier_type == expected_identifier
            ]
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
                cache_locales = ingest.provider_locales(provider, entity_type)
                if any(
                    not self.db.execute(
                        "SELECT 1 FROM metadata_cache WHERE provider=? AND entity_type=? AND provider_id=? AND locale=? LIMIT 1",
                        (provider, entity_type, str(provider_id), locale),
                    )
                    for locale in cache_locales
                ):
                    return True
            return False

        def needs_artwork_reconciliation(row: tuple) -> bool:
            if (
                row[1]
                not in {
                    "movie",
                    "episode",
                    "artist",
                    "release",
                    "track",
                }
                or row[0] not in self._scan_seen_ids
            ):
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
        release_documents = release_documents if release_documents is not None else {}
        music_locales = ingest.locales()
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
            provider_identity_rows = self.db.execute(
                "SELECT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY is_primary DESC,provider",
                (entity_id,),
            )
            expected_identifier = {
                "release": "release",
                "track": "recording",
            }.get(entity_type)
            provider_rows = [
                (provider, provider_id)
                for provider, identifier_type, provider_id in provider_identity_rows
                if provider != "musicbrainz"
                or expected_identifier is None
                or identifier_type == expected_identifier
            ]
            music_provider_id = next(
                (
                    str(value[1])
                    for value in provider_rows
                    if value[0] == "musicbrainz" and value[1]
                ),
                None,
            )
            if entity_type == "track":
                local_documents = self._music_track_documents(
                    entity_id,
                    row_parent_id,
                    music_provider_id,
                    release_documents,
                    service,
                    music_locales,
                )
                if local_documents:
                    try:
                        for locale, normalized in local_documents.items():
                            ingest.ingest_document(
                                "musicbrainz",
                                "track",
                                music_provider_id,
                                locale,
                                normalized,
                                force_assets=False,
                                complete_batch=True,
                                target_entity_id=entity_id,
                            )
                            self._persist_normalized_ids(
                                entity_id, entity_type, normalized
                            )
                    except Exception as error:
                        logger.warning(
                            "local music track projection deferred entity_id=%s path=%s error=%s",
                            entity_id,
                            relative_path,
                            error,
                        )
                    self.db.execute(
                        "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_metadata',updated_at=? WHERE id=?",
                        (now(), entity_id),
                    )
                    self._extract_and_reproject(
                        entity_id, entity_type, should_terminate
                    )
                    self.store.update_job(
                        job_id,
                        progress_current=index,
                        message=f"Seeded local track {relative_path}",
                    )
                    continue
            if not provider_rows:
                if (
                    entity_type in {"release", "track"}
                    and entity_id in self._music_local_metadata
                ):
                    self.db.execute(
                        "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_metadata',updated_at=? WHERE id=?",
                        (now(), entity_id),
                    )
                    self._extract_and_reproject(
                        entity_id, entity_type, should_terminate
                    )
                    self.store.update_job(
                        job_id,
                        progress_current=index,
                        message=f"Kept local metadata for {relative_path}",
                    )
                    continue
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
                    self._extract_and_reproject(
                        entity_id, entity_type, should_terminate
                    )
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
                    self._extract_and_reproject(
                        entity_id, entity_type, should_terminate
                    )
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
                locales = (
                    ingest.provider_locales(provider, entity_type)
                    if provider == "musicbrainz"
                    else ingest.locales()
                )
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
                    ingest_kwargs = {}
                    if entity_type in {"artist", "release", "track"}:
                        ingest_kwargs["target_entity_id"] = entity_id
                    normalized_by_locale = ingest.ingest_locales(
                        provider,
                        entity_type,
                        provider_id,
                        locales,
                        force=False,
                        **ingest_kwargs,
                    )
                    if entity_type == "release" and provider == "musicbrainz":
                        release_documents[entity_id] = {
                            locale: deepcopy(normalized)
                            for locale, normalized in normalized_by_locale.items()
                        }
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
                    entity_id,
                    library_id,
                    job_id,
                    failure,
                    ingest.provider_locales(required, entity_type)
                    if required == "musicbrainz"
                    else ingest.locales(),
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

        if provider not in {"tmdb", "tvdb", "musicbrainz", "lastfm"}:
            return

        ingest = MetadataIngestService(service, background_assets=False)
        locales = (
            ingest.provider_locales(provider, entity_type)
            if provider in {"musicbrainz", "lastfm"}
            else ingest.locales()
        )
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
        """Attach provider child IDs only with complete local track evidence."""

        tracks = [
            value for value in normalized.get("tracks", []) or [] if value.get("id")
        ]
        if not tracks:
            return
        children = self.db.execute(
            "SELECT id,track_number,disc_number FROM library_entities WHERE parent_id=? AND entity_type='track' ORDER BY disc_number,track_number,relative_path",
            (parent_id,),
        )
        for entity_id, track_number, disc_number in children:
            existing = self.db.execute(
                "SELECT 1 FROM entity_provider_ids WHERE entity_id=? "
                "AND provider='musicbrainz' AND identifier_type='recording' LIMIT 1",
                (entity_id,),
            )
            if existing:
                # Explicit or previously validated recording identities are
                # never replaced by a positional child-order guess.
                continue
            local = getattr(self, "_music_local_metadata", {}).get(entity_id)
            if not isinstance(local, dict):
                # A repair/replay may not have an in-memory inventory record;
                # without local title and duration evidence there is no safe
                # child match to make.
                continue
            candidates = [
                value
                for value in tracks
                if self._music_strict_child_track_match(
                    local,
                    value,
                    track_number=track_number,
                    disc_number=disc_number,
                )
            ]
            # A recording ID is a child identity, not an album-order hint.
            # Never attach the Nth provider track to the Nth local file when
            # title, duration, disc, and position do not uniquely agree.
            candidate = candidates[0] if len(candidates) == 1 else None
            if candidate and candidate.get("id"):
                identities = [("musicbrainz", "recording", str(candidate["id"]))]
                identities.extend(
                    ("musicbrainz", "work", str(work_id))
                    for work_id in candidate.get("workIds", []) or []
                    if work_id
                )
                self._ids(entity_id, identities)

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
        audio_probes: dict[str, dict] | None = None,
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
            "metadata_changed": False,
            "artwork_changed": False,
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
                if not old and path.suffix.lower() in AUDIO_EXTENSIONS:
                    quick_fingerprint = _audio_inventory_fingerprint(
                        file_size, modified_ns
                    )
                    bytes_read = 0
                else:
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
                if content_changed:
                    if role == "media":
                        result["content_changed"] = True
                    elif role == "metadata":
                        result["metadata_changed"] = True
                    elif role == "image":
                        result["artwork_changed"] = True
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
                elif role == "metadata":
                    result["metadata_changed"] = True
                elif role == "image":
                    result["artwork_changed"] = True
        for key, old in existing.items():
            if key in seen:
                continue
            self.db.execute("DELETE FROM media_files WHERE id=?", (old[0],))
            result["removed"] += 1
            if old[2] == "media":
                result["content_changed"] = True
            elif old[2] == "metadata":
                result["metadata_changed"] = True
            elif old[2] == "image":
                result["artwork_changed"] = True
        if has_fingerprint:
            self._materialize_local_artwork(entity_id, root)
        if (
            result["added"]
            or result["removed"]
            or result["content_changed"]
            or result["metadata_changed"]
            or result["artwork_changed"]
        ):
            self._mark_changed(
                entity_id,
                content_changed=result["content_changed"],
                metadata_changed=result["metadata_changed"],
                artwork_changed=result["artwork_changed"],
            )
        if result["artwork_changed"]:
            try:
                from app.metadata_services import reproject_entity_artwork

                reproject_entity_artwork(self.db, entity_id)
            except Exception:
                logger.debug(
                    "local artwork projection refresh deferred entity_id=%s",
                    entity_id,
                    exc_info=True,
                )
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
            PlaybackManager().probe_entity(
                entity_id,
                audio_probes=audio_probes,
            )
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
    def _video_state(path: Path, file_stat: os.stat_result | None = None) -> str:
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

        for current, directories, filenames in os.walk(
            directory, onerror=traversal_error
        ):
            directories.sort(key=str.casefold)
            filenames.sort(key=str.casefold)
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
                    if path.suffix.lower() in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
                        self._record_access_error(path)
                    yield path, None
                    continue
                stat_started = time.monotonic()
                try:
                    file_stat = path.stat()
                except OSError:
                    if path.suffix.lower() in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
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
                (
                    0
                    if path.name.lower() == "specials"
                    else (
                        int(SEASON_RE.match(path.name).group(1))
                        if SEASON_RE.match(path.name)
                        else 1
                    )
                ),
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
                                sidecar_entry[0]
                                .stem.casefold()
                                .startswith(media.stem.casefold())
                                and sidecar_entry[0].suffix.lower()
                                not in VIDEO_EXTENSIONS
                            )
                            or (
                                sidecar_entry[0].name.casefold()
                                in {"episodedetails.nfo", "episode.nfo"}
                                and sum(
                                    1
                                    for sibling, sibling_stat in files_by_parent.get(
                                        media.parent, []
                                    )
                                    if self._is_supported_video(sibling, sibling_stat)
                                )
                                == 1
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
            (
                f"Reconciling changed movie roots ({len(targets)} roots)"
                if targets is not None
                else "Enumerating movie roots"
            ),
            root=str(root),
            targets=sorted(targets) if targets else None,
        )
        enumeration_started = time.monotonic()
        entries = sorted(
            (
                path
                for path in self._target_entries(root, targets)
                if path.is_dir() or path.suffix.lower() in VIDEO_EXTENSIONS
            ),
            key=lambda path: _path_key(relative(str(root), str(path))),
        )
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
                discovered_ids.extend(parse_nfo_ids(nfo, "movie"))
            if discovered_ids:
                self._replace_ids(entity, discovered_ids)
            file_delta = self._files(
                entity,
                root,
                files,
                job_id=job_id,
            )
            self._persist_nfo_metadata(entity, "movie", entry, files)
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
                or file_delta["metadata_changed"]
                or file_delta["artwork_changed"]
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
                self._persist_nfo_metadata(entity, "movie", entry, files)
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
            (
                f"Reconciling changed series roots ({len(targets)} roots)"
                if targets is not None
                else "Enumerating TV series roots"
            ),
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
                self._defer_root(
                    series_relative_path, "episode path could not be inspected"
                )
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
            for path in series_children:
                if path.is_file() and path.suffix.casefold() in NFO_EXTENSIONS:
                    series_ids.extend(parse_nfo_ids(path, "series"))
            if series_ids:
                self._replace_ids(series, series_ids)
            series_metadata = None
            accepted_series_episodes = 0
            accepted_seasons: list[tuple[Path, int, str, list]] = []
            accepted_episodes: list[tuple[str, str, Path, list]] = []
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
                season_files = []
                try:
                    season_files = [
                        path
                        for path in season_dir.iterdir()
                        if path.is_file()
                        and (
                            path.suffix.casefold() in NFO_EXTENSIONS
                            or path.suffix.lower() in IMAGE_EXTENSIONS
                        )
                        and not any(
                            path.stem.startswith(record[2].stem)
                            for record in episode_records
                        )
                    ]
                except OSError:
                    self._defer_root(
                        relative(str(root), str(season_dir)),
                        "season artwork directory is inaccessible",
                    )
                self._files(season, root, season_files, job_id=job_id)
                self._persist_nfo_metadata(season, "season", season_dir, season_files)
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
                    for path, _file_stat in episode_files:
                        if path.suffix.casefold() in NFO_EXTENSIONS:
                            episode_ids.extend(parse_nfo_ids(path, "episode"))
                    if episode_ids:
                        self._replace_ids(episode, episode_ids)
                    self._files(
                        episode,
                        root,
                        episode_files,
                        job_id=job_id,
                    )
                    self._persist_nfo_metadata(episode, "episode", media, episode_files)
                    if not self.db.execute(
                        "SELECT 1 FROM media_files WHERE entity_id=? AND role='media' LIMIT 1",
                        (episode,),
                    ):
                        self._scan_rejected_ids.add(episode)
                        continue
                    accepted_season_episodes += 1
                    accepted_series_episodes += 1
                    accepted_episodes.append((episode, season, media, episode_files))
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
                accepted_seasons.append(
                    (season_dir, season_folder_number, season, season_files)
                )
            root_video_stems = {
                path.stem
                for path in series_children
                if path.suffix.lower() in VIDEO_EXTENSIONS
            }
            series_files = [
                path
                for path in series_children
                if path.is_file()
                and path.suffix.lower() not in VIDEO_EXTENSIONS
                and not any(
                    path.stem.startswith(video_stem) for video_stem in root_video_stems
                )
            ]
            self._files(
                series,
                root,
                series_files,
                job_id=job_id,
            )
            self._persist_nfo_metadata(series, "series", series_dir, series_files)
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
                for (
                    season_dir,
                    season_folder_number,
                    season,
                    season_files,
                ) in accepted_seasons:
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
                        self._persist_nfo_metadata(
                            season, "season", season_dir, season_files
                        )
                        for (
                            child_id,
                            child_season,
                            media,
                            episode_files,
                        ) in accepted_episodes:
                            if child_season == season:
                                self._persist_nfo_metadata(
                                    child_id,
                                    "episode",
                                    media,
                                    episode_files,
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
            self._persist_nfo_metadata(series, "series", series_dir, series_files)
            for child_id, _season, media, episode_files in accepted_episodes:
                self._persist_nfo_metadata(child_id, "episode", media, episode_files)
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

    def _music_entity_by_provider_id(
        self,
        library_id: str,
        entity_type: str,
        identifier_type: str,
        provider_id: str,
    ) -> str | None:
        rows = self.db.execute(
            "SELECT e.id FROM library_entities e "
            "JOIN entity_provider_ids p ON p.entity_id=e.id "
            "WHERE e.library_id=? AND e.entity_type=? AND p.provider='musicbrainz' "
            "AND p.identifier_type=? AND p.provider_id=? "
            "ORDER BY e.id LIMIT 1",
            (library_id, entity_type, identifier_type, str(provider_id)),
        )
        if not rows:
            return None
        entity_id = str(rows[0][0])
        self._scan_seen_ids.add(entity_id)
        return entity_id

    def _music_artist_by_name(self, library_id: str, name: str) -> str | None:
        normalized = _music_normalize(name)
        if not normalized:
            return None
        rows = self.db.execute(
            "SELECT id,relative_path FROM library_entities "
            "WHERE library_id=? AND entity_type='artist' AND parent_id IS NULL "
            "ORDER BY id",
            (library_id,),
        )
        for entity_id, relative_path in rows:
            if _music_normalize(relative_path) == normalized:
                entity_id = str(entity_id)
                self._scan_seen_ids.add(entity_id)
                return entity_id
        return None

    def _persist_music_local_artist(
        self, artist_id: str, name: str, ingest=None
    ) -> None:
        """Keep providerless artist metadata durable and projection-readable."""
        existing_nfo = self._local_nfo_document(artist_id)
        if existing_nfo is not None:
            self._music_local_metadata[artist_id] = existing_nfo
            return
        display_name = _music_display_value(name)
        if not display_name:
            return
        document = _music_local_artist_document(display_name, artist_id)
        self._music_local_metadata[artist_id] = document
        self.db.execute(
            "INSERT OR IGNORE INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES(?,?,?,?,0)",
            (artist_id, "local", "artist", artist_id),
        )
        if self._has_table("metadata_cache"):
            from app.metadata_services import MetadataSearchProjection
            from app.models.metadata import IMAGE_LANGUAGE_SCHEMA

            payload = dict(document)
            payload["_imageLanguageSchema"] = IMAGE_LANGUAGE_SCHEMA
            payload["_metadataLocale"] = ""
            fetched_at = datetime.now(timezone.utc)
            cache_values = (
                json.dumps(payload, ensure_ascii=False),
                fetched_at.isoformat(),
                (fetched_at + timedelta(days=7)).isoformat(),
            )
            cache_key = ("local", "artist", artist_id, "")
            if self.db.execute(
                "SELECT 1 FROM metadata_cache WHERE provider=? AND entity_type=? AND provider_id=? AND locale=? LIMIT 1",
                cache_key,
            ):
                self.db.execute(
                    "UPDATE metadata_cache SET payload=?,fetched_at=?,expires_at=? WHERE provider=? AND entity_type=? AND provider_id=? AND locale=?",
                    (*cache_values, *cache_key),
                )
            else:
                self.db.execute(
                    "INSERT INTO metadata_cache(provider,entity_type,provider_id,locale,payload,fetched_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                    (*cache_key, *cache_values),
                )
            for locale in getattr(ingest, "locales", lambda: ["en"])():
                MetadataSearchProjection(self.db).project(
                    "local",
                    "artist",
                    artist_id,
                    locale,
                    document,
                    target_entity_id=artist_id,
                )

    def _music_mark_identity_changed(self, entity_id: str) -> None:
        if entity_id not in self._scan_created_ids:
            self._scan_provider_identity_changed.add(entity_id)

    def _music_group_needs_metadata(
        self,
        artist_id: str,
        release_id: str,
        tracks: list[dict],
    ) -> bool:
        entity_ids = [
            artist_id,
            release_id,
            *[
                str(track["entity_id"])
                for track in tracks
                if isinstance(track, dict) and track.get("entity_id")
            ],
        ]
        entity_ids = list(dict.fromkeys(entity_ids))
        if not entity_ids:
            return True
        if any(
            entity_id in self._scan_created_ids
            or entity_id in self._scan_provider_identity_changed
            for entity_id in entity_ids
        ):
            return True
        if self._has_table("music_artist_credits"):
            for track in tracks:
                if not isinstance(track, dict) or not track.get("entity_id"):
                    continue
                if not self.db.execute(
                    "SELECT 1 FROM music_artist_credits WHERE track_id=? LIMIT 1",
                    (str(track["entity_id"]),),
                ):
                    return True
        return False

    @staticmethod
    def _music_release_track_candidate(
        local: dict,
        tracks: list[dict],
        used_ids: set[str],
    ) -> dict | None:
        """Match one local track to a release medium without guessing freely."""
        title = _music_normalize(local.get("title"))
        track_number = local.get("trackNumber")
        disc_number = local.get("discNumber")
        duration = local.get("durationSeconds")
        scored: list[tuple[int, str, dict]] = []
        for index, candidate in enumerate(tracks):
            if not isinstance(candidate, dict):
                continue
            provider_id = str(candidate.get("id") or "")
            if provider_id and provider_id in used_ids:
                continue
            candidate_title = _music_normalize(candidate.get("title"))
            score = 0
            if title and candidate_title == title:
                score += 100
            elif title and (title in candidate_title or candidate_title in title):
                score += 55
            if track_number is not None and str(candidate.get("position") or "") == str(
                track_number
            ):
                score += 45
            if disc_number is not None and candidate.get("disc") is not None:
                if str(candidate.get("disc")) == str(disc_number):
                    score += 25
                else:
                    score -= 40
            candidate_duration = candidate.get("durationSeconds")
            if duration is not None and candidate_duration is not None:
                try:
                    difference = abs(float(duration) - float(candidate_duration))
                except (TypeError, ValueError):
                    difference = None
                if difference is not None:
                    score += (
                        15 if difference <= max(2.0, float(duration) * 0.03) else -10
                    )
            if score:
                scored.append((score, provider_id or str(index), candidate))
        scored.sort(key=lambda value: (value[0], value[1]), reverse=True)
        if not scored or scored[0][0] < 90:
            return None
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        candidate = deepcopy(scored[0][2])
        if candidate.get("id"):
            used_ids.add(str(candidate["id"]))
        return candidate

    @staticmethod
    def _music_strict_child_track_match(
        local: dict,
        candidate: dict,
        *,
        track_number: int | None,
        disc_number: int | None,
    ) -> bool:
        """Require all local evidence before using a positional child match."""
        if track_number is None or disc_number is None:
            return False
        candidate_position = _int_tag(str(candidate.get("position")))
        candidate_disc = _int_tag(str(candidate.get("disc")))
        local_position = _int_tag(str(track_number))
        local_disc = _int_tag(str(disc_number))
        if (
            local_position is None
            or local_disc is None
            or candidate_position != local_position
            or candidate_disc != local_disc
        ):
            return False
        local_title = _music_normalize(local.get("title"))
        candidate_title = _music_normalize(candidate.get("title"))
        if not local_title or not candidate_title or local_title != candidate_title:
            return False
        local_duration = local.get("durationSeconds")
        candidate_duration = candidate.get("durationSeconds")
        if local_duration is None or candidate_duration is None:
            return False
        try:
            return abs(float(local_duration) - float(candidate_duration)) <= max(
                3.0, float(local_duration) * 0.05
            )
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _music_release_context_matches(
        local: dict, document: dict, tracks: list[dict]
    ) -> tuple[bool, str, dict]:
        """Validate an explicit release identity against local evidence."""
        evidence = {
            "localTitle": local.get("title") or local.get("album"),
            "providerTitle": document.get("title") or document.get("album"),
            "localAlbumArtist": local.get("albumArtist"),
            "providerAlbumArtist": document.get("albumArtist"),
            "localDate": local.get("date") or local.get("releaseDate"),
            "providerDate": document.get("date") or document.get("releaseDate"),
        }
        local_title = _music_normalize(
            _music_display_value(local.get("title") or local.get("album"))
        )
        provider_title = _music_normalize(
            _music_display_value(document.get("title") or document.get("album"))
        )
        if local_title and provider_title and local_title != provider_title:
            return False, "release title differs from local metadata", evidence

        local_artist = _music_normalize(local.get("albumArtist"))
        provider_artists = [
            _music_normalize(value.get("name"))
            for value in LibraryScanner._music_document_credits(document)
            if isinstance(value, dict) and value.get("name")
        ]
        provider_artist = _music_normalize(document.get("albumArtist"))
        if (
            local_artist
            and provider_artist
            and local_artist
            not in {
                provider_artist,
                *provider_artists,
            }
        ):
            return False, "release artist differs from local metadata", evidence

        local_year = str(local.get("year") or local.get("date") or "")[:4]
        provider_year = str(
            document.get("year")
            or document.get("date")
            or document.get("releaseDate")
            or ""
        )[:4]
        if (
            local_year
            and provider_year
            and local_year.isdigit()
            and provider_year.isdigit()
        ):
            if local_year != provider_year:
                return False, "release year differs from local metadata", evidence

        local_type = _music_normalize(local.get("albumType"))
        provider_type = _music_normalize(document.get("albumType"))
        if local_type and provider_type and local_type != provider_type:
            return False, "release type differs from local metadata", evidence

        provider_tracks = [
            value
            for value in document.get("tracks", []) or []
            if isinstance(value, dict)
        ]
        explicit_ids = {
            str(value[2])
            for track in tracks
            for value in track.get("music_ids", []) or []
            if value[1] == "recording" and value[2]
        }
        provider_ids = {
            str(value.get("id")) for value in provider_tracks if value.get("id")
        }
        missing_ids = sorted(explicit_ids - provider_ids)
        if missing_ids:
            evidence["missingRecordingIds"] = missing_ids
            return (
                False,
                "release does not contain explicitly tagged recordings",
                evidence,
            )
        return True, "", evidence

    @staticmethod
    def _music_recording_context_mismatches(
        local: dict, candidate: dict, *, require_release_context: bool = True
    ) -> list[dict]:
        """Return explainable hard/advisory differences for one recording.

        MusicBrainz lengths describe the provider recording, while a local
        file can be a remaster, live edit, silence-trimmed encode, or simply
        have an inaccurate probe.  Navidrome-style tag-first admission treats
        that length difference as advisory.  Title, disc, and position remain
        the useful safeguards against attaching an explicitly tagged ID to a
        different track.
        """
        mismatches: list[dict] = []
        if require_release_context and (
            candidate.get("position") is None and candidate.get("disc") is None
        ):
            mismatches.append(
                {
                    "field": "releaseContext",
                    "local": {
                        "disc": local.get("discNumber"),
                        "position": local.get("trackNumber"),
                    },
                    "provider": {
                        "disc": candidate.get("disc"),
                        "position": candidate.get("position"),
                    },
                    "severity": "hard",
                }
            )
        local_title = _music_normalize(local.get("title"))
        candidate_title = _music_normalize(candidate.get("title"))
        if local_title and candidate_title and local_title != candidate_title:
            mismatches.append(
                {
                    "field": "title",
                    "local": local.get("title"),
                    "provider": candidate.get("title"),
                    "severity": "hard",
                }
            )
        local_position = local.get("trackNumber")
        candidate_position = candidate.get("position")
        if (
            local_position is not None
            and candidate_position is not None
            and str(local_position) != str(candidate_position)
        ):
            mismatches.append(
                {
                    "field": "position",
                    "local": local_position,
                    "provider": candidate_position,
                    "severity": "hard",
                }
            )
        local_disc = local.get("discNumber")
        candidate_disc = candidate.get("disc")
        if (
            local_disc is not None
            and candidate_disc is not None
            and str(local_disc) != str(candidate_disc)
        ):
            mismatches.append(
                {
                    "field": "disc",
                    "local": local_disc,
                    "provider": candidate_disc,
                    "severity": "hard",
                }
            )
        local_duration = local.get("durationSeconds")
        candidate_duration = candidate.get("durationSeconds")
        if local_duration is not None and candidate_duration is not None:
            try:
                if abs(float(local_duration) - float(candidate_duration)) > max(
                    3.0, float(local_duration) * 0.05
                ):
                    mismatches.append(
                        {
                            "field": "duration",
                            "local": round(float(local_duration), 3),
                            "provider": round(float(candidate_duration), 3),
                            "difference": round(
                                abs(float(local_duration) - float(candidate_duration)),
                                3,
                            ),
                            "severity": "advisory",
                        }
                    )
            except (TypeError, ValueError):
                mismatches.append(
                    {
                        "field": "duration",
                        "local": local_duration,
                        "provider": candidate_duration,
                        "severity": "advisory",
                    }
                )
        if not (local_title or local_position is not None or local_disc is not None):
            mismatches.append(
                {
                    "field": "localEvidence",
                    "local": None,
                    "provider": candidate,
                    "severity": "hard",
                }
            )
        return mismatches

    @staticmethod
    def _music_recording_context_matches(
        local: dict, candidate: dict, *, require_release_context: bool = True
    ) -> bool:
        return not any(
            mismatch.get("severity") == "hard"
            for mismatch in LibraryScanner._music_recording_context_mismatches(
                local, candidate, require_release_context=require_release_context
            )
        )

    @staticmethod
    def _music_recording_context_reason(
        mismatches: Iterable[dict], *, explicit: bool = False
    ) -> str:
        fields = [
            str(mismatch.get("field"))
            for mismatch in mismatches
            if isinstance(mismatch, dict) and mismatch.get("severity") == "hard"
        ]
        labels = {
            "releaseContext": "release position/disc context",
            "title": "title",
            "position": "track position",
            "disc": "disc number",
            "localEvidence": "local track evidence",
        }
        rendered = ", ".join(
            dict.fromkeys(labels.get(field, field) for field in fields)
        )
        prefix = (
            "The explicitly tagged recording" if explicit else "The selected recording"
        )
        return (
            f"{prefix} differs in {rendered or 'track context'}"
            "; local metadata was retained"
        )

    def _index_music_group(
        self,
        library_id: str,
        root: Path,
        job_id: str,
        should_terminate: Callable[[], bool],
        group_entries: list[tuple[Path, dict[str, str]]],
        artist_entities: dict[str, str],
        release_entities: dict[tuple[str, ...], str],
        *,
        group_key: tuple[str, ...] | None = None,
    ) -> tuple[str, str, list[dict], int]:
        first_path, first_tags = group_entries[0]
        relative_first = Path(relative(str(root), str(first_path)))
        fallback_artist_name = (
            relative_first.parts[0] if relative_first.parts else first_path.parent.name
        )
        primary_credit = _music_primary_artist_credit(
            first_tags, fallback_name=fallback_artist_name
        )
        artist_name = primary_credit["name"] if primary_credit else fallback_artist_name
        artist_key = _music_normalize(artist_name)
        embedded_artist_id = primary_credit.get("id") if primary_credit else None
        artist = None
        if embedded_artist_id:
            artist = self._music_entity_by_provider_id(
                library_id, "artist", "artist", embedded_artist_id
            )
        artist = (
            artist
            or artist_entities.get(artist_key)
            or self._music_artist_by_name(library_id, artist_name)
        )
        if not artist:
            artist = self._entity(library_id, None, "artist", artist_name)
        artist_entities[artist_key] = artist
        self._music_local_metadata[artist] = _music_local_document(
            first_path,
            first_tags,
            "artist",
            artist_name=artist_name,
        )
        artist_directory = (
            root / relative_first.parts[0]
            if relative_first.parts and (root / relative_first.parts[0]).is_dir()
            else first_path.parent
        )
        artist_directory_files = self._music_directory_files(artist_directory)
        if artist_directory_files is None:
            artist_assets = []
            self._defer_root(
                relative(str(root), str(artist_directory)),
                "artist artwork directory is inaccessible",
            )
        else:
            artist_assets = [
                path
                for path in artist_directory_files
                if path.suffix.lower() in IMAGE_EXTENSIONS
                or path.suffix.casefold() in NFO_EXTENSIONS
            ]
        self._files(artist, root, artist_assets, job_id=job_id)
        self._persist_nfo_metadata(
            artist,
            "artist",
            artist_directory,
            artist_assets,
            base_document=self._music_local_metadata.get(artist),
        )
        if embedded_artist_id:
            self._replace_ids(artist, [("musicbrainz", "artist", embedded_artist_id)])

        release_ids = []
        for _, tags in group_entries:
            release_ids.extend(_music_ids(tags, "release"))
        release_id_values = sorted(
            {
                str(value[2])
                for value in release_ids
                if value[1] == "release" and value[2]
            }
        )
        release_identity_conflict = len(release_id_values) > 1 or (
            group_key is not None and group_key[:1] == ("conflict",)
        )
        release_id = (
            release_id_values[0]
            if len(release_id_values) == 1 and not release_identity_conflict
            else None
        )
        album_dirs = [path.parent for path, _ in group_entries]
        try:
            album_dir = Path(os.path.commonpath([str(value) for value in album_dirs]))
        except (ValueError, OSError):
            album_dir = first_path.parent
        if album_dir == root:
            album_dir = first_path.parent
        album_path = relative(str(root), str(album_dir))
        album_name = _music_display_value(first_tags.get("ALBUM")) or album_dir.name
        if group_key is None:
            group_key, _identity_alias = _music_album_identity_values(
                root, first_path, first_tags
            )
        identity_aliases: list[tuple[str, str]] = []
        for path, tags in group_entries:
            identity_aliases.extend(self._music_identity_keys(root, path, tags))
        identity_aliases = list(dict.fromkeys(identity_aliases))
        release_key = tuple(group_key)
        release = release_entities.get(release_key)
        release = release or self._music_identity_entity(
            library_id, "release", release_key
        )
        # A tag-only alias can reconnect a release that previously lost its
        # explicit ID. It must not reconnect two different explicit release
        # IDs that happen to share title/artist/date tags.
        if not release and not release_id_values and not release_identity_conflict:
            for identity_key, _source in identity_aliases:
                release = self._music_identity_entity(
                    library_id, "release", identity_key
                )
                if release:
                    break
        if not release and release_id:
            provider_release = self._music_entity_by_provider_id(
                library_id, "release", "release", release_id
            )
            if provider_release and self._has_table("music_identity_keys"):
                stored_keys = {
                    str(row[0])
                    for row in self.db.execute(
                        "SELECT identity_key FROM music_identity_keys "
                        "WHERE entity_id=? AND entity_type='release'",
                        (provider_release,),
                    )
                }
                if (
                    stored_keys
                    and _music_identity_key_text(release_key) not in stored_keys
                ):
                    # The provider ID is shared by an incompatible local tag
                    # context. Keep this candidate separate until it proves
                    # itself against the provider release document.
                    self._scan_seen_ids.discard(provider_release)
                    provider_release = None
            release = provider_release
        if not release:
            release_path = album_path
            path_conflict = False
            if self._has_table("music_identity_keys"):
                path_rows = self.db.execute(
                    "SELECT id FROM library_entities WHERE library_id=? "
                    "AND entity_type='release' AND relative_path IS ?",
                    (library_id, album_path),
                )
                group_key_text = _music_identity_key_text(release_key)
                for (path_entity_id,) in path_rows:
                    if path_entity_id in release_entities.values():
                        path_conflict = True
                        break
                    stored_keys = {
                        str(row[0])
                        for row in self.db.execute(
                            "SELECT identity_key FROM music_identity_keys "
                            "WHERE entity_id=? AND entity_type='release'",
                            (path_entity_id,),
                        )
                    }
                    if stored_keys and group_key_text not in stored_keys:
                        path_conflict = True
                        break
                    if release_id_values:
                        stored_release_ids = {
                            str(row[0])
                            for row in self.db.execute(
                                "SELECT provider_id FROM entity_provider_ids "
                                "WHERE entity_id=? AND provider='musicbrainz' "
                                "AND identifier_type='release'",
                                (path_entity_id,),
                            )
                        }
                        if stored_release_ids.isdisjoint(release_id_values):
                            path_conflict = True
                            break
            if path_conflict:
                suffix = hashlib.sha256(
                    _music_identity_key_text(release_key).encode("utf-8")
                ).hexdigest()[:12]
                release_path = (
                    f".zenstream-release-{suffix}"
                    if not album_path or album_path == "."
                    else f"{album_path}/.zenstream-release-{suffix}"
                )
            release = self._entity(library_id, artist, "release", release_path)
        else:
            # Provider-ID lookup can return an existing release before the
            # path-based entity helper runs. Keep its catalog hierarchy in
            # sync with the one explicit album owner on every rescan.
            existing_path = self.db.execute(
                "SELECT relative_path FROM library_entities WHERE id=?",
                (release,),
            )
            if existing_path:
                self._entity(library_id, artist, "release", existing_path[0][0])
        release_entities[release_key] = release
        self._persist_music_identity_keys(
            release,
            library_id,
            "release",
            identity_aliases,
        )
        release_tags = dict(first_tags)
        for key in (
            "ALBUMTYPE",
            "ALBUMTYPES",
            "ALBUMSECONDARYTYPES",
            "ALBUMVERSION",
            "DATE",
        ):
            values: list[str] = []
            for _, tags in group_entries:
                values.extend(_music_tag_values(tags, key))
            if values:
                release_tags[key] = ";".join(dict.fromkeys(values))
        self._music_local_metadata[release] = _music_local_document(
            first_path,
            release_tags,
            "release",
            artist_name=artist_name,
            album_name=album_name,
        )
        if release_id_values and not release_identity_conflict:
            # Embedded release IDs are candidates until their title, artist,
            # date/version, track positions, and durations agree with the
            # normalized provider document.  Attaching them here would make
            # a copied tag authoritative before that validation happens.
            self._music_pending_release_ids[release] = set(release_id_values)
        elif release_identity_conflict:
            self._music_release_conflicts.add(release)
            self._log_music_conflict(
                release,
                "release",
                "conflicting_release_ids",
                "The album contains more than one explicit MusicBrainz release ID; local tags were retained",
                {
                    "releaseIds": release_id_values,
                    "paths": sorted(
                        relative(str(root), str(path)) for path, _tags in group_entries
                    ),
                },
                job_id=job_id,
            )

        ordered_entries = sorted(
            group_entries,
            key=lambda value: (
                _int_tag(value[1].get("DISCNUMBER"))
                or _music_filename_parts(value[0])[1]
                or 0,
                _int_tag(value[1].get("TRACKNUMBER"))
                or _music_filename_parts(value[0])[2]
                or 10**9,
                relative(str(root), str(value[0])).casefold(),
            ),
        )
        tracks = []
        for track, tags in ordered_entries:
            self._check_termination(should_terminate)
            track_number = _int_tag(tags.get("TRACKNUMBER"))
            disc_number = _int_tag(tags.get("DISCNUMBER"))
            _, filename_disc_number, filename_track_number = _music_filename_parts(
                track
            )
            if disc_number is None:
                disc_number = filename_disc_number
            if track_number is None:
                track_number = filename_track_number
            entity = self._entity(
                library_id,
                release,
                "track",
                relative(str(root), str(track)),
                disc_number=disc_number,
                track_number=track_number,
            )
            local = _music_local_document(
                track,
                tags,
                "track",
                artist_name=artist_name,
                album_name=album_name,
                disc_number=disc_number,
                track_number=track_number,
            )
            self._music_local_metadata[entity] = local
            track_identity_values = [
                (
                    "track",
                    _music_identity_key_text(
                        (
                            "track",
                            _music_normalize(local.get("title")),
                            str(disc_number or ""),
                            str(track_number or ""),
                            str(local.get("durationSeconds") or ""),
                        )
                    ),
                )
            ]
            self._persist_music_identity_keys(
                entity,
                library_id,
                "track",
                track_identity_values,
            )
            music_ids = _music_ids(tags, "track")
            if music_ids:
                self._replace_ids(entity, music_ids)
                self._music_mark_identity_changed(entity)
            directory_files = self._music_directory_files(track.parent)
            if directory_files is None:
                sidecars = []
                self._defer_root(
                    relative(str(root), str(track.parent)),
                    "track sidecars are inaccessible",
                )
            else:
                audio_sibling_count = sum(
                    1
                    for sibling in directory_files
                    if sibling.suffix.casefold() in AUDIO_EXTENSIONS
                )
                sidecars = [
                    sidecar
                    for sidecar in directory_files
                    if (
                        sidecar.stem.casefold().startswith(track.stem.casefold())
                        or (
                            sidecar.name.casefold() in {"track.nfo", "recording.nfo"}
                            and audio_sibling_count == 1
                        )
                    )
                    and sidecar != track
                ]
            relative_track = relative(str(root), str(track))
            observation = self._music_file_observations.get(_path_key(relative_track))
            track_file = (
                (track, observation.file_stat) if observation is not None else track
            )
            audio_probes = (
                {relative_track: observation.probe}
                if observation is not None and observation.probe is not None
                else None
            )
            track_files = [track_file, *sidecars]
            self._files(
                entity,
                root,
                track_files,
                job_id=job_id,
                audio_probes=audio_probes,
            )
            self._persist_nfo_metadata(
                entity,
                "track",
                track,
                track_files,
                base_document=local,
            )
            local = self._music_local_metadata.get(entity, local)
            tracks.append(
                {
                    "entity_id": entity,
                    "path": track,
                    "files": track_files,
                    "tags": tags,
                    "local": local,
                    "music_ids": music_ids,
                }
            )
            if observation is not None and (
                observation.changed or observation.cached_entity_id != entity
            ):
                self._music_inventory_upsert(
                    library_id,
                    root,
                    track,
                    observation.file_stat,
                    tags,
                    group_key,
                    entity_id=entity,
                )

        album_directory_files = self._music_directory_files(album_dir)
        artwork_accessible = album_directory_files is not None
        if not artwork_accessible:
            image_paths = []
            self._defer_root(
                relative(str(root), str(album_dir)),
                "album artwork directory is inaccessible",
            )
        else:
            image_paths = [
                path
                for path in album_directory_files
                if path.suffix.lower() in IMAGE_EXTENSIONS
                or path.suffix.casefold() in NFO_EXTENSIONS
            ]
        if artwork_accessible:
            self._files(release, root, image_paths, job_id=job_id)
            self._persist_nfo_metadata(
                release,
                "release",
                album_dir,
                image_paths,
                base_document=self._music_local_metadata.get(release),
            )
        self._persist_nfo_metadata(
            artist,
            "artist",
            artist_directory,
            artist_assets,
            base_document=self._music_local_metadata.get(artist),
        )
        return artist, release, tracks, len(tracks)

    def _enrich_music_lastfm_group(
        self,
        library_id: str,
        album_artist_id: str,
        release_id: str,
        tracks: list[dict],
        service,
        ingest,
        job_id: str,
        should_terminate: Callable[[], bool],
    ) -> None:
        """Best-effort Last.fm enrichment after local/MB music identity work."""
        from app.providers import ProviderError

        try:
            client = service.client("lastfm")
        except (AttributeError, ProviderError, RuntimeError, ValueError):
            # Last.fm is optional. An absent or unreadable key must never make
            # a playable music unit fail admission.
            return

        locales = ingest.locales()
        if not locales:
            return
        attempted = getattr(self, "_scan_lastfm_attempted_ids", set())
        self._scan_lastfm_attempted_ids = attempted

        def provider_id(entity_id: str, identifier_type: str) -> str | None:
            rows = self.db.execute(
                "SELECT provider_id FROM entity_provider_ids WHERE entity_id=? "
                "AND provider='lastfm' AND identifier_type=? ORDER BY provider_id LIMIT 1",
                (entity_id, identifier_type),
            )
            return str(rows[0][0]) if rows else None

        def musicbrainz_id(entity_id: str, identifier_type: str) -> str | None:
            rows = self.db.execute(
                "SELECT provider_id FROM entity_provider_ids WHERE entity_id=? "
                "AND provider='musicbrainz' AND identifier_type=? "
                "ORDER BY is_primary DESC,provider_id LIMIT 1",
                (entity_id, identifier_type),
            )
            return str(rows[0][0]) if rows else None

        def attach(entity_id: str, identifier_type: str, value: str) -> None:
            current = provider_id(entity_id, identifier_type)
            if current == value:
                return
            if current:
                self.db.execute(
                    "DELETE FROM entity_provider_ids WHERE entity_id=? AND provider='lastfm' AND identifier_type=?",
                    (entity_id, identifier_type),
                )
            self.db.execute(
                "INSERT OR REPLACE INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES(?,?,?,?,0)",
                (entity_id, "lastfm", identifier_type, value),
            )
            if current:
                self._music_mark_identity_changed(entity_id)
                self._mark_changed(entity_id)

        def enrich(
            entity_id: str,
            entity_type: str,
            *,
            artist_name: str | None,
            album_name: str | None = None,
            track_name: str | None = None,
            year: str | None = None,
            duration_seconds: float | None = None,
        ) -> None:
            if entity_id in attempted:
                return
            if entity_type == "artist" and not artist_name:
                return
            if entity_type == "release" and not (artist_name and album_name):
                return
            if entity_type == "track" and not (track_name and artist_name):
                return
            attempted.add(entity_id)
            current = provider_id(entity_id, entity_type)
            changed = (
                entity_id in self._scan_created_ids
                or entity_id in self._scan_provider_identity_changed
                or entity_id in self._scan_delta.get("content_changed", set())
            )
            try:
                if current and not changed:
                    lookup = current
                else:
                    lookup, _payload = client.resolve_lookup(
                        entity_type,
                        artist_name=artist_name,
                        album_name=album_name,
                        track_name=track_name,
                        year=year,
                        duration_seconds=duration_seconds,
                        mbid=musicbrainz_id(
                            entity_id,
                            "recording" if entity_type == "track" else entity_type,
                        ),
                        locale=locales[0],
                    )
                attach(entity_id, entity_type, lookup)
                ingest.ingest_locales(
                    "lastfm",
                    entity_type,
                    lookup,
                    locales,
                    force=False,
                    target_entity_id=entity_id,
                )
            except ProviderError as error:
                # Keep the identity when a resolved document fails during
                # ingestion; the existing metadata repair job can retry it.
                if current:
                    self._queue_metadata_repair(
                        entity_id,
                        library_id,
                        job_id,
                        f"Last.fm metadata unavailable: {error}",
                        locales,
                    )
                logger.info(
                    "Last.fm enrichment skipped entity_id=%s type=%s error=%s",
                    entity_id,
                    entity_type,
                    error,
                )
            except JobTerminated:
                raise
            except Exception as error:
                if current:
                    self._queue_metadata_repair(
                        entity_id,
                        library_id,
                        job_id,
                        f"Last.fm metadata unavailable: {error}",
                        locales,
                    )
                logger.info(
                    "Last.fm enrichment skipped entity_id=%s type=%s error=%s",
                    entity_id,
                    entity_type,
                    error,
                )

        release_local = self._music_local_metadata.get(
            release_id
        ) or self._music_document(release_id, "release")
        artist_local = self._music_local_metadata.get(
            album_artist_id
        ) or self._music_document(album_artist_id, "artist")
        album_name = _music_display_value(
            release_local.get("title") or release_local.get("album")
        )
        artist_name = _music_display_value(
            release_local.get("albumArtist")
            or artist_local.get("title")
            or artist_local.get("albumArtist")
        )
        year = str(release_local.get("year") or "")[:4] or None

        enrich(
            album_artist_id,
            "artist",
            artist_name=artist_name,
        )
        enrich(
            release_id,
            "release",
            artist_name=artist_name,
            album_name=album_name,
            year=year,
        )

        artist_ids = [album_artist_id]
        track_ids = [
            str(track.get("entity_id"))
            for track in tracks
            if isinstance(track, dict) and track.get("entity_id")
        ]
        if track_ids and self._has_table("music_artist_credits"):
            placeholders = ",".join("?" for _ in track_ids)
            artist_ids.extend(
                str(row[0])
                for row in self.db.execute(
                    "SELECT DISTINCT artist_id FROM music_artist_credits "
                    f"WHERE track_id IN ({placeholders}) ORDER BY artist_id",
                    track_ids,
                )
                if str(row[0]) not in artist_ids
            )
        for entity_id in artist_ids:
            self._check_termination(should_terminate)
            document = self._music_local_metadata.get(
                entity_id
            ) or self._music_document(entity_id, "artist")
            name = _music_display_value(
                document.get("title") or document.get("albumArtist")
            )
            enrich(entity_id, "artist", artist_name=name)

        for track in tracks:
            self._check_termination(should_terminate)
            if not isinstance(track, dict) or not track.get("entity_id"):
                continue
            local = track.get("local") or self._music_local_metadata.get(
                str(track["entity_id"]), {}
            )
            track_artists = local.get("artists") if isinstance(local, dict) else []
            track_artist = next(
                (
                    _music_display_value(value.get("name"))
                    for value in track_artists or []
                    if isinstance(value, dict) and value.get("name")
                ),
                None,
            )
            enrich(
                str(track["entity_id"]),
                "track",
                artist_name=track_artist or artist_name,
                album_name=_music_display_value(local.get("album")) or album_name,
                track_name=_music_display_value(local.get("title")),
                year=str(local.get("year") or year or "")[:4] or None,
                duration_seconds=local.get("durationSeconds"),
            )

    def _resolve_music_group(
        self,
        library_id: str,
        root: Path,
        job_id: str,
        should_terminate: Callable[[], bool],
        artist: str,
        release: str,
        tracks: list[dict],
        service,
        ingest,
    ) -> None:
        from app.providers import _select_music_match

        self._check_termination(should_terminate)
        locales = ingest.provider_locales("musicbrainz", "release")
        release_local = self._music_local_metadata.get(release) or {}
        album_name = _music_display_value(release_local.get("title"))
        artist_local = self._music_local_metadata.get(artist) or {}
        artist_name = _music_display_value(
            release_local.get("albumArtist")
        ) or _music_display_value(artist_local.get("title"))
        year = str(release_local.get("year") or "")[:4] or None
        provider_rows = self.db.execute(
            "SELECT identifier_type,provider_id FROM entity_provider_ids "
            "WHERE entity_id=? AND provider='musicbrainz' "
            "ORDER BY CASE WHEN identifier_type='release' THEN 0 ELSE 1 END, provider_id",
            (release,),
        )
        stored_release_id = next(
            (str(row[1]) for row in provider_rows if row[0] == "release"), None
        )
        pending_release_ids = set(
            getattr(self, "_music_pending_release_ids", {}).pop(release, set())
        )
        release_conflicted = release in getattr(self, "_music_release_conflicts", set())
        release_conflict = release_conflicted
        release_id = (
            sorted(pending_release_ids)[0]
            if pending_release_ids and not release_conflicted
            else stored_release_id
            if not release_conflicted
            else None
        )
        explicit_release_id = bool(pending_release_ids)
        release_from_search = False
        client = service.client("musicbrainz")
        release_documents: dict[str, dict] = {}
        release_error: Exception | None = None
        if (
            not release_id
            and not release_conflicted
            and album_name
            and (release_local.get("album") or release_local.get("albumArtist"))
        ):
            try:
                candidates = client.search_releases(album_name, artist_name, year)
                release_id = _select_music_match(
                    candidates, album_name, artist_name, year
                )
                release_from_search = bool(release_id)
            except Exception as error:
                release_error = error
        if release_id:
            try:
                release_documents = ingest.ingest_locales(
                    "musicbrainz",
                    "release",
                    release_id,
                    locales,
                    force=False,
                    target_entity_id=release,
                )
                validation_document = next(
                    (
                        value
                        for value in release_documents.values()
                        if isinstance(value, dict)
                    ),
                    None,
                )
                if validation_document is not None:
                    valid, reason, evidence = self._music_release_context_matches(
                        release_local, validation_document, tracks
                    )
                    if not valid:
                        self._log_music_conflict(
                            release,
                            "release",
                            "provider_release_conflict",
                            reason,
                            {
                                **evidence,
                                "providerId": release_id,
                                "explicitTag": explicit_release_id,
                            },
                            job_id=job_id,
                        )
                        release_error = ValueError(reason)
                        release_conflict = True
                        release_documents = {}
                    else:
                        if explicit_release_id or release_from_search:
                            self._replace_ids(
                                release, [("musicbrainz", "release", release_id)]
                            )
                            self._music_mark_identity_changed(release)
                        for locale, normalized in release_documents.items():
                            self._persist_normalized_ids(release, "release", normalized)
                            self._persist_child_ids(release, normalized)
                            from app.metadata_services import MetadataSearchProjection

                            MetadataSearchProjection(self.db).project(
                                "musicbrainz",
                                "release",
                                release_id,
                                locale,
                                normalized,
                                target_entity_id=release,
                            )
            except Exception as error:
                release_error = error
                release_documents = {}

        if release_documents:
            self.db.execute(
                "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='musicbrainz_release',updated_at=? WHERE id=?",
                (now(), release),
            )
        else:
            self.db.execute(
                "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method=?,updated_at=? WHERE id=?",
                (
                    "local_conflict" if release_conflict else "local_metadata",
                    now(),
                    release,
                ),
            )
            self._queue_metadata_repair(
                release,
                library_id,
                job_id,
                "MusicBrainz release matching failed; local music metadata retained"
                + (f": {release_error}" if release_error else ""),
                locales,
            )

        local_album_artist = _music_display_value(release_local.get("albumArtist"))
        if release_documents and local_album_artist:
            # MusicBrainz release credits are additive enrichment. Preserve
            # the explicit embedded primary in the projected album payload;
            # the provider's first credit is not an ownership decision.
            try:
                from app.metadata_services import MetadataSearchProjection

                projection = MetadataSearchProjection(self.db)
                for locale, document in release_documents.items():
                    if not isinstance(document, dict):
                        continue
                    local_credits = self._music_document_credits(release_local)
                    if not local_credits:
                        local_credits = [{"name": local_album_artist}]
                    provider_credits = self._music_document_credits(document)
                    merged_credits = self._merge_music_artist_credits(
                        local_credits, provider_credits
                    )
                    if merged_credits:
                        document["artists"] = merged_credits
                        document["contributingArtists"] = deepcopy(merged_credits)
                    document["albumArtist"] = local_album_artist
                    projection.project(
                        "musicbrainz",
                        "release",
                        release_id,
                        locale,
                        document,
                        target_entity_id=release,
                    )
            except Exception:
                logger.warning(
                    "could not preserve embedded music album artist in projection release_id=%s",
                    release,
                    exc_info=True,
                )

        release_document_values = list(release_documents.values())
        release_tracks = []
        if release_document_values:
            release_tracks = list(release_document_values[0].get("tracks", []) or [])
        used_release_track_ids: set[str] = set()
        artist_provider_rows = self.db.execute(
            "SELECT provider_id FROM entity_provider_ids WHERE entity_id=? "
            "AND provider='musicbrainz' AND identifier_type='artist' "
            "ORDER BY is_primary DESC,provider_id LIMIT 1",
            (artist,),
        )
        artist_provider_id = (
            str(artist_provider_rows[0][0]) if artist_provider_rows else None
        )
        if not artist_provider_id:
            # If the local parent has no identity yet, only attach a provider
            # credit whose name matches that explicit parent. Never use the
            # first release credit as an implicit reparenting signal.
            for document in release_document_values:
                matching = next(
                    (
                        value
                        for value in self._music_document_credits(document)
                        if value.get("id")
                        and _music_normalize(value.get("name"))
                        == _music_normalize(artist_name)
                    ),
                    None,
                )
                if matching:
                    artist_provider_id = str(matching["id"])
                    self._replace_ids(
                        artist,
                        [("musicbrainz", "artist", artist_provider_id)],
                    )
                    self._music_mark_identity_changed(artist)
                    break
        if artist_provider_id:
            try:
                ingest.ingest_locales(
                    "musicbrainz",
                    "artist",
                    artist_provider_id,
                    locales,
                    force=False,
                    target_entity_id=artist,
                )
                self.db.execute(
                    "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='musicbrainz_credit',updated_at=? WHERE id=?",
                    (now(), artist),
                )
            except Exception as error:
                self._queue_metadata_repair(
                    artist,
                    library_id,
                    job_id,
                    f"MusicBrainz artist metadata unavailable: {error}",
                    locales,
                )
        elif not self.db.execute(
            "SELECT 1 FROM entity_provider_ids WHERE entity_id=? LIMIT 1",
            (artist,),
        ):
            self.db.execute(
                "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_metadata',updated_at=? WHERE id=?",
                (now(), artist),
            )

        for track in tracks:
            self._check_termination(should_terminate)
            entity_id = track["entity_id"]
            local = track["local"]
            explicit_id = next(
                (value[2] for value in track["music_ids"] if value[1] == "recording"),
                None,
            )
            candidate = None
            if explicit_id:
                candidate = next(
                    (
                        value
                        for value in release_tracks
                        if str(value.get("id") or "") == str(explicit_id)
                    ),
                    None,
                )
                if candidate is None:
                    self._log_music_conflict(
                        entity_id,
                        "track",
                        "provider_recording_conflict",
                        "The explicitly tagged recording is not present in the validated parent release",
                        {
                            "recordingId": str(explicit_id),
                            "releaseId": release_id,
                            "title": local.get("title"),
                            "discNumber": local.get("discNumber"),
                            "trackNumber": local.get("trackNumber"),
                        },
                        job_id=job_id,
                    )
                elif not self._music_recording_context_matches(local, candidate):
                    mismatches = self._music_recording_context_mismatches(
                        local, candidate
                    )
                    self._log_music_conflict(
                        entity_id,
                        "track",
                        "provider_recording_conflict",
                        self._music_recording_context_reason(mismatches, explicit=True),
                        {
                            "recordingId": str(explicit_id),
                            "releaseId": release_id,
                            "local": local,
                            "provider": candidate,
                            "mismatches": mismatches,
                        },
                        job_id=job_id,
                    )
                    candidate = None
            if candidate is None and release_tracks and not explicit_id:
                candidate = self._music_release_track_candidate(
                    local, release_tracks, used_release_track_ids
                )
            if candidate is None and release_documents and not explicit_id:
                try:
                    track_artists = local.get("artists") or []
                    track_artist = (
                        track_artists[0].get("name")
                        if track_artists and isinstance(track_artists[0], dict)
                        else None
                    )
                    recording_candidates = client.search_recordings(
                        local.get("title") or "",
                        track_artist or artist_name,
                        album_name,
                        year,
                        local.get("durationSeconds"),
                    )
                    recording_id = _select_music_match(
                        recording_candidates,
                        local.get("title") or "",
                        track_artist or artist_name,
                        year,
                    )
                    self._replace_ids(
                        entity_id, [("musicbrainz", "recording", recording_id)]
                    )
                    self._music_mark_identity_changed(entity_id)
                    track_documents = ingest.ingest_locales(
                        "musicbrainz",
                        "track",
                        recording_id,
                        locales,
                        force=False,
                        target_entity_id=entity_id,
                    )
                    candidate = next(
                        (
                            value
                            for document in track_documents.values()
                            for value in document.get("tracks", []) or []
                            if isinstance(value, dict)
                        ),
                        None,
                    )
                    if (
                        candidate is not None
                        and not self._music_recording_context_matches(
                            local, candidate, require_release_context=False
                        )
                    ):
                        mismatches = self._music_recording_context_mismatches(
                            local, candidate, require_release_context=False
                        )
                        self._log_music_conflict(
                            entity_id,
                            "track",
                            "provider_recording_conflict",
                            self._music_recording_context_reason(mismatches),
                            {
                                "releaseId": release_id,
                                "local": local,
                                "provider": candidate,
                                "mismatches": mismatches,
                            },
                            job_id=job_id,
                        )
                        candidate = None
                except Exception as error:
                    self._queue_metadata_repair(
                        entity_id,
                        library_id,
                        job_id,
                        f"MusicBrainz recording matching failed; local metadata retained: {error}",
                        locales,
                    )
            if candidate is not None:
                candidate = deepcopy(candidate)
                track["resolved_artists"] = deepcopy(
                    candidate.get("artists")
                    or candidate.get("contributingArtists")
                    or []
                )
                candidate.setdefault("position", local.get("trackNumber"))
                candidate.setdefault("disc", local.get("discNumber"))
                candidate_id = candidate.get("id")
                if candidate_id and not explicit_id:
                    identities = [("musicbrainz", "recording", str(candidate_id))]
                    identities.extend(
                        ("musicbrainz", "work", str(work_id))
                        for work_id in candidate.get("workIds", []) or []
                        if work_id
                    )
                    self._replace_ids(
                        entity_id,
                        identities,
                    )
                    self._music_mark_identity_changed(entity_id)
                if candidate_id:
                    used_release_track_ids.add(str(candidate_id))
                    for document in release_documents.values():
                        document_tracks = document.setdefault("tracks", [])
                        if not any(
                            str(value.get("id") or "") == str(candidate_id)
                            for value in document_tracks
                            if isinstance(value, dict)
                        ):
                            document_tracks.append(deepcopy(candidate))

        self._materialize_music_artist_credits(
            library_id,
            artist,
            release,
            tracks,
            release_documents,
            ingest,
            job_id,
            should_terminate,
        )
        self._extract_and_reproject(release, "release", should_terminate)
        self._seed_all_children(
            library_id,
            service,
            job_id,
            should_terminate,
            parent_id=release,
            release_documents={release: release_documents},
        )
        self._enrich_music_lastfm_group(
            library_id,
            artist,
            release,
            tracks,
            service,
            ingest,
            job_id,
            should_terminate,
        )
        self._extract_and_reproject(artist, "artist", should_terminate)

    def _materialize_music_artist_credits(
        self,
        library_id: str,
        album_artist_id: str,
        release_id: str,
        tracks: list[dict],
        release_documents: dict[str, dict] | None,
        ingest,
        job_id: str,
        should_terminate: Callable[[], bool],
        *,
        resolve_provider_metadata: bool = True,
    ) -> None:
        """Materialize every credited artist and rewrite this group's links."""
        if not self._has_table("music_artist_credits"):
            return

        def values_from(source) -> list[dict]:
            return self._music_credit_source(source)

        release_values: list[dict] = []
        local_release = self._music_local_metadata.get(release_id) or {}
        release_values.extend(self._music_document_credits(local_release))
        for document in (release_documents or {}).values():
            if not isinstance(document, dict):
                continue
            release_values.extend(self._music_document_credits(document))

        artist_local = self._music_local_metadata.get(album_artist_id) or {}
        album_artist_name = _music_display_value(
            local_release.get("albumArtist")
            or artist_local.get("title")
            or artist_local.get("albumArtist")
        )
        parent_id_rows = self.db.execute(
            "SELECT provider_id FROM entity_provider_ids WHERE entity_id=? "
            "AND provider='musicbrainz' AND identifier_type='artist' "
            "ORDER BY is_primary DESC,provider_id LIMIT 1",
            (album_artist_id,),
        )
        album_artist_provider_id = str(parent_id_rows[0][0]) if parent_id_rows else None

        def primary_from_document(document: dict) -> dict | None:
            if not isinstance(document, dict):
                return None
            document_name = _music_display_value(document.get("albumArtist"))
            document_credits = self._music_document_credits(document)
            if document_credits:
                first_credit = document_credits[0]
                if document_name and _music_normalize(
                    first_credit["name"]
                ) == _music_normalize(document_name):
                    return dict(first_credit)
                if len(document_credits) > 1:
                    # A scalar albumArtist that names a later credit is not
                    # an ownership signal once ordered atomic credits exist.
                    return dict(first_credit)
                if document_name:
                    return {"name": document_name}
                return dict(first_credit)
            return {"name": document_name} if document_name else None

        primary_credit = primary_from_document(local_release)
        if primary_credit is None:
            for document in (release_documents or {}).values():
                primary_credit = primary_from_document(document)
                if primary_credit:
                    break
        if primary_credit is None and album_artist_name:
            primary_credit = {"name": album_artist_name}
        if primary_credit is None:
            primary_credit = {"name": "Unknown Artist"}
        album_artist_name = primary_credit["name"]
        parent_credit = {
            "name": album_artist_name,
            "id": album_artist_provider_id or primary_credit.get("id"),
        }
        attempted_provider_ids: set[str] = set()
        materialized_artists: set[str] = set()
        artist_entities = getattr(self, "_music_artist_entities", {})

        def dedupe(values: list[dict]) -> list[dict]:
            return self._music_document_credits({"artists": values})

        def resolve_artist(credit: dict, *, primary: bool = False) -> str:
            name = credit["name"]
            provider_id = credit.get("id")
            entity = None
            normalized_name = _music_normalize(name)
            if primary:
                entity = album_artist_id
            elif provider_id:
                entity = self._music_entity_by_provider_id(
                    library_id, "artist", "artist", provider_id
                )
            entity = entity or artist_entities.get(normalized_name)
            entity = entity or self._music_artist_by_name(library_id, name)
            if not entity:
                entity = self._entity(library_id, None, "artist", name)
            self._scan_seen_ids.add(entity)
            artist_entities[normalized_name] = entity
            self._persist_music_local_artist(entity, name, ingest)
            if provider_id:
                self._replace_ids(entity, [("musicbrainz", "artist", provider_id)])
                self._music_mark_identity_changed(entity)
                if not resolve_provider_metadata:
                    self.db.execute(
                        "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='musicbrainz_credit',updated_at=? WHERE id=?",
                        (now(), entity),
                    )
                elif provider_id not in attempted_provider_ids:
                    attempted_provider_ids.add(provider_id)
                    try:
                        ingest.ingest_locales(
                            "musicbrainz",
                            "artist",
                            provider_id,
                            ingest.provider_locales("musicbrainz", "artist"),
                            force=False,
                            target_entity_id=entity,
                        )
                        self.db.execute(
                            "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='musicbrainz_credit',updated_at=? WHERE id=?",
                            (now(), entity),
                        )
                    except Exception as error:
                        self.db.execute(
                            "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_metadata',updated_at=? WHERE id=?",
                            (now(), entity),
                        )
                        self._queue_metadata_repair(
                            entity,
                            library_id,
                            job_id,
                            f"MusicBrainz artist metadata unavailable; local artist retained: {error}",
                            ingest.provider_locales("musicbrainz", "artist"),
                        )
            else:
                self.db.execute(
                    "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_metadata',updated_at=? WHERE id=?",
                    (now(), entity),
                )
            materialized_artists.add(entity)
            return entity

        for track in tracks:
            self._check_termination(should_terminate)
            local = track.get("local") or {}
            candidates = [parent_credit]
            candidates.extend(release_values)
            candidates.extend(values_from(local.get("artists")))
            candidates.extend(values_from(local.get("contributingArtists")))
            candidates.extend(values_from(track.get("resolved_artists")))
            credits = dedupe(candidates)
            rows = []
            for order, credit in enumerate(credits):
                artist_entity = resolve_artist(credit, primary=order == 0)
                rows.append(
                    (
                        track["entity_id"],
                        artist_entity,
                        order,
                        credit["name"],
                    )
                )
            self.db.execute(
                "DELETE FROM music_artist_credits WHERE track_id=?",
                (track["entity_id"],),
            )
            if rows:
                self.db.write_many(
                    (
                        "INSERT INTO music_artist_credits(track_id,artist_id,credit_order,credited_name) VALUES(?,?,?,?)",
                        row,
                    )
                    for row in rows
                )

        for entity in materialized_artists:
            self._scan_refresh_root_ids.add(entity)
            self._publish_root(entity)
        self._flush_publications()

    def _music_document(self, entity_id: str, entity_type: str) -> dict:
        """Read one cached/projected music document without contacting a provider."""
        local_nfo = self._local_nfo_document(entity_id)
        if local_nfo is not None:
            return local_nfo
        projected_document = None
        if self._has_table("catalog_item_projection"):
            rows = self.db.execute(
                "SELECT payload FROM catalog_item_projection WHERE entity_id=? "
                "ORDER BY CASE locale WHEN '' THEN 0 WHEN 'en' THEN 1 ELSE 2 END,locale",
                (entity_id,),
            )
            for row in rows:
                try:
                    document = json.loads(row[0] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(document, dict):
                    projected_document = document
                    if (
                        entity_type == "artist"
                        or (
                            entity_type == "release"
                            and isinstance(document.get("tracks"), list)
                        )
                        or (
                            entity_type == "track"
                            and self._music_document_credits(document)
                        )
                    ):
                        return document

        if not self._has_table("metadata_cache"):
            return projected_document or {}
        identifier_type = "recording" if entity_type == "track" else entity_type
        rows = self.db.execute(
            "SELECT cache.payload FROM metadata_cache cache "
            "JOIN entity_provider_ids identity ON identity.provider='musicbrainz' "
            "AND identity.identifier_type=? AND identity.provider_id=cache.provider_id "
            "WHERE identity.entity_id=? AND cache.provider='musicbrainz' "
            "AND cache.entity_type=? ORDER BY cache.locale",
            (identifier_type, entity_id, entity_type),
        )
        for row in rows:
            try:
                document = json.loads(row[0] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(document, dict):
                return document
        return projected_document or {}

    def _update_music_projection_fields(self, entity_id: str, document: dict) -> None:
        """Apply local music credit fields while retaining cached payload data."""
        if not document or not self._has_table("catalog_item_projection"):
            return
        rows = self.db.execute(
            "SELECT locale,payload FROM catalog_item_projection WHERE entity_id=?",
            (entity_id,),
        )
        projection_columns = {
            row[1]
            for row in self.db.execute("PRAGMA table_info(catalog_item_projection)")
        }
        updated_at = (
            ",updated_at=CURRENT_TIMESTAMP"
            if "updated_at" in projection_columns
            else ""
        )
        for locale, payload in rows:
            try:
                merged = json.loads(payload or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                merged = {}
            if not isinstance(merged, dict):
                merged = {}
            changed = False
            for field in ("albumArtist", "artists", "contributingArtists"):
                if field in document and merged.get(field) != document[field]:
                    merged[field] = deepcopy(document[field])
                    changed = True
            if changed:
                self.db.execute(
                    "UPDATE catalog_item_projection SET payload=?"
                    + updated_at
                    + " WHERE entity_id=? AND locale=?",
                    (json.dumps(merged, ensure_ascii=False), entity_id, locale),
                )

    @staticmethod
    def _music_credit_source(source) -> list[dict]:
        """Normalize one ordered metadata credit list without joining names."""
        if not isinstance(source, list):
            return []
        values = []
        for value in source:
            if isinstance(value, dict):
                name = _music_display_value(value.get("name") or value.get("title"))
                provider_id = value.get("id") or value.get("providerId")
                join_phrase = value.get("joinPhrase")
                if join_phrase is None:
                    join_phrase = value.get("joinphrase")
            else:
                name = _music_display_value(value)
                provider_id = None
                join_phrase = None
            if not name:
                continue
            credit = {
                "name": name,
                "id": str(provider_id).strip()
                if provider_id is not None and str(provider_id).strip()
                else None,
            }
            if join_phrase is not None:
                credit["joinPhrase"] = str(join_phrase)
            values.append(credit)
        return values

    @staticmethod
    def _music_credit_is_combined(value: dict, atomic_values: list[dict]) -> bool:
        """Identify a joined scalar once structured artist boundaries exist."""
        if len(atomic_values) < 2:
            return False
        candidate = _music_normalize(value.get("name"))
        atomic_names = [_music_normalize(item.get("name")) for item in atomic_values]
        if not candidate or candidate in atomic_names:
            return False
        rendered = "".join(
            f"{item.get('name', '')}{item.get('joinPhrase', '')}"
            for item in atomic_values
        )
        if _music_normalize(rendered) == candidate:
            return True
        # Local documents may not retain a provider joinPhrase. The ordered
        # structured names still establish the boundary, so remove a scalar
        # that contains those names in order without attempting to split it.
        cursor = 0
        for name in atomic_names:
            if not name:
                return False
            position = candidate.find(name, cursor)
            if position < 0:
                return False
            cursor = position + len(name)
        return True

    @staticmethod
    def _music_document_has_combined_credit(
        document: dict, atomic_values: list[dict]
    ) -> bool:
        if not isinstance(document, dict) or len(atomic_values) < 2:
            return False
        return any(
            LibraryScanner._music_credit_is_combined(value, atomic_values)
            for key in ("artists", "contributingArtists")
            for value in LibraryScanner._music_credit_source(document.get(key))
        )

    @staticmethod
    def _music_document_credits(document: dict) -> list[dict]:
        """Combine primary and contributing credits from a cached document."""
        values: list[dict] = []
        by_id: dict[str, int] = {}
        by_name: dict[str, list[int]] = {}
        sources: list[list[dict]] = []
        for key in ("artists", "contributingArtists"):
            source = LibraryScanner._music_credit_source(document.get(key))
            if source:
                sources.append(source)
                for value in source:
                    name = value["name"]
                    provider_id = value.get("id")
                    name_key = _music_normalize(name)
                    index = by_id.get(provider_id) if provider_id else None
                    if index is None:
                        name_indices = by_name.get(name_key, [])
                        index = next(
                            (
                                candidate
                                for candidate in name_indices
                                if not values[candidate].get("id")
                            ),
                            None,
                        )
                        if index is None and not provider_id and name_indices:
                            index = name_indices[0]
                    if index is None:
                        index = len(values)
                        values.append(
                            {
                                "name": name,
                                **({"id": provider_id} if provider_id else {}),
                                **(
                                    {"joinPhrase": value["joinPhrase"]}
                                    if "joinPhrase" in value
                                    else {}
                                ),
                            }
                        )
                        by_name.setdefault(name_key, []).append(index)
                        if provider_id:
                            by_id[provider_id] = index
                        continue
                    existing = values[index]
                    if provider_id and not existing.get("id"):
                        existing["id"] = provider_id
                        by_id[provider_id] = index
                    if "joinPhrase" not in existing and "joinPhrase" in value:
                        existing["joinPhrase"] = value["joinPhrase"]
        atomic_sources: list[list[dict]] = []
        for source in sources:
            candidate_values = [
                value
                for index, value in enumerate(source)
                if not LibraryScanner._music_credit_is_combined(
                    value,
                    [
                        other
                        for other_index, other in enumerate(source)
                        if other_index != index
                    ],
                )
            ]
            identified = [value for value in candidate_values if value.get("id")]
            if len(identified) >= 2:
                atomic_source = identified
            else:
                atomic_source = candidate_values
            if len(atomic_source) > 1:
                atomic_sources.append(atomic_source)
        if atomic_sources:
            filtered = []
            for value in values:
                combined = False
                for atomic_source in atomic_sources:
                    atomic_ids = {
                        item.get("id") for item in atomic_source if item.get("id")
                    }
                    if LibraryScanner._music_credit_is_combined(
                        value, atomic_source
                    ) and (not value.get("id") or value.get("id") not in atomic_ids):
                        combined = True
                        break
                if not combined:
                    filtered.append(value)
            values = filtered
        return values

    @staticmethod
    def _merge_music_artist_credits(
        *sources: list[dict] | None,
    ) -> list[dict]:
        """Merge local/provider credits while retaining provider join phrases."""
        values: list[dict] = []
        for source in sources:
            if isinstance(source, list):
                values.extend(value for value in source if isinstance(value, dict))
        return LibraryScanner._music_document_credits({"artists": values})

    @staticmethod
    def _music_release_track_document(
        release_document: dict,
        recording_id: str | None,
        disc_number,
        track_number,
        local: dict | None = None,
    ) -> dict:
        candidates = release_document.get("tracks")
        if not isinstance(candidates, list):
            return {}
        for candidate in candidates:
            if (
                isinstance(candidate, dict)
                and recording_id
                and str(candidate.get("id") or "") == str(recording_id)
            ):
                return candidate
        if isinstance(local, dict):
            matches = [
                candidate
                for candidate in candidates
                if isinstance(candidate, dict)
                and LibraryScanner._music_strict_child_track_match(
                    local,
                    candidate,
                    track_number=track_number,
                    disc_number=disc_number,
                )
            ]
            if len(matches) == 1:
                return matches[0]
        return {}

    def repair_music_artist_credits(
        self,
        ingest,
        job_id: str,
        should_terminate: Callable[[], bool],
    ) -> int:
        """Backfill credited artist entities from already indexed music metadata."""
        if not self._has_table("music_artist_credits") or not self._has_table(
            "library_entities"
        ):
            return 0

        self._music_local_metadata = {}
        self._music_artist_entities = {}
        release_rows = self.db.execute(
            "SELECT id,library_id,parent_id FROM library_entities "
            "WHERE entity_type='release' AND parent_id IS NOT NULL "
            "ORDER BY library_id,id"
        )
        repaired = 0
        affected_libraries: set[str] = set()
        affected_artist_roots: set[str] = set()
        for release_id, library_id, album_artist_id in release_rows:
            self._check_termination(should_terminate)
            release_id = str(release_id)
            library_id = str(library_id)
            old_album_artist_id = str(album_artist_id)
            release_document = self._music_document(release_id, "release")
            artist_document = self._music_document(old_album_artist_id, "artist")
            if not isinstance(release_document, dict):
                release_document = {}
            release_document = deepcopy(release_document)
            artist_row = self.db.execute(
                "SELECT relative_path FROM library_entities WHERE id=? AND entity_type='artist'",
                (old_album_artist_id,),
            )
            current_artist_name = _music_display_value(
                (artist_document or {}).get("title")
                or (artist_row[0][0] if artist_row else "")
            )
            release_credits = self._music_document_credits(release_document)
            explicit_album_artist = _music_display_value(
                release_document.get("albumArtist")
            )
            primary_credit = None
            if release_credits:
                first_credit = release_credits[0]
                if explicit_album_artist and _music_normalize(
                    first_credit.get("name")
                ) == _music_normalize(explicit_album_artist):
                    primary_credit = first_credit
                elif len(release_credits) > 1:
                    # A legacy scalar albumArtist that names a later credit
                    # is superseded by the ordered atomic credit list.
                    primary_credit = first_credit
                elif explicit_album_artist:
                    primary_credit = {"name": explicit_album_artist}
                else:
                    primary_credit = first_credit
            elif explicit_album_artist:
                primary_credit = {"name": explicit_album_artist}
            if primary_credit is None and current_artist_name:
                primary_credit = {"name": current_artist_name}
            if primary_credit is None:
                primary_credit = {"name": "Unknown Artist"}
            primary_credit = dict(primary_credit)
            artist_name = _music_display_value(primary_credit.get("name"))
            if artist_name:
                release_document["albumArtist"] = artist_name
                ordered_credits = [primary_credit]
                for credit in release_credits:
                    same_provider_id = bool(
                        credit.get("id")
                        and primary_credit.get("id")
                        and str(credit["id"]) == str(primary_credit["id"])
                    )
                    same_name = _music_normalize(
                        credit.get("name")
                    ) == _music_normalize(artist_name)
                    if same_provider_id or same_name:
                        continue
                    ordered_credits.append(credit)
                release_document["artists"] = ordered_credits
                release_document["contributingArtists"] = deepcopy(ordered_credits)
            self._music_local_metadata[str(release_id)] = release_document
            self._update_music_projection_fields(release_id, release_document)

            primary_provider_id = primary_credit.get("id")
            target_artist_id = None
            if primary_provider_id:
                target_artist_id = self._music_entity_by_provider_id(
                    library_id,
                    "artist",
                    "artist",
                    str(primary_provider_id),
                )
            target_artist_id = target_artist_id or self._music_artist_by_name(
                library_id, artist_name
            )
            if target_artist_id is None and _music_normalize(
                current_artist_name
            ) == _music_normalize(artist_name):
                target_artist_id = old_album_artist_id
            if target_artist_id is None:
                target_artist_id = self._entity(library_id, None, "artist", artist_name)
            self._scan_seen_ids.add(target_artist_id)
            self._music_artist_entities[_music_normalize(artist_name)] = (
                target_artist_id
            )
            self._persist_music_local_artist(target_artist_id, artist_name, ingest)
            if primary_provider_id:
                self._replace_ids(
                    target_artist_id,
                    [("musicbrainz", "artist", str(primary_provider_id))],
                )
                self._music_mark_identity_changed(target_artist_id)

            album_artist_id = target_artist_id
            affected_libraries.add(library_id)
            affected_artist_roots.update({old_album_artist_id, album_artist_id})
            if album_artist_id != old_album_artist_id:
                relative_row = self.db.execute(
                    "SELECT relative_path FROM library_entities WHERE id=? AND entity_type='release'",
                    (release_id,),
                )
                if relative_row:
                    self._entity(
                        library_id,
                        album_artist_id,
                        "release",
                        relative_row[0][0],
                    )
                self._scan_refresh_root_ids.update(
                    {old_album_artist_id, album_artist_id}
                )

            track_columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(library_entities)")
            }
            disc_expression = (
                "disc_number" if "disc_number" in track_columns else "NULL"
            )
            track_expression = (
                "track_number" if "track_number" in track_columns else "NULL"
            )
            tracks = self.db.execute(
                "SELECT id,relative_path,"
                + disc_expression
                + ","
                + track_expression
                + " "
                "FROM library_entities WHERE parent_id=? AND entity_type='track' "
                "ORDER BY "
                + disc_expression
                + " IS NULL,"
                + disc_expression
                + ","
                + track_expression
                + " IS NULL,"
                + track_expression
                + ",relative_path COLLATE NOCASE,id",
                (release_id,),
            )
            materialized_tracks = []
            for track_id, relative_path, disc_number, track_number in tracks:
                self._check_termination(should_terminate)
                document = self._music_document(str(track_id), "track")
                if not isinstance(document, dict):
                    document = {}
                document = deepcopy(document)
                recording_rows = self.db.execute(
                    "SELECT provider_id FROM entity_provider_ids WHERE entity_id=? "
                    "AND provider='musicbrainz' AND identifier_type='recording' "
                    "ORDER BY is_primary DESC,provider_id LIMIT 1",
                    (track_id,),
                )
                recording_id = str(recording_rows[0][0]) if recording_rows else None
                release_track = self._music_release_track_document(
                    release_document,
                    recording_id,
                    disc_number,
                    track_number,
                    local=document,
                )
                track_credits = self._music_document_credits(document)
                release_track_credits = self._music_document_credits(release_track)
                if len(release_track_credits) > 1 or not track_credits:
                    track_credits = release_track_credits or track_credits
                if not track_credits and len(release_credits) > 1:
                    track_credits = release_credits
                structured_credits = (
                    release_track_credits
                    if len(release_track_credits) > 1
                    else release_credits
                    if len(release_credits) > 1
                    else []
                )
                if structured_credits and self._music_document_has_combined_credit(
                    document, structured_credits
                ):
                    track_credits = release_track_credits or release_credits
                if track_credits:
                    document["artists"] = deepcopy(track_credits)
                    document["contributingArtists"] = deepcopy(track_credits)
                    self._update_music_projection_fields(str(track_id), document)
                document.setdefault("discNumber", disc_number)
                document.setdefault("trackNumber", track_number)
                if not document.get("title") and relative_path:
                    document["title"] = clean_music_title(Path(relative_path).stem)
                materialized_tracks.append(
                    {
                        "entity_id": str(track_id),
                        "local": document,
                        "resolved_artists": track_credits,
                    }
                )
            if not materialized_tracks:
                continue
            self._materialize_music_artist_credits(
                str(library_id),
                str(album_artist_id),
                str(release_id),
                materialized_tracks,
                {"": release_document},
                ingest,
                job_id,
                should_terminate,
                resolve_provider_metadata=False,
            )
            repaired += 1
        for library_id in affected_libraries:
            self._remove_orphan_music_artists(library_id)
        for root_id in affected_artist_roots:
            if self.db.execute(
                "SELECT 1 FROM library_entities WHERE id=? AND entity_type='artist'",
                (root_id,),
            ):
                self._scan_refresh_root_ids.add(root_id)
                self._publish_root(root_id)
        self._flush_publications()
        if affected_libraries and self._has_table("catalog_item_projection"):
            try:
                from app.catalog_read_model import CatalogReadModel

                CatalogReadModel(self.db).refresh_roots(
                    [], affected_library_ids=sorted(affected_libraries)
                )
            except Exception:
                logger.warning(
                    "music artist repair catalog refresh failed libraries=%s",
                    sorted(affected_libraries),
                    exc_info=True,
                )
        return repaired

    def _remove_orphan_music_artists(self, library_id: str) -> None:
        if not self._has_table("music_artist_credits"):
            return
        rows = self.db.execute(
            "SELECT artist.id FROM library_entities artist "
            "WHERE artist.library_id=? AND artist.entity_type='artist' "
            "AND NOT EXISTS (SELECT 1 FROM library_entities release "
            "WHERE release.parent_id=artist.id AND release.entity_type='release') "
            "AND NOT EXISTS (SELECT 1 FROM music_artist_credits credit "
            "WHERE credit.artist_id=artist.id)",
            (library_id,),
        )
        orphan_ids = [str(row[0]) for row in rows]
        if not orphan_ids:
            return
        from app.library_cleanup import cleanup_entities

        cleanup_entities(self.db, orphan_ids)
        self._delete_catalog_rows(orphan_ids)
        self._scan_delta["removed"].update(orphan_ids)
        self._scan_seen_ids.difference_update(orphan_ids)
        self._scan_refresh_root_ids.difference_update(orphan_ids)
        self._scan_created_ids = [
            entity_id
            for entity_id in self._scan_created_ids
            if entity_id not in orphan_ids
        ]

    def _scan_music(
        self,
        library_id: str,
        root: Path,
        job_id: str,
        should_terminate: Callable[[], bool],
        targets: set[str] | None = None,
    ) -> int:
        self._music_local_metadata = {}
        self._local_nfo_sources = {}
        self._music_artist_entities: dict[str, str] = {}
        self._music_release_entities: dict[tuple[str, ...], str] = {}
        self._music_file_observations = {}
        self._music_dirty_group_keys = set()
        self._music_dirty_release_ids = set()
        self._music_directory_cache = {}
        scan_roots = [root] if targets is None else self._target_entries(root, targets)
        self._set_stage(
            job_id,
            "Discovering music albums",
            root=str(root),
            targetCount=len(scan_roots),
            current=0,
            total=len(scan_roots),
            unit="roots",
        )
        inspected_files = 0
        group_count = 0
        count = 0
        groups: dict[tuple[str, ...], list[tuple[Path, dict[str, str]]]] = {}
        previous_inventory = self._music_inventory_rows(library_id, targets)
        current_inventory_paths: set[str] = set()
        service = None
        ingest = None
        last_progress = time.monotonic()

        def inspect_audio(path: Path, file_stat: os.stat_result) -> None:
            relative_path = relative(str(root), str(path))
            path_key = _path_key(relative_path)
            current_inventory_paths.add(path_key)
            cached = self._music_inventory_lookup(library_id, path_key, file_stat)
            previous_group_key = cached[1] if cached else None
            cached_entity_id = cached[3] if cached else None
            if cached and cached[2]:
                tags = dict(cached[0])
                probe = None
                changed = False
            else:
                parsed = parse_audio_tags(path)
                tags = dict(parsed)
                probe = getattr(parsed, "probe", None)
                changed = True
            group_key = _music_group_key(root, path, tags)
            if changed:
                self._music_dirty_group_keys.add(group_key)
            if previous_group_key and previous_group_key != group_key:
                self._music_dirty_group_keys.add(previous_group_key)
            self._music_file_observations[path_key] = MusicFileObservation(
                relative_path,
                file_stat,
                probe,
                changed,
                previous_group_key,
                cached_entity_id,
            )
            if not cached or not cached[2]:
                self._music_inventory_upsert(
                    library_id,
                    root,
                    path,
                    file_stat,
                    tags,
                    group_key,
                )
            groups.setdefault(group_key, []).append((path, tags))

        def flush_group(
            group_key: tuple[str, ...],
            group_entries: list[tuple[Path, dict[str, str]]],
        ) -> None:
            nonlocal group_count, count, service, ingest
            if not group_entries:
                return
            self._check_termination(should_terminate)
            changed_before = set(self._scan_delta.get("changed", set()))
            created_before = set(self._scan_created_ids)
            provider_changed_before = set(self._scan_provider_identity_changed)
            artist, release, tracks, indexed_count = self._index_music_group(
                library_id,
                root,
                job_id,
                should_terminate,
                group_entries,
                self._music_artist_entities,
                self._music_release_entities,
                group_key=group_key,
            )
            track_ids = {
                str(track["entity_id"])
                for track in tracks
                if isinstance(track, dict) and track.get("entity_id")
            }
            group_entity_ids = {artist, release, *track_ids}
            group_dirty = group_key in self._music_dirty_group_keys
            group_dirty = group_dirty or bool(
                group_entity_ids
                & (set(self._scan_delta.get("changed", set())) - changed_before)
            )
            group_dirty = group_dirty or bool(
                group_entity_ids & (set(self._scan_created_ids) - created_before)
            )
            group_dirty = group_dirty or bool(
                group_entity_ids
                & (set(self._scan_provider_identity_changed) - provider_changed_before)
            )
            group_dirty = group_dirty or self._music_group_needs_metadata(
                artist, release, tracks
            )
            if not group_dirty:
                self._scan_refresh_root_ids.add(artist)
                self._publish_root(artist)
                self._flush_publications()
                group_count += 1
                count += indexed_count
                self.store.update_job(
                    job_id,
                    progress_current=group_count,
                    progress_total=max(group_count, 1),
                    message=f"Indexed unchanged music album {group_count}",
                )
                return
            self._music_dirty_release_ids.add(release)
            if service is None:
                from app.metadata_services import MetadataIngestService
                from app.providers import MetadataService

                service = MetadataService()
                ingest = MetadataIngestService(service, background_assets=False)
            artist_local = self._music_local_metadata.get(artist) or {}
            self._persist_music_local_artist(
                artist,
                _music_display_value(artist_local.get("title"))
                or _music_display_value(artist_local.get("albumArtist")),
                ingest,
            )
            has_metadata_context = any(
                tags.get("ALBUM")
                or tags.get("ALBUMARTIST")
                or _music_ids(tags, "release")
                or _music_ids(tags, "track")
                for _, tags in group_entries
            )
            try:
                if has_metadata_context:
                    self._set_stage(
                        job_id,
                        f"Resolving music album {group_count + 1}",
                        entityId=release,
                        current=group_count,
                        total=max(group_count + 1, 1),
                    )
                    self._resolve_music_group(
                        library_id,
                        root,
                        job_id,
                        should_terminate,
                        artist,
                        release,
                        tracks,
                        service,
                        ingest,
                    )
                else:
                    self.db.execute(
                        "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_metadata',updated_at=? WHERE id IN (?,?)",
                        (now(), artist, release),
                    )
                    self._materialize_music_artist_credits(
                        library_id,
                        artist,
                        release,
                        tracks,
                        None,
                        ingest,
                        job_id,
                        should_terminate,
                    )
                    self._seed_all_children(
                        library_id,
                        service,
                        job_id,
                        should_terminate,
                        parent_id=release,
                    )
                    self._enrich_music_lastfm_group(
                        library_id,
                        artist,
                        release,
                        tracks,
                        service,
                        ingest,
                        job_id,
                        should_terminate,
                    )
                    self._extract_and_reproject(release, "release", should_terminate)
                    self._extract_and_reproject(artist, "artist", should_terminate)
            except JobTerminated:
                raise
            except Exception as error:
                # Admission must remain independent from provider availability.
                # The album and its tracks are already playable at this point;
                # retain their local documents and let repair retry metadata.
                logger.warning(
                    "music album metadata failed; continuing library_id=%s release_id=%s error=%s",
                    library_id,
                    release,
                    error,
                    exc_info=True,
                )
                self.db.execute(
                    "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='local_metadata',updated_at=? WHERE id IN (?,?)",
                    (now(), artist, release),
                )
                try:
                    self._materialize_music_artist_credits(
                        library_id,
                        artist,
                        release,
                        tracks,
                        None,
                        ingest,
                        job_id,
                        should_terminate,
                    )
                except Exception:
                    logger.exception(
                        "local music artist credit materialization failed release_id=%s",
                        release,
                    )
                self._queue_metadata_repair(
                    release,
                    library_id,
                    job_id,
                    f"MusicBrainz album resolution failed; local metadata retained: {error}",
                    ingest.locales() if ingest else None,
                )
                self._seed_all_children(
                    library_id,
                    service,
                    job_id,
                    should_terminate,
                    parent_id=release,
                )
                self._enrich_music_lastfm_group(
                    library_id,
                    artist,
                    release,
                    tracks,
                    service,
                    ingest,
                    job_id,
                    should_terminate,
                )
                self._extract_and_reproject(release, "release", should_terminate)
                self._extract_and_reproject(artist, "artist", should_terminate)
            self._repersist_nfo_metadata(
                [artist, release]
                + [
                    str(track.get("entity_id"))
                    for track in tracks
                    if isinstance(track, dict) and track.get("entity_id")
                ]
            )
            self._scan_refresh_root_ids.add(artist)
            self._publish_root(artist)
            self._flush_publications()
            group_count += 1
            count += indexed_count
            self.store.update_job(
                job_id,
                progress_current=group_count,
                progress_total=max(group_count, 1),
                message=f"Indexed music album {group_count}",
            )

        for root_index, scan_root in enumerate(scan_roots, start=1):
            self._check_termination(should_terminate)
            try:
                is_directory = scan_root.is_dir()
            except OSError:
                is_directory = False
            if not is_directory:
                # The normal watcher promotes a root-level audio file to a
                # full scan, but keep targeted scans safe for callers that
                # provide a file directly.  The music inventory is allowed
                # to admit that one file; it must not call os.walk(file).
                try:
                    file_stat = scan_root.stat()
                except OSError:
                    file_stat = None
                if (
                    file_stat is not None
                    and scan_root.suffix.lower() in AUDIO_EXTENSIONS
                    and stat.S_ISREG(file_stat.st_mode)
                ):
                    inspected_files += 1
                    inspect_audio(scan_root, file_stat)
                else:
                    self._defer_root(
                        relative(str(root), str(scan_root)),
                        "music root is inaccessible",
                    )
                self._update_stage_progress(
                    job_id,
                    current=root_index,
                    total=len(scan_roots),
                    item=scan_root.name,
                    files=inspected_files,
                    message=f"Discovered {inspected_files} music files",
                )
                continue
            try:
                for path, file_stat in self._walk_file_entries(scan_root):
                    self._check_termination(should_terminate)
                    if (
                        path.suffix.lower() not in AUDIO_EXTENSIONS
                        or file_stat is None
                        or not stat.S_ISREG(file_stat.st_mode)
                    ):
                        continue
                    inspected_files += 1
                    inspect_audio(path, file_stat)
                    if (
                        inspected_files % 250 == 0
                        or time.monotonic() - last_progress >= 2.0
                    ):
                        self._update_stage_progress(
                            job_id,
                            current=root_index,
                            total=len(scan_roots),
                            item=path.name,
                            files=inspected_files,
                            message=f"Discovered {inspected_files} music files",
                        )
                        last_progress = time.monotonic()
            except OSError:
                self._record_access_error(scan_root)
            for inaccessible in list(self._scan_access_errors):
                try:
                    inaccessible.relative_to(scan_root)
                except (ValueError, RuntimeError):
                    continue
                self._defer_root(
                    relative(str(root), str(inaccessible)),
                    "music path could not be inspected",
                )

            self._update_stage_progress(
                job_id,
                current=root_index,
                total=len(scan_roots),
                item=scan_root.name,
                files=inspected_files,
                message=f"Discovered {inspected_files} music files",
            )
        for path_key, row in previous_inventory.items():
            if path_key in current_inventory_paths:
                continue
            previous_group_key = _music_group_key_from_text(row[7])
            if previous_group_key is None:
                continue
            if _top_level_key(_path_key(row[0] or "")) in {
                _top_level_key(_path_key(value)) for value in self._scan_deferred_roots
            }:
                continue
            self._music_dirty_group_keys.add(previous_group_key)
        # Publish only after the complete inventory has been classified.  A
        # filesystem walk is not an album boundary: files from one release
        # can be interleaved with another release and can span directories.
        for group_key in sorted(
            groups, key=lambda value: tuple(str(part) for part in value)
        ):
            self._check_termination(should_terminate)
            flush_group(group_key, groups[group_key])
        self._remove_orphan_music_artists(library_id)
        self._music_inventory_prune(
            library_id,
            current_inventory_paths,
            previous_inventory,
            targets,
        )
        self._scan_complete = True
        return count

    def _collection_source_rows(
        self,
        sources: Iterable[str],
        provider: str,
        identifier_types: Iterable[str],
    ) -> list[tuple[str, str, str]]:
        source_ids = list(dict.fromkeys(sources))
        types = list(dict.fromkeys(identifier_types))
        if not source_ids or not types:
            return []
        source_placeholders = ",".join("?" for _ in source_ids)
        type_placeholders = ",".join("?" for _ in types)
        return self.db.execute(
            "SELECT e.id,e.entity_type,p.provider_id "
            "FROM library_entities e "
            "JOIN entity_provider_ids p ON p.entity_id=e.id "
            f"WHERE e.library_id IN ({source_placeholders}) "
            "AND p.provider=? "
            f"AND p.identifier_type IN ({type_placeholders}) "
            "ORDER BY e.id,p.provider_id",
            [*source_ids, provider, *types],
        )

    def _discover_tvdb_collections(
        self,
        client,
        source_rows: Iterable[tuple[str, str, str]],
        should_terminate: Callable[[], bool],
    ) -> tuple[dict[str, dict], int]:
        from app.providers import ProviderError

        if not callable(getattr(client, "lists", None)) or not callable(
            getattr(client, "list_details", None)
        ):
            raise ProviderError("TheTVDB collection endpoints are unavailable")
        by_id = {(row[1], str(row[2])): row[0] for row in source_rows}
        lists: dict[str, dict] = {}
        # Lists are paged; stop at the first short page to avoid unbounded calls.
        for page in range(50):
            self._check_termination(should_terminate)
            page_values = list(client.lists(page) or [])
            for value in page_values:
                if not isinstance(value, dict) or not value.get("isOfficial"):
                    continue
                list_id = value.get("id")
                if list_id is not None:
                    lists[str(list_id)] = value
            if len(page_values) < 100:
                break

        discovered: dict[str, dict] = {}
        for list_id, base in lists.items():
            self._check_termination(should_terminate)
            payload = client.list_details(list_id)
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            if not isinstance(data, dict):
                data = {}
            members = []
            for entity in data.get("entities", []) or []:
                if not isinstance(entity, dict):
                    continue
                movie_id = entity.get("movieId")
                series_id = entity.get("seriesId")
                key = (
                    ("movie", str(movie_id))
                    if movie_id is not None
                    else (("series", str(series_id)) if series_id is not None else None)
                )
                if key and key in by_id:
                    members.append(by_id[key])
            members = list(dict.fromkeys(members))
            if len(members) < 2:
                continue
            title = base.get("name") or data.get("name") or f"Collection {list_id}"
            discovered[list_id] = {"members": members, "title": title, "data": data}
        return discovered, len(lists)

    def _discover_tmdb_collections(
        self,
        service,
        client,
        source_rows: Iterable[tuple[str, str, str]],
        locales: list[str],
        should_terminate: Callable[[], bool],
    ) -> tuple[dict[str, dict], int]:
        from app.metadata_services import metadata_task_results
        from app.providers import ProviderError

        if not callable(getattr(client, "collection_details", None)) and not callable(
            getattr(client, "details", None)
        ):
            raise ProviderError("TMDB collection endpoints are unavailable")
        movie_entities: dict[str, list[str]] = {}
        for entity_id, _entity_type, provider_id in source_rows:
            movie_entities.setdefault(str(provider_id), []).append(entity_id)
        if not movie_entities:
            return {}, 0
        if not locales:
            raise ProviderError("No metadata language is configured")

        discovery_locale = locales[0]
        missing_references = []
        for provider_id in movie_entities:
            cached = service.cache.get_locales("tmdb", "movie", provider_id)
            document = cached.get(discovery_locale)
            if not document or "collectionRef" not in document:
                missing_references.append(provider_id)

        def backfill_reference(provider_id: str):
            # The collection relationship is language-independent. Backfill
            # only the first configured locale so legacy caches become usable
            # without refetching every movie's complete localized metadata.
            return service.fetch_locales(
                "tmdb",
                "movie",
                provider_id,
                [discovery_locale],
                force=True,
                project=False,
            )

        errors = []
        for provider_id, _result, error in metadata_task_results(
            sorted(missing_references), backfill_reference, should_terminate
        ):
            self._check_termination(should_terminate)
            if error is not None:
                errors.append((provider_id, error))
        self._check_termination(should_terminate)
        if errors:
            provider_id, error = errors[0]
            raise ProviderError(
                f"TMDB movie collection-reference backfill failed for "
                f"{len(errors)} movie(s); first provider_id={provider_id}: {error}"
            )

        grouped: dict[str, list[tuple[str, str]]] = {}
        for provider_id, entity_ids in movie_entities.items():
            cached = service.cache.get_locales("tmdb", "movie", provider_id)
            document = cached.get(discovery_locale)
            if not document or "collectionRef" not in document:
                raise ProviderError(
                    f"TMDB movie collection-reference cache missing provider_id={provider_id}"
                )
            reference = document.get("collectionRef")
            if not isinstance(reference, dict):
                continue
            collection_id = reference.get("id")
            if collection_id is None or not str(collection_id).strip():
                continue
            grouped.setdefault(str(collection_id), []).extend(
                (provider_id, entity_id) for entity_id in entity_ids
            )

        self._check_termination(should_terminate)
        details = getattr(client, "collection_details", None)
        if not callable(details):
            details = lambda provider_id, locale: client.details(
                "collection", provider_id, locale
            )
        discovered: dict[str, dict] = {}
        for collection_id, local_members in grouped.items():
            self._check_termination(should_terminate)
            payload = details(collection_id, discovery_locale)
            if not isinstance(payload, dict):
                raise ProviderError(
                    f"TMDB collection details returned an invalid payload "
                    f"provider_id={collection_id}"
                )
            by_movie_id: dict[str, list[str]] = {}
            for movie_id, entity_id in local_members:
                by_movie_id.setdefault(movie_id, []).append(entity_id)
            ordered_members = []
            seen_entities = set()
            for part in payload.get("parts", []) or []:
                if not isinstance(part, dict) or part.get("id") is None:
                    continue
                for entity_id in by_movie_id.get(str(part["id"]), []):
                    if entity_id not in seen_entities:
                        seen_entities.add(entity_id)
                        ordered_members.append(entity_id)
            # Keep locally indexed movies that TMDB has not yet included in
            # its parts response, with a deterministic fallback order.
            for movie_id in sorted(by_movie_id):
                for entity_id in by_movie_id[movie_id]:
                    if entity_id not in seen_entities:
                        seen_entities.add(entity_id)
                        ordered_members.append(entity_id)
            if len(ordered_members) < 2:
                continue
            title = payload.get("name") or f"Collection {collection_id}"
            discovered[collection_id] = {
                "members": ordered_members,
                "title": title,
                "data": payload,
            }
        return discovered, len(grouped)

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
        self.store.begin_progress(job_id, "collection_rebuild")
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
            from app.metadata_services import MetadataIngestService
            from app.providers import MetadataService, ProviderError

            service = MetadataService()
            collection_locales = MetadataIngestService(service).locales()
            tvdb_source_rows = self._collection_source_rows(
                sources, "tvdb", ("series", "movie")
            )
            tmdb_source_rows = self._collection_source_rows(sources, "tmdb", ("movie",))
            discovered: list[tuple[str, str, dict]] = []
            successful_providers = set()
            provider_errors = []
            progress_total = 0

            for provider, discover in (
                (
                    "tvdb",
                    lambda client: self._discover_tvdb_collections(
                        client, tvdb_source_rows, should_terminate
                    ),
                ),
                (
                    "tmdb",
                    lambda client: self._discover_tmdb_collections(
                        service,
                        client,
                        tmdb_source_rows,
                        collection_locales,
                        should_terminate,
                    ),
                ),
            ):
                try:
                    result, provider_total = discover(service.client(provider))
                except JobTerminated:
                    raise
                except Exception as error:
                    provider_errors.append((provider, error))
                    logger.warning(
                        "collection provider enumeration failed provider=%s",
                        provider,
                        exc_info=True,
                    )
                    continue
                successful_providers.add(provider)
                progress_total += provider_total
                discovered.extend(
                    (provider, provider_id, value)
                    for provider_id, value in result.items()
                )

            if not successful_providers:
                reasons = ", ".join(
                    f"{provider}: {type(error).__name__}"
                    for provider, error in provider_errors
                )
                suffix = f" ({reasons})" if reasons else ""
                raise ProviderError(
                    f"No collection provider could be enumerated{suffix}"
                )

            # Provider enumeration is complete at this point. Only now mutate
            # the collection inventory, so a partial/failing provider response
            # cannot erase the previous catalog for that provider.
            ingest = MetadataIngestService(service)
            count = 0
            for provider, provider_id, value in discovered:
                self._check_termination(should_terminate)
                path = (
                    f"tvdb-list-{provider_id}"
                    if provider == "tvdb"
                    else f"tmdb-collection-{provider_id}"
                )
                collection = self._entity(library_id, None, "collection", path)
                self._scan_refresh_root_ids.add(collection)
                self._replace_ids(collection, [(provider, "collection", provider_id)])
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
                try:
                    ingest.ingest_locales(
                        provider,
                        "collection",
                        provider_id,
                        collection_locales,
                        force=False,
                    )
                except Exception:
                    for locale in collection_locales:
                        normalized = {
                            "title": value["title"],
                            "overview": value["data"].get("overview"),
                            "provider": provider,
                            "providerId": provider_id,
                            "images": [],
                        }
                        service.cache.put(
                            provider, "collection", provider_id, locale, normalized
                        )
                count += 1
                self.store.update_job(
                    job_id, progress_current=count, message=f"Derived {value['title']}"
                )
            provider_placeholders = ",".join("?" for _ in successful_providers)
            stale_params = [library_id, *sorted(successful_providers)]
            stale_filter = ""
            if self._scan_seen_ids:
                seen_placeholders = ",".join("?" for _ in self._scan_seen_ids)
                stale_filter = f" AND e.id NOT IN ({seen_placeholders})"
                stale_params.extend(sorted(self._scan_seen_ids))
            stale = [
                row[0]
                for row in self.db.execute(
                    "SELECT DISTINCT e.id FROM library_entities e "
                    "JOIN entity_provider_ids p ON p.entity_id=e.id "
                    "WHERE e.library_id=? AND e.entity_type='collection' "
                    "AND p.identifier_type='collection' "
                    f"AND p.provider IN ({provider_placeholders}){stale_filter}",
                    stale_params,
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
                progress_total=progress_total,
                finished_at=now(),
                message=(
                    f"Derived {count} collections"
                    + (
                        "; preserved "
                        + ", ".join(provider.upper() for provider, _ in provider_errors)
                        + " inventory after provider failure"
                        if provider_errors
                        else ""
                    )
                ),
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
        finally:
            self.store.end_progress(job_id)


def _music_filename_parts(path: Path) -> tuple[str, int | None, int | None]:
    """Return a clean title and numeric positions from a music filename.

    Music libraries commonly omit embedded tags and use either ``01. Title``
    or ``1.01. Title``.  The numeric prefix is inventory metadata, not part of
    the track title.  Keep other filenames unchanged so the fallback remains
    deterministic and does not reinterpret ordinary titles.
    """
    return music_filename_parts(path)


def _inventory_query(relative_path: str) -> tuple[str, str | None]:
    path = Path(relative_path)
    raw = path.stem if path.suffix else path.name
    filename_title = raw
    if path.suffix.lower() in AUDIO_EXTENSIONS:
        filename_title, _, _ = _music_filename_parts(path)
    parsed = guess_media(path)
    query = str(parsed.get("title") or filename_title)
    if filename_title != raw:
        query = filename_title
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


def _music_tag_values(tags: dict[str, str], key: str) -> list[str]:
    return [
        value.strip()
        for value in re.split(r"[;\x00]", str(tags.get(key) or ""))
        if value.strip()
    ]


def _music_artist_credits(
    tags: dict[str, str], fallback_name: str | None = None
) -> list[dict[str, str]]:
    """Return ordered atomic local artist credits without guessing joins."""
    names = _music_tag_values(tags, "ARTIST")
    provider_ids = _music_tag_values(tags, "MUSICBRAINZ_ARTISTID")
    # A single joined name plus several IDs has no safe name boundary. Keep
    # the name intact and let provider metadata enrich it later rather than
    # manufacturing a credit-to-ID pairing.
    pair_ids = provider_ids if len(names) != 1 or len(provider_ids) <= 1 else []
    credits: list[dict[str, str]] = []
    for index, name in enumerate(names):
        credit = {"name": name}
        if index < len(pair_ids):
            credit["id"] = pair_ids[index]
        credits.append(credit)
    if not credits and fallback_name:
        name = _music_display_value(fallback_name)
        if name:
            credits.append({"name": name})
    return credits


def _music_primary_artist_credit(
    tags: dict[str, str], fallback_name: str | None = None
) -> dict[str, str] | None:
    """Select the one atomic artist that owns a local release."""
    album_artist_ids = _music_tag_values(tags, "MUSICBRAINZ_ALBUMARTISTID")
    album_artist_names = _music_tag_values(tags, "ALBUMARTIST")
    credits = _music_artist_credits(tags, fallback_name=None)

    if album_artist_ids:
        primary_id = album_artist_ids[0]
        if len(album_artist_names) > 1:
            # Plural album-artist tags establish the ordered name boundary;
            # pair the first one with the first ordered embedded ID.
            return {"name": album_artist_names[0], "id": primary_id}
        matching = next(
            (credit for credit in credits if credit.get("id") == primary_id), None
        )
        if matching:
            return dict(matching)
        if album_artist_names:
            named = next(
                (
                    credit
                    for credit in credits
                    if _music_normalize(credit.get("name"))
                    == _music_normalize(album_artist_names[0])
                ),
                None,
            )
            if named:
                named = dict(named)
                named["id"] = primary_id
                return named
        if credits:
            # The ordered album-artist ID establishes which structured name
            # is primary even when the local name and ID fields were written
            # by different tag writers.
            return {"name": credits[0]["name"], "id": primary_id}
        if album_artist_names:
            return {"name": album_artist_names[0], "id": primary_id}

    # Multiple structured credits establish atomic boundaries. Prefer their
    # first entry over an otherwise ambiguous joined album-artist scalar.
    if len(credits) > 1:
        return dict(credits[0])

    if len(album_artist_names) > 1:
        # A structured plural album-artist field can disambiguate a joined
        # scalar artist field even when the file has no provider IDs.
        return {"name": album_artist_names[0]}

    if album_artist_names:
        primary_name = album_artist_names[0]
        matching = next(
            (
                credit
                for credit in credits
                if _music_normalize(credit.get("name"))
                == _music_normalize(primary_name)
            ),
            None,
        )
        return dict(matching) if matching else {"name": primary_name}
    if credits:
        return dict(credits[0])
    fallback = _music_display_value(fallback_name)
    return {"name": fallback} if fallback else None


def _music_display_value(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _music_album_type_values(tags: dict[str, str]) -> tuple[str | None, list[str]]:
    primary_values = _music_tag_values(tags, "ALBUMTYPE")
    all_values = _music_tag_values(tags, "ALBUMTYPES")
    secondary_values = _music_tag_values(tags, "ALBUMSECONDARYTYPES")
    primary = _music_display_value(primary_values[0]) if primary_values else None
    if not primary and all_values:
        primary = _music_display_value(all_values.pop(0))
    secondary = [*all_values, *secondary_values]
    unique_secondary: list[str] = []
    for value in secondary:
        normalized = _music_display_value(value)
        if normalized and normalized.casefold() != (primary or "").casefold():
            if normalized.casefold() not in {
                existing.casefold() for existing in unique_secondary
            }:
                unique_secondary.append(normalized)
    return primary, unique_secondary


def _music_album_identity_values(
    root: Path, path: Path, tags: dict[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the grouping key and its tag-only identity alias.

    MusicBrainz release IDs are useful disambiguators, but the remaining tag
    values are retained as an alias so an incremental scan can connect a file
    that has lost its ID to the same release.  The key intentionally contains
    no directory component: folders are traversal targets, not album
    identity.
    """
    release_ids = tuple(dict.fromkeys(_music_tag_values(tags, "MUSICBRAINZ_ALBUMID")))
    relative_path = Path(relative(str(root), str(path)))
    fallback_artist = (
        relative_path.parts[0] if relative_path.parts else path.parent.name
    )
    album = _music_normalize(
        _music_display_value(tags.get("ALBUM")) or path.parent.name
    )
    primary = _music_primary_artist_credit(tags, fallback_name=fallback_artist)
    album_artist_ids = _music_tag_values(tags, "MUSICBRAINZ_ALBUMARTISTID")
    artist_id = album_artist_ids[0] if album_artist_ids else (primary or {}).get("id")
    artist_key = (
        f"id:{artist_id}"
        if artist_id
        else f"name:{_music_normalize((primary or {}).get('name') or fallback_artist)}"
    )
    version_values = _music_tag_values(tags, "ALBUMVERSION")
    version = _music_normalize(version_values[0]) if version_values else ""
    date_values = _music_tag_values(tags, "DATE")
    date = _music_normalize(date_values[0]) if date_values else ""
    album_type, _secondary_types = _music_album_type_values(tags)
    album_type_key = _music_normalize(album_type)
    tag_key = ("tag", artist_key, album, version, album_type_key, date)
    if len(release_ids) > 1:
        # Several release IDs in one file are not an ordered identity list;
        # choosing the first would make the result depend on tag order.
        # Keep the conflict isolated from valid files and let the scanner
        # retain local playback metadata while reporting the evidence.
        return (
            (
                "conflict",
                "release_ids",
                *sorted(release_ids),
                artist_key,
                album,
                version,
                album_type_key,
                date,
            ),
            tag_key,
        )
    if release_ids:
        # Artist and album keep an incorrectly copied release ID from silently
        # joining an unrelated album. The surrounding tag context is part of
        # the grouping key, so incompatible copies cannot merge silently.
        return (
            ("id", release_ids[0], artist_key, album, version, album_type_key, date),
            tag_key,
        )
    return tag_key, tag_key


def _music_identity_key_text(key: tuple[str, ...]) -> str:
    """Serialize a normalized identity key without platform-specific paths."""
    return json.dumps(list(key), ensure_ascii=False, separators=(",", ":"))


def _music_normalize(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", _music_display_value(value))
    return re.sub(r"\s+", " ", normalized).casefold()


def _music_group_key(root: Path, path: Path, tags: dict[str, str]) -> tuple[str, ...]:
    """Return a deterministic, tag-first grouping key for one track."""
    key, _ = _music_album_identity_values(root, path, tags)
    return key


def _music_local_document(
    path: Path,
    tags: dict[str, str],
    entity_type: str,
    *,
    artist_name: str | None = None,
    album_name: str | None = None,
    disc_number: int | None = None,
    track_number: int | None = None,
) -> dict:
    """Build a provider-shaped, artwork-free document from admitted tags.

    This is intentionally only a local inventory fallback.  It gives the
    catalog a stable title/relationship while MusicBrainz is unavailable or
    being repaired; provider artwork still comes from release metadata.
    """
    artist_credits = _music_artist_credits(tags, fallback_name=artist_name)
    primary_credit = _music_primary_artist_credit(tags, fallback_name=artist_name)
    album_artist = primary_credit["name"] if primary_credit else None
    if not artist_credits and primary_credit:
        artist_credits = [dict(primary_credit)]
    filename_title, filename_disc_number, filename_track_number = _music_filename_parts(
        path
    )
    if disc_number is None:
        disc_number = filename_disc_number
    if track_number is None:
        track_number = filename_track_number
    album = _music_display_value(album_name) or _music_display_value(tags.get("ALBUM"))
    title = clean_music_title(_music_display_value(tags.get("TITLE"))) or filename_title
    date = _music_display_value(tags.get("DATE")) or None
    duration_seconds = None
    try:
        parsed_duration = float(tags.get("DURATIONSECONDS") or "")
        if parsed_duration >= 0:
            duration_seconds = parsed_duration
    except (TypeError, ValueError):
        pass
    genres = []
    for key in ("GENRE", "STYLE", "MOOD"):
        genres.extend(_music_tag_values(tags, key))
    genres = list(dict.fromkeys(genres))
    if entity_type == "artist":
        # An artist root represents the one album owner. Participating
        # credits belong on releases/tracks, never on the root document.
        if primary_credit:
            artist_credits = [dict(primary_credit)]
            album_artist = primary_credit["name"]
        title = _music_display_value(album_artist) or title
    elif entity_type == "release":
        title = album or title
    album_type, album_secondary_types = _music_album_type_values(tags)

    values = {
        "title": title,
        "overview": None,
        "description": None,
        "date": date,
        "releaseDate": date,
        "year": str(date or "")[:4] or None,
        "tags": genres,
        "originalLanguage": None,
        "albumArtist": album_artist,
        "artists": artist_credits,
        "contributingArtists": deepcopy(artist_credits),
        "album": album or None,
        "albumId": None,
        "albumType": album_type,
        "albumSecondaryTypes": album_secondary_types,
        "label": _music_display_value(
            tags.get("LABEL") or tags.get("ORGANIZATION") or tags.get("PUBLISHER")
        )
        or None,
        "durationSeconds": duration_seconds,
        "discNumber": disc_number,
        "trackNumber": track_number,
        "tracks": [],
        "provider": "musicbrainz",
        "providerId": None,
        "ids": [],
        "images": [],
        "extraImages": [],
        # Do not make a provider credit/artwork task for every track in a
        # large local library.  Track cards inherit release artwork in the
        # catalog serializer, while contributingArtists remains available.
        "credits": [],
    }
    if entity_type == "track":
        values["tracks"] = [
            {
                "title": title,
                "position": track_number,
                "disc": disc_number,
                "durationSeconds": duration_seconds,
            }
        ]
    return values


def _music_local_artist_document(name: str, artist_id: str) -> dict:
    return {
        "title": name,
        "overview": None,
        "description": None,
        "date": None,
        "releaseDate": None,
        "year": None,
        "tags": [],
        "originalLanguage": None,
        "albumArtist": name,
        "artists": [{"name": name}],
        "contributingArtists": [{"name": name}],
        "album": None,
        "albumId": None,
        "albumType": None,
        "albumSecondaryTypes": [],
        "label": None,
        "durationSeconds": None,
        "discNumber": None,
        "trackNumber": None,
        "tracks": [],
        "provider": "local",
        "providerId": artist_id,
        "ids": [
            {
                "provider": "local",
                "identifierType": "artist",
                "id": artist_id,
            }
        ],
        "images": [],
        "extraImages": [],
        "credits": [],
    }


def _music_ids(
    tags: dict[str, str], entity_type: str | None = None
) -> list[tuple[str, str, str]]:
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
    if entity_type == "artist":
        # The parent artist represents the album artist.  Contributing artist
        # IDs remain structured metadata on the track rather than being
        # incorrectly attached to that parent entity.
        primary = _music_primary_artist_credit(tags)
        if primary and primary.get("id"):
            return [("musicbrainz", "artist", primary["id"])]
        return []
    for key, identifier_type in mapping.items():
        if entity_type == "artist" and identifier_type != "artist":
            continue
        if entity_type == "release" and identifier_type not in {
            "release",
            "release_group",
        }:
            continue
        if entity_type == "track" and identifier_type not in {
            "recording",
            "release_track",
            "work",
        }:
            continue
        for value in _music_tag_values(tags, key):
            ids.append(("musicbrainz", identifier_type, value))
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
        self.store.runtime = self
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
        self._worker_threads: dict[str, threading.Thread] = {}
        self._active_lock = threading.RLock()
        self._root_locks: dict[tuple[str, str], threading.Lock] = {}
        self._root_lock_last_used: dict[tuple[str, str], float] = {}
        self._root_locks_guard = threading.RLock()
        self._inventory_locks: dict[str, threading.Lock] = {}
        self._inventory_locks_guard = threading.RLock()
        self._reconcile_state_lock = threading.RLock()
        self._reconcile_target_cache: dict[str, dict[str, dict[str, object]]] = {}
        self._reconcile_cache_loaded: set[str] = set()
        self._reconcile_pending: dict[str, set[str]] = {}
        self._full_music_scan_due: dict[str, float] = {}
        self._reconcile_table_available: bool | None = None
        self._reconcile_last_flush = 0.0
        self._notification_suppressed_libraries: set[str] = set()

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

    def stop(self, timeout: float = 30.0):
        self.stop_event.set()
        with self._active_lock:
            active = list(self._cancel_events.items())
        for job_id, cancel_event in active:
            cancel_event.set()
            try:
                self.store.update_job(
                    job_id,
                    state="terminating",
                    message="Termination requested during Orchestrator shutdown",
                )
            except Exception:
                logger.warning(
                    "could not mark library job terminating during shutdown job_id=%s",
                    job_id,
                    exc_info=True,
                )
        with self.condition:
            self.condition.notify_all()
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None
        if self.thread:
            self.thread.join(timeout=5)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._active_lock:
                workers = list(getattr(self, "_worker_threads", {}).values())
            workers = [
                worker
                for worker in workers
                if worker is not threading.current_thread() and worker.is_alive()
            ]
            if not workers:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for worker in workers:
                worker.join(timeout=min(0.25, remaining))
                if time.monotonic() >= deadline:
                    break
        with self._active_lock:
            remaining_workers = [
                worker
                for worker in getattr(self, "_worker_threads", {}).values()
                if worker.is_alive() and worker is not threading.current_thread()
            ]
            if remaining_workers:
                logger.warning(
                    "library jobs did not stop before shutdown timeout active=%s",
                    len(remaining_workers),
                )
        self._flush_reconcile_updates(force=True)
        self._watch_paths.clear()
        with self._active_lock:
            self._job_targets.clear()
            self._job_target_revisions.clear()

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
        # Full inventory work and watcher reconciliation retain separate
        # history rows, but the runtime serializes their mutable inventory
        # execution per library before either scanner starts.
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

    def wait_for_job(
        self,
        job_id: str,
        should_terminate: Callable[[], bool] | None = None,
        timeout: float | None = None,
    ) -> dict | None:
        """Wait for one queued/running library job without busy-spinning."""
        should_terminate = should_terminate or (lambda: False)
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            job = self.store.job(job_id)
            if not job or job.get("state") not in ACTIVE_JOB_STATES:
                return job
            if should_terminate():
                self.terminate(job_id)
            remaining = 0.25
            if deadline is not None:
                remaining = min(remaining, max(0.0, deadline - time.monotonic()))
                if remaining <= 0:
                    return job
            with self.condition:
                self.condition.wait(timeout=remaining)

    def suppress_library_notifications(self, library_id: str, suppressed: bool) -> None:
        with self._active_lock:
            if suppressed:
                self._notification_suppressed_libraries.add(library_id)
            else:
                self._notification_suppressed_libraries.discard(library_id)

    def notifications_suppressed(self, library_id: str) -> bool:
        with self._active_lock:
            return library_id in self._notification_suppressed_libraries

    def has_active_inventory_jobs(self) -> bool:
        """Return whether inventory work is queued or running."""
        rows = self.store.db.execute(
            "SELECT 1 FROM library_jobs "
            "WHERE kind IN ('scan','reconcile','collection_rebuild') "
            "AND state IN ('queued','running','terminating') LIMIT 1"
        )
        return bool(rows)

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
        self._ensure_reconcile_state()
        library = self.store.get(library_id)
        if not library:
            return
        root = Path(library["directory"])
        targets: set[str] = set()
        full_music_fallback = False
        for value in paths:
            if not value:
                continue
            try:
                relative_path = Path(value).relative_to(root)
            except ValueError:
                continue
            if relative_path.parts:
                if library.get("type") == "music" and len(relative_path.parts) == 1:
                    try:
                        direct_file = Path(value).is_file()
                    except OSError:
                        direct_file = False
                    if direct_file or Path(value).suffix.casefold() in (
                        AUDIO_EXTENSIONS | NFO_EXTENSIONS | IMAGE_EXTENSIONS
                    ):
                        # A root-level file is not a directory target for the
                        # tag-first music scanner. Full traversal is the safe
                        # fallback for both media and sidecar changes,
                        # including deleted files whose paths no longer exist.
                        full_music_fallback = True
                        continue
                targets.add(relative_path.parts[0])
            else:
                # An event on the library directory itself has no target root
                # to scope. Queue a full traversal as the safe fallback.
                self.enqueue(library_id, "scan")
        if full_music_fallback:
            if self._durable_reconcile_targets_available():
                self._queue_full_music_reconcile(library_id)
            else:
                # Compatibility path for pre-0024 databases. If inventory is
                # already active, retain a follow-up marker so this event is
                # not lost when enqueue() deduplicates the active scan.
                active_inventory = self.store.db.execute(
                    "SELECT 1 FROM library_jobs WHERE library_id=? "
                    "AND kind IN ('scan','reconcile','collection_rebuild') "
                    "AND state IN ('queued','running','terminating') LIMIT 1",
                    (library_id,),
                )
                if active_inventory:
                    self._full_music_scan_due[library_id] = (
                        time.monotonic() + WATCHER_RECONCILE_DEBOUNCE_SECONDS
                    )
                self.enqueue(library_id, "scan")
            return
        if not targets:
            return
        deadline = time.time() + WATCHER_RECONCILE_DEBOUNCE_SECONDS
        timestamp = now()
        has_queue = self._durable_reconcile_targets_available()
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
            self._reconcile_due[library_id] = (
                time.monotonic() + WATCHER_RECONCILE_DEBOUNCE_SECONDS
            )
            with self.condition:
                self.condition.notify_all()
            return
        with self._reconcile_state_lock:
            cache = self._load_reconcile_target_cache(library_id)
            pending = self._reconcile_pending.setdefault(library_id, set())
            new_records: list[dict[str, object]] = []
            for target in targets:
                key = _top_level_key(target)
                record = cache.get(key)
                if record is None:
                    record = {
                        "top_level_root": target,
                        "stored_root": target,
                        "debounce_until": deadline,
                        "event_count": 1,
                        "revision": 1,
                        "first_seen_at": timestamp,
                        "last_seen_at": timestamp,
                    }
                    cache[key] = record
                    new_records.append(dict(record))
                    continue
                record["top_level_root"] = target
                record["debounce_until"] = deadline
                record["event_count"] = int(record["event_count"]) + 1
                record["revision"] = int(record["revision"]) + 1
                record["last_seen_at"] = timestamp
                pending.add(key)
            if new_records:
                self._persist_new_reconcile_targets(library_id, new_records)
        with self.condition:
            self.condition.notify_all()

    def _ensure_reconcile_state(self) -> None:
        if not hasattr(self, "_reconcile_state_lock"):
            self._reconcile_state_lock = threading.RLock()
        if not hasattr(self, "_reconcile_target_cache"):
            self._reconcile_target_cache = {}
        if not hasattr(self, "_reconcile_cache_loaded"):
            self._reconcile_cache_loaded = set()
        if not hasattr(self, "_reconcile_pending"):
            self._reconcile_pending = {}
        if not hasattr(self, "_full_music_scan_due"):
            self._full_music_scan_due = {}
        if not hasattr(self, "_reconcile_table_available"):
            self._reconcile_table_available = None
        if not hasattr(self, "_reconcile_last_flush"):
            self._reconcile_last_flush = 0.0

    def _queue_full_music_reconcile(self, library_id: str) -> None:
        """Persist a full-music fallback as a revisioned watcher target."""
        self._ensure_reconcile_state()
        deadline = time.time() + WATCHER_RECONCILE_DEBOUNCE_SECONDS
        timestamp = now()
        with self._reconcile_state_lock:
            cache = self._load_reconcile_target_cache(library_id)
            key = _top_level_key(MUSIC_FULL_RECONCILE_TARGET)
            record = cache.get(key)
            if record is None:
                record = {
                    "top_level_root": MUSIC_FULL_RECONCILE_TARGET,
                    "stored_root": MUSIC_FULL_RECONCILE_TARGET,
                    "debounce_until": deadline,
                    "event_count": 1,
                    "revision": 1,
                    "first_seen_at": timestamp,
                    "last_seen_at": timestamp,
                }
                cache[key] = record
                self._persist_new_reconcile_targets(library_id, [dict(record)])
            else:
                record["top_level_root"] = MUSIC_FULL_RECONCILE_TARGET
                record["debounce_until"] = deadline
                record["event_count"] = int(record["event_count"]) + 1
                record["revision"] = int(record["revision"]) + 1
                record["last_seen_at"] = timestamp
                self._reconcile_pending.setdefault(library_id, set()).add(key)
        with self.condition:
            self.condition.notify_all()

    def _durable_reconcile_targets_available(self) -> bool:
        self._ensure_reconcile_state()
        available = getattr(self, "_reconcile_table_available", None)
        if available is None:
            available = bool(
                self.store.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_reconcile_targets'"
                )
            )
            self._reconcile_table_available = available
        return available

    def _load_reconcile_target_cache(
        self, library_id: str
    ) -> dict[str, dict[str, object]]:
        self._ensure_reconcile_state()
        cache = self._reconcile_target_cache.setdefault(library_id, {})
        if library_id in self._reconcile_cache_loaded:
            return cache
        rows = self.store.db.execute(
            "SELECT top_level_root,debounce_until,event_count,revision,first_seen_at,last_seen_at "
            "FROM library_reconcile_targets WHERE library_id=?",
            (library_id,),
        )
        for root, deadline, event_count, revision, first_seen, last_seen in rows:
            cache[_top_level_key(root)] = {
                "top_level_root": root,
                "stored_root": root,
                "debounce_until": float(deadline),
                "event_count": int(event_count),
                "revision": int(revision),
                "first_seen_at": first_seen,
                "last_seen_at": last_seen,
            }
        self._reconcile_cache_loaded.add(library_id)
        return cache

    def _persist_new_reconcile_targets(
        self, library_id: str, records: list[dict[str, object]]
    ) -> None:
        if not records:
            return
        with self.store.db.transaction() as cursor:
            cursor.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,))
            if not cursor.fetchone():
                self._reconcile_target_cache.pop(library_id, None)
                self._reconcile_pending.pop(library_id, None)
                return
            for record in records:
                cursor.execute(
                    """
                    INSERT INTO library_reconcile_targets
                        (library_id,top_level_root,debounce_until,event_count,revision,first_seen_at,last_seen_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(library_id,top_level_root) DO UPDATE SET
                        debounce_until=excluded.debounce_until,
                        event_count=excluded.event_count,
                        revision=excluded.revision,
                        first_seen_at=excluded.first_seen_at,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        library_id,
                        record["top_level_root"],
                        record["debounce_until"],
                        record["event_count"],
                        record["revision"],
                        record["first_seen_at"],
                        record["last_seen_at"],
                    ),
                )

    def _flush_reconcile_updates(self, *, force: bool = False) -> None:
        if not self._durable_reconcile_targets_available():
            return
        self._ensure_reconcile_state()
        current = time.monotonic()
        with self._reconcile_state_lock:
            if not force and (
                current - self._reconcile_last_flush
                < WATCHER_RECONCILE_FLUSH_INTERVAL_SECONDS
            ):
                return
            snapshots: list[tuple[str, str, dict[str, object]]] = []
            for library_id, keys in self._reconcile_pending.items():
                cache = self._reconcile_target_cache.get(library_id, {})
                for key in keys:
                    record = cache.get(key)
                    if record is not None:
                        snapshots.append((library_id, key, dict(record)))
            if not snapshots:
                self._reconcile_last_flush = current
                return
            for library_id, key, _record in snapshots:
                self._reconcile_pending.setdefault(library_id, set()).discard(key)

        try:
            with self.store.db.transaction() as cursor:
                existing_libraries: dict[str, bool] = {}
                for library_id, _key, record in snapshots:
                    if library_id not in existing_libraries:
                        cursor.execute(
                            "SELECT 1 FROM libraries WHERE id=?", (library_id,)
                        )
                        existing_libraries[library_id] = bool(cursor.fetchone())
                    if not existing_libraries[library_id]:
                        continue
                    cursor.execute(
                        """
                        UPDATE library_reconcile_targets
                        SET top_level_root=?, debounce_until=?, event_count=?,
                            revision=?, first_seen_at=?, last_seen_at=?
                        WHERE library_id=? AND top_level_root=?
                        """,
                        (
                            record["top_level_root"],
                            record["debounce_until"],
                            record["event_count"],
                            record["revision"],
                            record["first_seen_at"],
                            record["last_seen_at"],
                            library_id,
                            record["stored_root"],
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """
                            INSERT INTO library_reconcile_targets
                                (library_id,top_level_root,debounce_until,event_count,revision,first_seen_at,last_seen_at)
                            VALUES(?,?,?,?,?,?,?)
                            """,
                            (
                                library_id,
                                record["top_level_root"],
                                record["debounce_until"],
                                record["event_count"],
                                record["revision"],
                                record["first_seen_at"],
                                record["last_seen_at"],
                            ),
                        )
        except Exception:
            with self._reconcile_state_lock:
                for library_id, key, _record in snapshots:
                    self._reconcile_pending.setdefault(library_id, set()).add(key)
            logger.exception("durable watcher target flush failed")
            return

        with self._reconcile_state_lock:
            for library_id, key, record in snapshots:
                current_record = self._reconcile_target_cache.get(library_id, {}).get(
                    key
                )
                if current_record is not None:
                    current_record["stored_root"] = record["top_level_root"]
            self._reconcile_last_flush = current

    def _forget_reconcile_targets(
        self, library_id: str, revisions: dict[str, int]
    ) -> None:
        if not revisions:
            return
        self._ensure_reconcile_state()
        with self._reconcile_state_lock:
            cache = self._reconcile_target_cache.get(library_id, {})
            pending = self._reconcile_pending.get(library_id, set())
            for target, revision in revisions.items():
                key = _top_level_key(target)
                record = cache.get(key)
                if record is None or int(record["revision"]) != revision:
                    continue
                cache.pop(key, None)
                pending.discard(key)

    def _root_lock(self, library_id: str, root: str) -> threading.Lock:
        key = (library_id, _top_level_key(root))
        with self._root_locks_guard:
            if not hasattr(self, "_root_lock_last_used"):
                self._root_lock_last_used = {}
            lock = self._root_locks.setdefault(key, threading.Lock())
            self._root_lock_last_used[key] = time.monotonic()
            return lock

    def _inventory_lock(self, library_id: str) -> threading.Lock:
        """Serialize every mutable inventory operation for one library."""
        # A few compatibility callers construct the runtime with ``__new__``
        # against the pre-inventory test schema.  Keep the lock lazy just as
        # the older root-lock state is, while normal construction initializes
        # it eagerly above.
        if not hasattr(self, "_inventory_locks_guard"):
            self._inventory_locks_guard = threading.RLock()
        if not hasattr(self, "_inventory_locks"):
            self._inventory_locks = {}
        with self._inventory_locks_guard:
            return self._inventory_locks.setdefault(library_id, threading.Lock())

    def prune_runtime_state(self) -> None:
        """Drop locks and watcher caches for libraries no longer present."""
        try:
            library_ids = {
                row[0] for row in self.store.db.read_execute("SELECT id FROM libraries")
            }
        except Exception:
            return
        try:
            queued_job_ids = {
                row[0]
                for row in self.store.db.read_execute(
                    "SELECT id FROM library_jobs WHERE state='queued'"
                )
            }
        except Exception:
            queued_job_ids = set()
        with self._active_lock:
            active_job_ids = set(getattr(self, "_active_jobs", set()))
            for mapping in (
                getattr(self, "_job_targets", {}),
                getattr(self, "_job_target_revisions", {}),
            ):
                for job_id in list(mapping):
                    if job_id not in active_job_ids and job_id not in queued_job_ids:
                        mapping.pop(job_id, None)
        with self._root_locks_guard:
            for key in list(self._root_locks):
                lock = self._root_locks.get(key)
                if key[0] not in library_ids and lock is not None and not lock.locked():
                    self._root_locks.pop(key, None)
                    getattr(self, "_root_lock_last_used", {}).pop(key, None)
            if len(self._root_locks) > 4096:
                last_used = getattr(self, "_root_lock_last_used", {})
                for key in sorted(
                    self._root_locks,
                    key=lambda item: last_used.get(item, 0.0),
                ):
                    if len(self._root_locks) <= 2048:
                        break
                    lock = self._root_locks.get(key)
                    if lock is None or lock.locked():
                        continue
                    self._root_locks.pop(key, None)
                    last_used.pop(key, None)
        with self._inventory_locks_guard:
            for library_id in list(self._inventory_locks):
                lock = self._inventory_locks[library_id]
                if library_id not in library_ids and not lock.locked():
                    self._inventory_locks.pop(library_id, None)
        with self._reconcile_state_lock:
            active_libraries = {
                library_id for library_id, _ in self._active_jobs_by_library()
            }
            for mapping in (
                self._reconcile_due,
                self._reconcile_targets,
                self._reconcile_target_cache,
                self._reconcile_pending,
            ):
                for library_id in list(mapping):
                    if (
                        library_id not in library_ids
                        and library_id not in active_libraries
                    ):
                        mapping.pop(library_id, None)
            protected = active_libraries | set(self._reconcile_pending)
            for mapping in (
                self._reconcile_due,
                self._reconcile_targets,
                self._reconcile_target_cache,
            ):
                for library_id in list(mapping):
                    if len(mapping) <= 1024:
                        break
                    if library_id in protected:
                        continue
                    mapping.pop(library_id, None)
                    if mapping is self._reconcile_target_cache:
                        self._reconcile_cache_loaded.discard(library_id)
            self._reconcile_cache_loaded.intersection_update(library_ids)

    def _active_jobs_by_library(self):
        rows = []
        with self._active_lock:
            job_ids = set(getattr(self, "_active_jobs", set()))
        for job_id in job_ids:
            row = self.store.db.execute(
                "SELECT library_id FROM library_jobs WHERE id=?", (job_id,)
            )
            if row:
                rows.append((row[0][0], job_id))
        return rows

    def _acquire_roots(self, library_id: str, roots: set[str]):
        # Keep the map guard while acquiring so a retention pass cannot evict
        # a lock after one worker has looked it up but before it owns it.
        with self._root_locks_guard:
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
            # Keep the durable first-seen target cheap, then batch the noisy
            # follow-up event counters/revisions before the due-target query.
            self._flush_reconcile_updates()
            has_queue = self._durable_reconcile_targets_available()
            if has_queue:
                due_rows = self.store.db.execute(
                    "SELECT DISTINCT library_id FROM library_reconcile_targets WHERE debounce_until<=?",
                    (time.time(),),
                )
                for (library_id,) in due_rows:
                    self.enqueue(library_id, "reconcile")
            else:
                full_music_scan_due = getattr(self, "_full_music_scan_due", {})
                full_due = [
                    library_id
                    for library_id, deadline in full_music_scan_due.items()
                    if time.monotonic() >= deadline
                ]
                for library_id in full_due:
                    active_inventory = self.store.db.execute(
                        "SELECT 1 FROM library_jobs WHERE library_id=? "
                        "AND kind IN ('scan','reconcile','collection_rebuild') "
                        "AND state IN ('queued','running','terminating') LIMIT 1",
                        (library_id,),
                    )
                    if active_inventory:
                        continue
                    self.enqueue(library_id, "scan")
                    full_music_scan_due.pop(library_id, None)
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
            worker = threading.Thread(
                target=self._execute_job,
                args=(job_id, library_id, kind),
                name=f"zenstream-library-{job_id[:8]}",
                daemon=True,
            )
            with self._active_lock:
                self._worker_threads[job_id] = worker
            worker.start()

    def _execute_job(self, job_id: str, library_id: str, kind: str) -> None:
        locks: list[threading.Lock] = []
        inventory_lock: threading.Lock | None = None
        full_music_reconcile = False
        try:
            if kind in {"scan", "reconcile", "collection_rebuild"}:
                inventory_lock = self._inventory_lock(library_id)
                # Full scans and watcher reconciles mutate the same inventory,
                # identity, projection, and cleanup rows. Serialize the
                # complete operation; durable watcher revisions remain queued
                # while a worker waits here.
                inventory_lock.acquire()
                targets = self._job_targets.pop(job_id, None)
                target_revisions: dict[str, int] = {}
                library = self.store.get(library_id) or {}
                if kind == "reconcile" and targets is None:
                    # Watcher callbacks can continue while this worker is
                    # being claimed.  Make the latest batched revision
                    # visible before taking the due-target snapshot.
                    self._flush_reconcile_updates(force=True)
                    has_queue = self._durable_reconcile_targets_available()
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
                if (
                    kind == "reconcile"
                    and library.get("type") == "music"
                    and targets
                    and MUSIC_FULL_RECONCILE_TARGET in targets
                ):
                    # The sentinel is a durable full-inventory fallback for a
                    # root-level music file/sidecar event. A full traversal
                    # may cover ordinary due targets in the same batch too.
                    full_music_reconcile = True
                    targets = None
                if kind == "reconcile" and not targets and not full_music_reconcile:
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
                # A scan owns mutable traversal state and conflict logging. Do not
                # share one scanner between concurrent library workers: a movie
                # scan could otherwise overwrite a TV scan's stage, delta, and
                # heartbeat message.
                scanner = LibraryScanner(self.store)
                scanner.scan(
                    library_id,
                    job_id,
                    self._cancel_events[job_id].is_set,
                    targets=(
                        None if kind != "reconcile" or full_music_reconcile else targets
                    ),
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
            if inventory_lock is not None:
                inventory_lock.release()
            revisions = getattr(self, "_job_target_revisions", {}).pop(job_id, {})
            completed_job = self.store.job(job_id)
            can_acknowledge = not completed_job or completed_job.get("state") not in {
                "failed",
                "terminated",
                "terminating",
            }
            if revisions and can_acknowledge:
                with self.store.db.transaction() as cursor:
                    for target, revision in revisions.items():
                        cursor.execute(
                            "DELETE FROM library_reconcile_targets WHERE library_id=? AND top_level_root=? AND revision=?",
                            (library_id, target, revision),
                        )
                self._forget_reconcile_targets(library_id, revisions)
            with self._active_lock:
                self._active_jobs.discard(job_id)
                self._cancel_events.pop(job_id, None)
                getattr(self, "_worker_threads", {}).pop(job_id, None)
            self._aggregate_scan_state(library_id)
            with self.condition:
                self.condition.notify_all()


runtime = LibraryRuntime()
