from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from app.images import LocalArtworkCache, encode_webp_variant
from app.logging_config import get_logger

logger = get_logger("artwork_variants")

VARIANT_WIDTHS = (160, 320)
ARTWORK_VARIANT_ALGORITHM_VERSION = 1
VARIANT_CACHE_DIRECTORY = "artwork-variant-cache"
PREWARM_MAX_ACTIVE = 2
STALE_PRUNE_INTERVAL_SECONDS = 6 * 60 * 60


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

    def submit(self, source: ArtworkVariantSource, width: int, executor) -> Future | None:
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
            with self._lock:
                self._pending.pop(key, None)

        future.add_done_callback(finished)
        return future

    def active_keys(self, sources: list[ArtworkVariantSource]) -> set[str]:
        return {
            self._key(source, width)
            for source in sources
            for width in VARIANT_WIDTHS
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
            return {"files": 0, "bytes": 0, "pending": len(self._pending)}
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
            pending = len(self._pending)
        return {"files": files, "bytes": total, "pending": pending}


class ArtworkVariantPrewarmer:
    def __init__(self, cache: ArtworkVariantCache):
        self.cache = cache
        self._lock = threading.RLock()
        self._queue: deque[tuple[ArtworkVariantSource, int]] = deque()
        self._queued: set[str] = set()
        self._active = 0
        self._executor = None
        self._stopped = False

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

    def _pump_locked(self) -> None:
        while self._active < PREWARM_MAX_ACTIVE and self._queue and not self._stopped:
            source, width = self._queue.popleft()
            key = self._key(self.cache, source, width)
            self._queued.discard(key)
            if self.cache.get(source, width) is not None:
                continue
            future = self.cache.submit(source, width, self._executor)
            if future is None:
                self._queued.add(key)
                self._queue.appendleft((source, width))
                return
            self._active += 1
            future.add_done_callback(self._finished)

    def _finished(self, _future: Future) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._pump_locked()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._queue.clear()
            self._queued.clear()

    def diagnostics(self) -> dict[str, int]:
        with self._lock:
            return {"queued": len(self._queue), "active": self._active}


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
            LocalArtworkCache(db)
            if isinstance(db_file, (str, Path))
            else None
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
            version = hashlib.sha256(
                f"{path}:{updated_at or ''}".encode("utf-8")
            ).hexdigest()[:12]
            source = ArtworkVariantSource(Path(path), version)
            if source.path.is_file():
                sources[(str(source.path), source.version)] = source
    return list(sources.values()), True


def queue_selected(db, executor) -> dict[str, int]:
    db_file = getattr(db, "db_file", None)
    if not isinstance(db_file, (str, Path)):
        db_file = None
    cache = cache_for(db_file)
    sources, complete = selected_sources(db)
    pruned = 0
    if complete and cache.should_prune_stale():
        pruned = cache.prune_stale(sources)
    elif not complete:
        logger.debug("skipping artwork variant cleanup because selection data is incomplete")
    prewarmer = prewarmer_for(db_file)
    prewarmer.enqueue(sources, executor)
    diagnostics = cache.diagnostics()
    diagnostics.update(prewarmer.diagnostics())
    diagnostics["sources"] = len(sources)
    diagnostics["pruned"] = pruned
    return diagnostics


def stop_all() -> None:
    with _cache_lock:
        for prewarmer in _prewarmers.values():
            prewarmer.stop()
