"""Cross-platform native library monitoring and bounded delta verification."""

from __future__ import annotations

import ctypes
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment,misc]

from app.logging_config import get_logger

logger = get_logger("library_watcher")
DELTA_INTERVAL_SECONDS = 60.0
DELTA_MIN_SECONDS = 5.0
DELTA_MAX_SECONDS = 3600.0
MAX_DELTA_WORKERS = 2
HEALTH_CHECK_SECONDS = 15.0
RESTART_BACKOFF_SECONDS = (1.0, 5.0, 30.0, 300.0)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _configured_backend() -> str:
    value = os.getenv("LIBRARY_WATCH_BACKEND", "auto").strip().lower()
    return value if value in {"auto", "native", "polling"} else "auto"


def _delta_interval() -> float:
    value = os.getenv(
        "LIBRARY_WATCH_DELTA_SECONDS", os.getenv("LIBRARY_WATCH_POLL_SECONDS", "60")
    )
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = DELTA_INTERVAL_SECONDS
    return max(DELTA_MIN_SECONDS, min(DELTA_MAX_SECONDS, value))


def _windows_remote_drive(path: Path) -> bool:
    value = os.fspath(path)
    if value.startswith(("\\\\", "//")):
        return True
    if os.name != "nt" or len(value) < 2 or value[1] != ":":
        return False
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(value[:3]) == 4
    except (AttributeError, OSError):
        return False


def _linux_mount_type(_path: Path) -> str | None:
    """Compatibility probe retained for callers; auto selection is native-first."""
    return None


def _in_container() -> bool:
    return Path("/.dockerenv").exists()


def choose_backend(path: str | Path, requested: str = "auto") -> tuple[str, str]:
    """Use Watchdog native events first; polling means delta-only compatibility mode."""
    requested = requested if requested in {"auto", "native", "polling"} else "auto"
    if requested != "auto":
        return requested, "explicit"
    override = _configured_backend()
    if override != "auto":
        return override, "environment_override"
    return "native", "native_first_remote" if _windows_remote_drive(
        Path(path)
    ) else "native_first"


@dataclass
class WatchStatus:
    requested_mode: str = "auto"
    backend: str | None = None
    state: str = "starting"
    capability: str = "unknown"
    native_implementation: str | None = None
    reason: str | None = None
    delta_interval_seconds: float = DELTA_INTERVAL_SECONDS
    last_event_at: str | None = None
    last_delta_started_at: str | None = None
    last_delta_finished_at: str | None = None
    last_reconcile_queued_at: str | None = None
    pending_root_count: int = 0
    restart_count: int = 0
    last_error_code: str | None = None
    catchup_state: str = "pending"
    last_delta_changed_roots: int = 0

    def payload(self) -> dict:
        return {
            "requestedMode": self.requested_mode,
            "backend": self.backend,
            "state": self.state,
            "capability": self.capability,
            "nativeImplementation": self.native_implementation,
            "reason": self.reason,
            "pollIntervalSeconds": int(self.delta_interval_seconds),
            "deltaIntervalSeconds": int(self.delta_interval_seconds),
            "lastEventAt": self.last_event_at,
            "lastDeltaStartedAt": self.last_delta_started_at,
            "lastDeltaFinishedAt": self.last_delta_finished_at,
            "lastPollStartedAt": self.last_delta_started_at,
            "lastPollFinishedAt": self.last_delta_finished_at,
            "lastReconcileQueuedAt": self.last_reconcile_queued_at,
            "pendingRootCount": self.pending_root_count,
            "restartCount": self.restart_count,
            "lastErrorCode": self.last_error_code,
            "catchupState": self.catchup_state,
            "lastDeltaChangedRoots": self.last_delta_changed_roots,
        }


@dataclass
class _Registration:
    library_id: str
    root: Path
    status: WatchStatus
    observer: object | None = None
    next_delta: float = 0.0
    failures: int = 0
    future: Future | None = None


class _NativeHandler(FileSystemEventHandler):
    def __init__(self, manager: LibraryWatcherManager, library_id: str, root: Path):
        self.manager, self.library_id, self.root = manager, library_id, root

    def _emit(self, *paths: str | None) -> None:
        self.manager.emit(self.library_id, self.root, paths, full_scan=False)

    def on_created(self, event):
        self._emit(getattr(event, "src_path", None))

    def on_deleted(self, event):
        self._emit(getattr(event, "src_path", None))

    def on_modified(self, event):
        if not getattr(event, "is_directory", False):
            self._emit(getattr(event, "src_path", None))

    def on_moved(self, event):
        self._emit(getattr(event, "src_path", None), getattr(event, "dest_path", None))

    def on_closed(self, event):
        if not getattr(event, "is_directory", False):
            self._emit(getattr(event, "src_path", None))

    def on_closed_no_write(self, _event):
        return

    def on_opened(self, _event):
        return

    def on_accessed(self, _event):
        return


class LibraryWatcherManager:
    """Native Watchdog observers plus shallow runtime-provided delta probes."""

    def __init__(self, callback: Callable, delta_probe: Callable | None = None):
        self.callback = callback
        self.delta_probe = delta_probe
        self.delta_interval = _delta_interval()
        self._lock = threading.RLock()
        self._registrations: dict[str, _Registration] = {}
        self._statuses: dict[str, WatchStatus] = {}
        self._executor: ThreadPoolExecutor | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_health_check = 0.0
        self._event_source = threading.local()

    def status(self, library: dict) -> dict:
        if library.get("type") == "collection" or not library.get("directory"):
            return {"state": "not_applicable", "backend": None, "capability": "n/a"}
        with self._lock:
            status = self._statuses.get(library["id"])
            if status:
                return status.payload()
        return WatchStatus(
            requested_mode=library.get("watchMode", "auto"),
            state="disabled" if not library.get("watchEnabled") else "starting",
            capability="disabled" if not library.get("watchEnabled") else "unknown",
            delta_interval_seconds=self.delta_interval,
            reason="not_registered",
        ).payload()

    def configure(self, libraries: list[dict]) -> None:
        desired = {
            x["id"]: x
            for x in libraries
            if x.get("type") != "collection" and x.get("directory")
        }
        with self._lock:
            existing_ids = set(self._registrations)
        for library_id in existing_ids - set(desired):
            self._unregister(library_id)
        for library_id, library in desired.items():
            if not library.get("watchEnabled"):
                self._unregister(library_id)
                with self._lock:
                    self._statuses[library_id] = WatchStatus(
                        requested_mode=library.get("watchMode", "auto"),
                        state="disabled",
                        capability="disabled",
                        reason="watch_disabled",
                        delta_interval_seconds=self.delta_interval,
                    )
                continue
            root = Path(library["directory"])
            with self._lock:
                current = self._registrations.get(library_id)
                same = (
                    current
                    and current.root == root
                    and current.status.requested_mode
                    == library.get("watchMode", "auto")
                )
            if not same:
                self._unregister(library_id)
                self._register(library)
        with self._lock:
            if self._registrations and self._thread is None:
                self._stop.clear()
                self._executor = ThreadPoolExecutor(
                    max_workers=MAX_DELTA_WORKERS,
                    thread_name_prefix="zenstream-library-delta",
                )
                self._thread = threading.Thread(
                    target=self._loop, name="zenstream-library-watcher", daemon=True
                )
                self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            registrations = list(self._registrations.values())
            self._registrations.clear()
        for registration in registrations:
            self._stop_observer(registration)
        if self._thread:
            self._thread.join(timeout=5)
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._thread = None
        self._executor = None

    def health_check(self) -> None:
        now = time.monotonic()
        if now - self._last_health_check < HEALTH_CHECK_SECONDS:
            return
        self._last_health_check = now
        with self._lock:
            dead = [
                r
                for r in self._registrations.values()
                if r.observer is not None and not r.observer.is_alive()
            ]
        for registration in dead:
            self._stop_observer(registration)
            with self._lock:
                status = registration.status
                status.restart_count += 1
                status.backend = "delta"
                status.state = "degraded"
                status.capability = "degraded"
                status.reason = "native_observer_stopped"
                status.last_error_code = "native_observer_stopped"
                registration.next_delta = (
                    time.monotonic()
                    + RESTART_BACKOFF_SECONDS[min(status.restart_count - 1, 3)]
                )
            logger.warning(
                "library_watcher_degraded library_id=%s reason=native_observer_stopped",
                registration.library_id,
            )

    def emit(
        self,
        library_id: str,
        root: Path,
        paths: tuple[str | None, ...],
        *,
        full_scan: bool = False,
    ) -> None:
        with self._lock:
            status = self._statuses.get(library_id)
            if status:
                status.last_event_at = _now_iso()
                status.capability = "verified" if not full_scan else status.capability
                status.state = "active"
                status.reason = "native_event" if not full_scan else status.reason
        self._event_source.native = True
        try:
            self.callback(library_id, root, paths, full_scan)
        finally:
            self._event_source.native = False

    def emit_delta(
        self, library_id: str, root: Path, paths: tuple[str | None, ...]
    ) -> None:
        self._event_source.native = False
        self.callback(library_id, root, paths, False)

    def last_event_was_native(self) -> bool:
        return bool(getattr(self._event_source, "native", False))

    def mark_reconcile_queued(self, library_id: str, pending_root_count: int) -> None:
        with self._lock:
            status = self._statuses.get(library_id)
            if status:
                status.last_reconcile_queued_at = _now_iso()
                status.pending_root_count = pending_root_count

    def trigger_delta(self, library_id: str) -> None:
        with self._lock:
            registration = self._registrations.get(library_id)
            if registration:
                registration.next_delta = time.monotonic()

    def _register(self, library: dict) -> None:
        library_id, root = library["id"], Path(library["directory"])
        requested = library.get("watchMode", "auto")
        backend, reason = choose_backend(root, requested)
        status = WatchStatus(
            requested_mode=requested,
            backend=backend,
            reason=reason,
            delta_interval_seconds=self.delta_interval,
        )
        registration = _Registration(library_id, root, status)
        with self._lock:
            self._registrations[library_id] = registration
            self._statuses[library_id] = status
        if not root.is_dir():
            status.state, status.capability, status.reason = (
                "failed",
                "unavailable",
                "directory_unavailable",
            )
            status.last_error_code = "directory_unavailable"
        elif backend == "native" and Observer is not None:
            try:
                observer = Observer()
                observer.schedule(
                    _NativeHandler(self, library_id, root),
                    os.fspath(root),
                    recursive=True,
                )
                observer.start()
                registration.observer = observer
                status.backend, status.state, status.capability = (
                    "native",
                    "active",
                    "listening",
                )
                status.native_implementation = (
                    f"{observer.__class__.__module__}.{observer.__class__.__name__}"
                )
                logger.info(
                    "library_watcher_configured library_id=%s backend=native implementation=%s",
                    library_id,
                    status.native_implementation,
                )
            except Exception:
                logger.exception(
                    "library_watcher_native_failed library_id=%s", library_id
                )
                status.restart_count += 1
                status.backend, status.state, status.capability = (
                    "delta",
                    "degraded",
                    "degraded",
                )
                status.reason, status.last_error_code = (
                    "native_start_failed",
                    "native_start_failed",
                )
        elif backend == "native":
            status.backend, status.state, status.capability = (
                "delta",
                "degraded",
                "unavailable",
            )
            status.reason, status.last_error_code = (
                "watchdog_unavailable",
                "watchdog_unavailable",
            )
        else:
            status.backend, status.state, status.capability = (
                "delta",
                "active",
                "delta_only",
            )
            status.reason = "explicit_delta_mode"
        registration.next_delta = time.monotonic()

    def _unregister(self, library_id: str) -> None:
        with self._lock:
            registration = self._registrations.pop(library_id, None)
        if registration:
            self._stop_observer(registration)

    @staticmethod
    def _stop_observer(registration: _Registration) -> None:
        observer = registration.observer
        registration.observer = None
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=5)
            except Exception:
                logger.exception(
                    "library_watcher_native_stop_failed library_id=%s",
                    registration.library_id,
                )

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.health_check()
            now = time.monotonic()
            completed: list[tuple[_Registration, Future]] = []
            with self._lock:
                registrations = list(self._registrations.values())
                for registration in registrations:
                    if (
                        registration.observer is None
                        and registration.status.requested_mode != "polling"
                        and registration.status.last_error_code
                        in {"native_observer_stopped", "native_start_failed"}
                        and registration.next_delta <= now
                    ):
                        self._restart_native(registration)
                    if registration.future and registration.future.done():
                        completed.append((registration, registration.future))
                        registration.future = None
                active = sum(1 for registration in registrations if registration.future)
                for registration in registrations:
                    if (
                        registration.future is not None
                        or registration.next_delta > now
                        or not self._executor
                        or active >= MAX_DELTA_WORKERS
                    ):
                        continue
                    registration.status.last_delta_started_at = _now_iso()
                    registration.status.catchup_state = "running"
                    registration.future = self._executor.submit(
                        self._run_delta, registration.library_id, registration.root
                    )
                    active += 1
            for registration, future in completed:
                self._finish_delta(registration, future)
            self._stop.wait(0.25)

    def _restart_native(self, registration: _Registration) -> None:
        try:
            if Observer is None or not registration.root.is_dir():
                return
            observer = Observer()
            observer.schedule(
                _NativeHandler(self, registration.library_id, registration.root),
                os.fspath(registration.root),
                recursive=True,
            )
            observer.start()
            registration.observer = observer
            registration.status.backend = "native"
            registration.status.state = "active"
            registration.status.capability = "listening"
            registration.status.reason = "native_observer_restarted"
            registration.status.last_error_code = None
            registration.status.native_implementation = (
                f"{observer.__class__.__module__}.{observer.__class__.__name__}"
            )
            registration.next_delta = time.monotonic() + self.delta_interval
            logger.info(
                "library_watcher_restarted library_id=%s", registration.library_id
            )
        except Exception:
            registration.status.restart_count += 1
            registration.status.last_error_code = "native_start_failed"
            registration.next_delta = (
                time.monotonic()
                + RESTART_BACKOFF_SECONDS[min(registration.status.restart_count - 1, 3)]
            )
            logger.exception(
                "library_watcher_restart_failed library_id=%s", registration.library_id
            )

    def _run_delta(self, library_id: str, root: Path) -> tuple[str, ...]:
        return (
            tuple(self.delta_probe(library_id, root, "startup_or_safety_delta"))
            if self.delta_probe
            else ()
        )

    def _finish_delta(self, registration: _Registration, future: Future) -> None:
        try:
            paths = tuple(future.result())
            registration.failures = 0
            registration.next_delta = time.monotonic() + self.delta_interval
            status = registration.status
            status.last_delta_finished_at = _now_iso()
            status.catchup_state = "complete"
            status.last_delta_changed_roots = len(paths)
            if registration.observer is None:
                status.backend = "delta"
                status.state = "degraded" if status.last_error_code else "active"
                status.capability = (
                    "degraded" if status.last_error_code else "delta_only"
                )
            if paths:
                self.emit_delta(registration.library_id, registration.root, paths)
        except Exception:
            registration.failures += 1
            registration.next_delta = time.monotonic() + min(
                300, RESTART_BACKOFF_SECONDS[min(registration.failures - 1, 3)]
            )
            registration.status.state = "degraded"
            registration.status.capability = "degraded"
            registration.status.catchup_state = "failed"
            registration.status.last_error_code = "delta_probe_failed"
            logger.exception(
                "library_watcher_delta_failed library_id=%s", registration.library_id
            )
