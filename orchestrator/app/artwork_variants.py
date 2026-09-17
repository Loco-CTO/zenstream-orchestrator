from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.images import LocalArtworkCache, encode_webp_variant
from app.logging_config import get_logger

logger = get_logger("artwork_variants")

VARIANT_WIDTHS = (160, 320)
ARTWORK_VARIANT_ALGORITHM_VERSION = 1
VARIANT_CACHE_DIRECTORY = "artwork-variant-cache"
PREWARM_MAX_ACTIVE = 2
STALE_PRUNE_INTERVAL_SECONDS = 6 * 60 * 60
ARTWORK_VARIANT_STATES = frozenset(
    {"starting", "warming", "ready", "degraded", "unavailable"}
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_error(error: BaseException | str) -> str:
    message = " ".join(str(error).split())
    message = re.sub(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\s,;]+", "<path>", message)
    if len(message) > 240:
        message = message[:237].rstrip() + "..."
    return message or type(error).__name__


@dataclass(frozen=True)
class ArtworkVariantSource:
    path: Path
    version: str


def source_version(path: Path, version: object = None) -> str:
    value = str(version or "").strip()
    if value:
        return value
    try:
        stat = path.stat()
        return f"stat-{stat.st_size:x}-{stat.st_mtime_ns:x}"
    except OSError:
        return "missing"


def normalize_variant_width(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        width = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Artwork width must be 160 or 320.") from error
    if width not in VARIANT_WIDTHS:
        raise ValueError("Artwork width must be 160 or 320.")
    return width


class ArtworkVariantCache:
    """Persistent, source-versioned artwork variants with no capacity eviction."""

    def __init__(self, db_file: str | None):
        self.root = (
            Path(db_file).parent / VARIANT_CACHE_DIRECTORY
            if db_file and db_file != ":memory:"
            else None
        )
        self._lock = threading.RLock()
        self._pending: dict[str, Future] = {}
        self._last_pruned_at = 0.0
        self._last_files = 0
        self._last_bytes = 0
        self._failed_conversions = 0
        self._last_error: str | None = None

    @staticmethod
    def _key(source: ArtworkVariantSource, width: int) -> str:
        value = (
            f"{source.path}|{source.version}|{width}|"
            f"{ARTWORK_VARIANT_ALGORITHM_VERSION}"
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def path_for(self, source: ArtworkVariantSource, width: int) -> Path | None:
        if self.root is None or width not in VARIANT_WIDTHS:
            return None
        key = self._key(source, width)
        return self.root / key[:2] / f"{key}.webp"

    def get(self, source: ArtworkVariantSource, width: int) -> Path | None:
        target = self.path_for(source, width)
        if target is None:
            return None
        try:
            if not target.is_file() or target.stat().st_size <= 0:
                return None
        except OSError:
            return None
        return target

    def _generate(self, source: ArtworkVariantSource, width: int) -> Path:
        target = self.path_for(source, width)
        if target is None:
            raise RuntimeError("Artwork variants require a persistent database path.")
        existing = self.get(source, width)
        if existing is not None:
            return existing
        if not source.path.is_file():
            raise FileNotFoundError(source.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        encode_webp_variant(source.path, target, width)
        if not target.is_file() or target.stat().st_size <= 0:
            raise RuntimeError("Artwork variant output is empty.")
        return target

    def submit(
        self, source: ArtworkVariantSource, width: int, executor
    ) -> Future | None:
        target = self.path_for(source, width)
        if target is None:
            return None
        existing = self.get(source, width)
        if existing is not None:
            return None
        key = self._key(source, width)
        with self._lock:
            current = self._pending.get(key)
            if current is not None and not current.done():
                return current
        submit = getattr(executor, "try_submit_future", None)
        if submit is None:
            future = executor.submit_future(
                ("artwork-variant", key),
                lambda: self._generate(source, width),
            )
        else:
            future = submit(
                ("artwork-variant", key),
                lambda: self._generate(source, width),
            )
        if future is None:
            return None
        with self._lock:
            self._pending[key] = future

        def finished(_done: Future) -> None:
            error = None
            if _done.cancelled():
                error = "conversion cancelled"
            else:
                try:
                    error = _done.exception()
                except Exception as exception:
                    error = exception
            with self._lock:
                self._pending.pop(key, None)
                if error is not None:
                    self._failed_conversions += 1
                    self._last_error = _safe_error(error)

        future.add_done_callback(finished)
        return future

    def active_keys(self, sources: list[ArtworkVariantSource]) -> set[str]:
        return {
            self._key(source, width) for source in sources for width in VARIANT_WIDTHS
        }

    def prune_stale(self, sources: list[ArtworkVariantSource]) -> int:
        if self.root is None or not self.root.is_dir():
            return 0
        active = self.active_keys(sources)
        removed = 0
        try:
            candidates = list(self.root.rglob("*"))
        except OSError:
            return 0
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                suffix = candidate.suffix.lower()
                remove = candidate.name.startswith(".") or suffix == ".tmp"
                if suffix == ".webp":
                    size = candidate.stat().st_size
                    remove = size <= 0 or candidate.stem not in active
                if remove:
                    candidate.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                logger.debug("could not prune artwork variant path=%s", candidate)
        with self._lock:
            self._last_pruned_at = time.monotonic()
        return removed

    def should_prune_stale(self) -> bool:
        with self._lock:
            return (
                self._last_pruned_at <= 0
                or time.monotonic() - self._last_pruned_at
                >= STALE_PRUNE_INTERVAL_SECONDS
            )

    def diagnostics(self) -> dict[str, int]:
        if self.root is None or not self.root.is_dir():
            with self._lock:
                self._last_files = 0
                self._last_bytes = 0
                pending = len(self._pending)
                failed = self._failed_conversions
            return {
                "files": 0,
                "bytes": 0,
                "pending": pending,
                "failed": failed,
            }
        files = 0
        total = 0
        try:
            for candidate in self.root.rglob("*.webp"):
                try:
                    size = candidate.stat().st_size
                except OSError:
                    continue
                if size > 0:
                    files += 1
                    total += size
        except OSError:
            pass
        with self._lock:
            self._last_files = files
            self._last_bytes = total
            pending = len(self._pending)
            failed = self._failed_conversions
        return {"files": files, "bytes": total, "pending": pending, "failed": failed}

    def status_diagnostics(self) -> dict[str, int | str | None]:
        """Return the last sweep's cache totals without traversing the cache."""
        with self._lock:
            return {
                "files": self._last_files,
                "bytes": self._last_bytes,
                "pending": len(self._pending),
                "failed": self._failed_conversions,
                "last_error": self._last_error,
            }


class ArtworkVariantPrewarmer:
    def __init__(self, cache: ArtworkVariantCache):
        self.cache = cache
        self._lock = threading.RLock()
        self._queue: deque[tuple[ArtworkVariantSource, int]] = deque()
        self._queued: set[str] = set()
        self._active = 0
        self._executor = None
        self._stopped = False
        self._source_count = 0
        self._expected_keys: set[str] = set()
        self._ready_keys: set[str] = set()
        self._failed_keys: set[str] = set()
        self._selection_complete = False
        self._has_sweep = False
        self._state = "starting"
        self._last_sweep_at: str | None = None
        self._last_successful_sweep_at: str | None = None
        self._last_error: str | None = None

    @staticmethod
    def _key(cache: ArtworkVariantCache, source: ArtworkVariantSource, width: int):
        return cache._key(source, width)

    def enqueue(self, sources: list[ArtworkVariantSource], executor) -> None:
        with self._lock:
            if self._stopped:
                return
            self._executor = executor
            for source in sources:
                for width in VARIANT_WIDTHS:
                    if self.cache.get(source, width) is not None:
                        continue
                    key = self._key(self.cache, source, width)
                    if key not in self._queued:
                        self._queued.add(key)
                        self._queue.append((source, width))
            self._pump_locked()

    def mark_sweep_started(self) -> None:
        with self._lock:
            self._state = "warming"

    def update_selection(
        self, sources: list[ArtworkVariantSource], complete: bool
    ) -> None:
        expected_keys = {
            self._key(self.cache, source, width)
            for source in sources
            for width in VARIANT_WIDTHS
        }
        ready_keys = {
            self._key(self.cache, source, width)
            for source in sources
            for width in VARIANT_WIDTHS
            if self.cache.get(source, width) is not None
        }
        with self._lock:
            self._source_count = len(sources)
            self._expected_keys = expected_keys
            self._ready_keys = ready_keys
            self._failed_keys.intersection_update(expected_keys)
            self._queue = deque(
                (source, width)
                for source, width in self._queue
                if self._key(self.cache, source, width) in expected_keys
            )
            self._queued.intersection_update(expected_keys)
            self._selection_complete = complete
            self._state = "warming" if complete else "unavailable"

    def _update_state_locked(self, cache_failed: int = 0) -> None:
        if not self._selection_complete:
            self._state = "unavailable" if self._has_sweep else "starting"
        elif self._failed_keys or cache_failed:
            self._state = "degraded"
        elif (
            self._active
            or self._queue
            or len(self._ready_keys) < len(self._expected_keys)
        ):
            self._state = "warming"
        else:
            self._state = "ready"

    def _pump_locked(self) -> None:
        while self._active < PREWARM_MAX_ACTIVE and self._queue and not self._stopped:
            source, width = self._queue.popleft()
            key = self._key(self.cache, source, width)
            self._queued.discard(key)
            if self.cache.get(source, width) is not None:
                self._ready_keys.add(key)
                continue
            future = self.cache.submit(source, width, self._executor)
            if future is None:
                self._queued.add(key)
                self._queue.appendleft((source, width))
                return
            self._active += 1
            future.add_done_callback(
                lambda done, target_key=key: self._finished(target_key, done)
            )

    def _finished(self, key: str, future: Future) -> None:
        succeeded = False
        if not future.cancelled():
            try:
                succeeded = future.exception() is None
            except Exception:
                succeeded = False
        with self._lock:
            self._active = max(0, self._active - 1)
            if key in self._expected_keys:
                if succeeded:
                    self._ready_keys.add(key)
                    self._failed_keys.discard(key)
                else:
                    self._failed_keys.add(key)
                    self._last_error = "Artwork variant conversion failed."
            self._pump_locked()
            self._update_state_locked()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._queue.clear()
            self._queued.clear()

    def diagnostics(self) -> dict[str, int]:
        with self._lock:
            return {"queued": len(self._queue), "active": self._active}

    def complete_sweep(self) -> None:
        cache_status = self.cache.status_diagnostics()
        with self._lock:
            now = _timestamp()
            self._has_sweep = True
            self._last_sweep_at = now
            if self._selection_complete:
                self._last_successful_sweep_at = now
            if self._failed_keys:
                self._last_error = "One or more artwork variant conversions failed."
            elif not self._selection_complete:
                self._last_error = "Artwork selection data is incomplete."
            elif cache_status.get("last_error") is None:
                self._last_error = None
            self._update_state_locked(int(cache_status.get("failed", 0) or 0))

    def record_sweep_error(self, error: BaseException | str) -> None:
        with self._lock:
            self._has_sweep = True
            self._last_sweep_at = _timestamp()
            self._last_error = _safe_error(error)
            self._state = (
                "degraded" if self._last_successful_sweep_at else "unavailable"
            )

    def status(self) -> dict[str, int | str | None]:
        with self._lock:
            source_count = self._source_count
            expected = len(self._expected_keys)
            ready = min(expected, len(self._ready_keys))
            state = self._state
            queued = len(self._queue)
            active = self._active
            last_sweep_at = self._last_sweep_at
            last_successful_sweep_at = self._last_successful_sweep_at
            last_error = self._last_error
        cache_status = self.cache.status_diagnostics()
        cache_failed = int(cache_status.get("failed", 0) or 0)
        if cache_failed and state in {"starting", "warming", "ready"}:
            state = "degraded"
        return {
            "state": state,
            "sourceCount": source_count,
            "expectedVariants": expected,
            "readyVariants": ready,
            "remainingVariants": max(0, expected - ready),
            "queuedConversions": queued,
            "activeConversions": active,
            "pendingConversions": int(cache_status.get("pending", 0) or 0),
            "cacheFileCount": int(cache_status.get("files", 0) or 0),
            "cacheBytes": int(cache_status.get("bytes", 0) or 0),
            "failedConversions": cache_failed,
            "lastSweepAt": last_sweep_at,
            "lastSuccessfulSweepAt": last_successful_sweep_at,
            "lastError": last_error or cache_status.get("last_error"),
        }


_cache_lock = threading.RLock()
_caches: dict[str, ArtworkVariantCache] = {}
_prewarmers: dict[str, ArtworkVariantPrewarmer] = {}


def cache_for(db_file: str | None) -> ArtworkVariantCache:
    key = str(Path(db_file).resolve()) if db_file else ":memory:"
    with _cache_lock:
        cache = _caches.get(key)
        if cache is None:
            cache = ArtworkVariantCache(db_file)
            _caches[key] = cache
        return cache


def prewarmer_for(db_file: str | None) -> ArtworkVariantPrewarmer:
    key = str(Path(db_file).resolve()) if db_file else ":memory:"
    with _cache_lock:
        prewarmer = _prewarmers.get(key)
        if prewarmer is None:
            prewarmer = ArtworkVariantPrewarmer(cache_for(db_file))
            _prewarmers[key] = prewarmer
        return prewarmer


def selected_sources(db) -> tuple[list[ArtworkVariantSource], bool]:
    sources: dict[tuple[str, str], ArtworkVariantSource] = {}
    try:
        tables = {
            row[0]
            for row in db.read_execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    except Exception:
        return [], False
    if "catalog_artwork_selection" not in tables:
        return [], False
    try:
        rows = db.read_execute(
            "SELECT DISTINCT provider,local_path,version "
            "FROM catalog_artwork_selection WHERE local_path IS NOT NULL"
        )
    except Exception:
        return [], False
    for provider, path, version in rows:
        path = Path(path)
        if provider == "local":
            version = path.stem[:12] or version
        source = ArtworkVariantSource(path, source_version(path, version))
        if source.path.is_file():
            sources[(str(source.path), source.version)] = source

    if "media_files" in tables:
        db_file = getattr(db, "db_file", None)
        local_cache = (
            LocalArtworkCache(db) if isinstance(db_file, (str, Path)) else None
        )
        try:
            local_rows = db.read_execute(
                "SELECT DISTINCT quick_fingerprint FROM media_files "
                "WHERE role='image' AND quick_fingerprint IS NOT NULL"
            )
        except Exception:
            return list(sources.values()), False
        if local_cache is not None:
            for (fingerprint,) in local_rows:
                path = local_cache.path(str(fingerprint or ""))
                if path and path.is_file():
                    version = str(fingerprint).strip()[:12]
                    source = ArtworkVariantSource(path, version)
                    sources[(str(path), version)] = source

    if {"people", "entity_person_credits"}.issubset(tables):
        try:
            people_columns = {
                row[1] for row in db.read_execute("PRAGMA table_info(people)")
            }
            updated_at = "p.updated_at" if "updated_at" in people_columns else "NULL"
            people_rows = db.read_execute(
                f"SELECT DISTINCT p.local_path,{updated_at} FROM people p "
                "JOIN entity_person_credits c ON c.person_id=p.id "
                "WHERE p.local_path IS NOT NULL"
            )
        except Exception:
            return list(sources.values()), False
        for path, updated_at in people_rows:
            version = hashlib.sha256(f"{path}:{updated_at or ''}".encode()).hexdigest()[
                :12
            ]
            source = ArtworkVariantSource(Path(path), version)
            if source.path.is_file():
                sources[(str(source.path), source.version)] = source
    return list(sources.values()), True


def queue_selected(db, executor) -> dict[str, int]:
    db_file = getattr(db, "db_file", None)
    if not isinstance(db_file, (str, Path)):
        db_file = None
    cache = cache_for(db_file)
    prewarmer = prewarmer_for(db_file)
    prewarmer.mark_sweep_started()
    sources, complete = selected_sources(db)
    prewarmer.update_selection(sources, complete)
    pruned = 0
    if complete and cache.should_prune_stale():
        pruned = cache.prune_stale(sources)
    elif not complete:
        logger.debug(
            "skipping artwork variant cleanup because selection data is incomplete"
        )
    prewarmer.enqueue(sources, executor)
    diagnostics = cache.diagnostics()
    diagnostics.update(prewarmer.diagnostics())
    diagnostics["sources"] = len(sources)
    diagnostics["pruned"] = pruned
    prewarmer.complete_sweep()
    return diagnostics


def record_sweep_error(db, error: BaseException | str) -> None:
    db_file = getattr(db, "db_file", None)
    if not isinstance(db_file, (str, Path)):
        db_file = None
    prewarmer_for(db_file).record_sweep_error(error)


def artwork_variant_status(db) -> dict[str, int | str | None]:
    db_file = getattr(db, "db_file", None)
    if not isinstance(db_file, (str, Path)):
        db_file = None
    return prewarmer_for(db_file).status()


def stop_all() -> None:
    with _cache_lock:
        for prewarmer in _prewarmers.values():
            prewarmer.stop()
