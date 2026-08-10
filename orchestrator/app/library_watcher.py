from __future__ import annotations

import ctypes
import os
import platform
import random
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
    from watchdog.utils.dirsnapshot import DirectorySnapshot, DirectorySnapshotDiff
except ImportError:  # pragma: no cover - exercised by minimal installations
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment,misc]
    DirectorySnapshot = None  # type: ignore[assignment,misc]
    DirectorySnapshotDiff = None  # type: ignore[assignment,misc]

from app.logging_config import get_logger

logger = get_logger("library_watcher")

POLL_INTERVAL_SECONDS = 60.0
POLL_MAX_SECONDS = 3600.0
POLL_MIN_SECONDS = 5.0
MAX_POLL_WORKERS = 2
HEALTH_CHECK_SECONDS = 15.0
RESTART_BACKOFF_SECONDS = (1.0, 5.0, 30.0, 300.0)
REMOTE_FILESYSTEMS = {
    "9p",
    "afp",
    "cifs",
    "fuse.curlftpfs",
    "fuse.rclone",
    "fuse.sshfs",
    "fuseblk",
    "fuse.smb",
    "nfs",
    "nfs4",
    "smb3",
    "virtiofs",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _configured_backend() -> str:
    value = os.getenv("LIBRARY_WATCH_BACKEND", "auto").strip().lower()
    return value if value in {"auto", "native", "polling"} else "auto"


def _poll_interval() -> float:
    try:
        value = float(
            os.getenv("LIBRARY_WATCH_POLL_SECONDS", str(POLL_INTERVAL_SECONDS))
        )
    except (TypeError, ValueError):
        value = POLL_INTERVAL_SECONDS
    return max(POLL_MIN_SECONDS, min(POLL_MAX_SECONDS, value))


def _in_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text(errors="ignore").lower()
    except OSError:
        return False


def _decode_mount_component(value: str) -> str:
    return value.replace(r"\040", " ").replace(r"\011", "\t").replace(r"\134", "\\")


def _linux_mount_type(path: Path) -> str | None:
    if platform.system().lower() != "linux":
        return None
    try:
        rows = Path("/proc/self/mountinfo").read_text(errors="ignore").splitlines()
    except OSError:
        return None
    candidate = os.path.abspath(os.fspath(path))
    best: tuple[int, str] | None = None
    for row in rows:
        parts = row.split(" - ", 1)
        if len(parts) != 2:
            continue
        left, right = parts
        fields = left.split()
        if len(fields) < 5:
            continue
        mountpoint = os.path.abspath(_decode_mount_component(fields[4]))
        if candidate != mountpoint and not candidate.startswith(
            mountpoint.rstrip(os.sep) + os.sep
        ):
            continue
        fstype = right.split()[0].lower() if right.split() else ""
        if best is None or len(mountpoint) > best[0]:
            best = (len(mountpoint), fstype)
    return best[1] if best else None


def _windows_remote_drive(path: Path) -> bool:
    value = os.fspath(path)
    if value.startswith(("\\\\", "//")):
        return True
    if os.name != "nt" or len(value) < 2 or value[1] != ":":
        return False
    try:
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(value[:3])
    except (AttributeError, OSError):
        return False
    return drive_type == 4  # DRIVE_REMOTE


def choose_backend(path: str | Path, requested: str = "auto") -> tuple[str, str]:
    requested = requested if requested in {"auto", "native", "polling"} else "auto"
    if requested != "auto":
        return requested, "explicit"
    override = _configured_backend()
    if override != "auto":
        return override, "environment_override"
    root = Path(path)
    if _windows_remote_drive(root):
        return "polling", "remote_windows_drive"
    mount_type = _linux_mount_type(root)
    if mount_type in REMOTE_FILESYSTEMS:
        return "polling", f"filesystem_{mount_type}"
    if _in_container() and mount_type not in {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "zfs",
        "tmpfs",
    }:
        return "polling", "unknown_container_mount"
    return "native", "local_filesystem"


@dataclass
class WatchStatus:
    requested_mode: str = "auto"
    backend: str | None = None
    state: str = "starting"
    reason: str | None = None
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS
    last_event_at: str | None = None
    last_poll_started_at: str | None = None
    last_poll_finished_at: str | None = None
    last_reconcile_queued_at: str | None = None
    pending_root_count: int = 0
    restart_count: int = 0
    last_error_code: str | None = None

    def payload(self) -> dict:
        return {
            "requestedMode": self.requested_mode,
            "backend": self.backend,
            "state": self.state,
            "reason": self.reason,
            "pollIntervalSeconds": int(self.poll_interval_seconds),
            "lastEventAt": self.last_event_at,
            "lastPollStartedAt": self.last_poll_started_at,
            "lastPollFinishedAt": self.last_poll_finished_at,
            "lastReconcileQueuedAt": self.last_reconcile_queued_at,
            "pendingRootCount": self.pending_root_count,
            "restartCount": self.restart_count,
            "lastErrorCode": self.last_error_code,
        }


@dataclass
class _PollRegistration:
    library_id: str
    root: Path
    status: WatchStatus
    snapshot: object | None = None
    next_due: float = 0.0
    failures: int = 0
    future: Future | None = None
    poll_started: float | None = None


class _NativeHandler(FileSystemEventHandler):
    def __init__(self, manager: LibraryWatcherManager, library_id: str, root: Path):
        self.manager = manager
        self.library_id = library_id
        self.root = root

    def _emit(self, *paths: str | None, full_scan: bool = False) -> None:
        self.manager.emit(self.library_id, self.root, paths, full_scan=full_scan)

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


class LibraryWatcherManager:
    """Cross-platform native/polling watcher coordinator.

    The callback is invoked as ``callback(library_id, root, paths, full_scan)``.
    It must be quick and must not perform database or filesystem traversal work.
    """

    def __init__(
        self, callback: Callable[[str, Path, tuple[str | None, ...], bool], None]
    ):
        self.callback = callback
        self.poll_interval = _poll_interval()
        self._lock = threading.RLock()
        self._native: dict[str, tuple[object, WatchStatus, Path]] = {}
        self._polls: dict[str, _PollRegistration] = {}
        self._statuses: dict[str, WatchStatus] = {}
        self._poll_executor: ThreadPoolExecutor | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_health_check = 0.0

    def status(self, library: dict) -> dict:
        if library.get("type") == "collection" or not library.get("directory"):
            return {"state": "not_applicable", "backend": None}
        with self._lock:
            value = self._statuses.get(library["id"])
            return (
                value.payload()
                if value
                else {
                    "requestedMode": library.get("watchMode", "auto"),
                    "backend": None,
                    "state": "disabled"
                    if not library.get("watchEnabled")
                    else "starting",
                    "reason": "not_registered",
                    "pollIntervalSeconds": int(self.poll_interval),
                    "lastEventAt": None,
                    "lastPollStartedAt": None,
                    "lastPollFinishedAt": None,
                    "lastReconcileQueuedAt": None,
                    "pendingRootCount": 0,
                    "restartCount": 0,
                    "lastErrorCode": None,
                }
            )

    def configure(self, libraries: list[dict]) -> None:
        self.stop()
        self._stop.clear()
        with self._lock:
            self._statuses.clear()
        for library in libraries:
            if not library.get("watchEnabled") or library.get("type") == "collection":
                continue
            directory = library.get("directory")
            if not directory:
                continue
            self._register(library)
        with self._lock:
            if self._polls:
                self._poll_executor = ThreadPoolExecutor(
                    max_workers=MAX_POLL_WORKERS,
                    thread_name_prefix="zenstream-library-poll",
                )
                self._poll_thread = threading.Thread(
                    target=self._poll_loop,
                    name="zenstream-library-poll-coordinator",
                    daemon=True,
                )
                self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()
        native = []
        with self._lock:
            native = list(self._native.values())
            self._native.clear()
            self._polls.clear()
        for observer, _status, _root in native:
            try:
                observer.stop()
                observer.join(timeout=5)
            except Exception:
                logger.exception("library_watcher_native_stop_failed")
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        if self._poll_executor:
            self._poll_executor.shutdown(wait=False, cancel_futures=True)
        self._poll_thread = None
        self._poll_executor = None

    def health_check(self) -> None:
        now = time.monotonic()
        if now - self._last_health_check < HEALTH_CHECK_SECONDS:
            return
        self._last_health_check = now
        with self._lock:
            dead = [
                (library_id, observer, status, root)
                for library_id, (observer, status, root) in self._native.items()
                if not observer.is_alive()
            ]
        for library_id, observer, status, root in dead:
            try:
                observer.stop()
                observer.join(timeout=2)
            except Exception:
                pass
            with self._lock:
                self._native.pop(library_id, None)
                status.restart_count += 1
                status.state = "degraded"
                status.backend = "polling"
                status.reason = "native_observer_stopped"
                status.last_error_code = "native_observer_stopped"
                registration = self._polls.get(library_id)
                if registration is None:
                    self._polls[library_id] = _PollRegistration(
                        library_id,
                        root,
                        status,
                        next_due=time.monotonic(),
                    )
                    if self._poll_executor is None:
                        self._poll_executor = ThreadPoolExecutor(
                            max_workers=MAX_POLL_WORKERS,
                            thread_name_prefix="zenstream-library-poll",
                        )
                    if self._poll_thread is None or not self._poll_thread.is_alive():
                        self._stop.clear()
                        self._poll_thread = threading.Thread(
                            target=self._poll_loop,
                            name="zenstream-library-poll-coordinator",
                            daemon=True,
                        )
                        self._poll_thread.start()
            logger.warning(
                "library_watcher_fallback library_id=%s reason=native_observer_stopped",
                library_id,
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
        self.callback(library_id, root, paths, full_scan)

    def mark_reconcile_queued(self, library_id: str, pending_root_count: int) -> None:
        with self._lock:
            status = self._statuses.get(library_id)
            if status:
                status.last_reconcile_queued_at = _now_iso()
                status.pending_root_count = pending_root_count

    def _register(self, library: dict) -> None:
        library_id = library["id"]
        root = Path(library["directory"])
        requested = library.get("watchMode", "auto")
        backend, reason = choose_backend(root, requested)
        status = WatchStatus(
            requested_mode=requested,
            backend=backend,
            state="starting",
            reason=reason,
            poll_interval_seconds=self.poll_interval,
        )
        with self._lock:
            self._statuses[library_id] = status
        if not root.is_dir():
            status.state = "failed"
            status.reason = "directory_unavailable"
            status.last_error_code = "directory_unavailable"
            return
        if backend == "native" and Observer is not None:
            try:
                observer = Observer()
                observer.schedule(
                    _NativeHandler(self, library_id, root),
                    os.fspath(root),
                    recursive=True,
                )
                observer.start()
                with self._lock:
                    self._native[library_id] = (observer, status, root)
                status.state = "active"
                logger.info(
                    "library_watcher_configured library_id=%s backend=native reason=%s",
                    library_id,
                    reason,
                )
                return
            except Exception:
                logger.exception(
                    "library_watcher_native_failed library_id=%s", library_id
                )
                status.restart_count += 1
                status.last_error_code = "native_start_failed"
                backend = "polling"
                status.backend = backend
                status.reason = "native_start_failed"
        if DirectorySnapshot is None:
            status.state = "failed"
            status.last_error_code = "watchdog_unavailable"
            status.reason = "watchdog_unavailable"
            return
        registration = _PollRegistration(
            library_id,
            root,
            status,
            next_due=time.monotonic() + random.uniform(0, 15),
        )
        with self._lock:
            self._polls[library_id] = registration
        status.backend = "polling"
        status.state = "active"
        logger.info(
            "library_watcher_configured library_id=%s backend=polling reason=%s",
            library_id,
            status.reason,
        )

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            completed: list[tuple[_PollRegistration, Future]] = []
            with self._lock:
                for registration in self._polls.values():
                    if registration.future and registration.future.done():
                        completed.append((registration, registration.future))
                        registration.future = None
                completed_ids = {
                    id(registration) for registration, _future in completed
                }
                active_count = sum(1 for value in self._polls.values() if value.future)
                for registration in self._polls.values():
                    if (
                        id(registration) not in completed_ids
                        and registration.future is None
                        and registration.next_due <= now
                        and self._poll_executor
                        and active_count < MAX_POLL_WORKERS
                    ):
                        registration.poll_started = time.monotonic()
                        registration.status.last_poll_started_at = _now_iso()
                        registration.future = self._poll_executor.submit(
                            self._take_snapshot, registration.root
                        )
                        active_count += 1
            for registration, future in completed:
                self._finish_poll(registration, future)
            self._stop.wait(0.25)

    @staticmethod
    def _take_snapshot(root: Path):
        return DirectorySnapshot(os.fspath(root), recursive=True)

    def _finish_poll(self, registration: _PollRegistration, future: Future) -> None:
        finished = time.monotonic()
        try:
            snapshot = future.result()
            old = registration.snapshot
            registration.snapshot = snapshot
            registration.failures = 0
            registration.next_due = finished + self.poll_interval
            registration.status.last_poll_finished_at = _now_iso()
            registration.status.state = "active"
            if old is None:
                self.emit(
                    registration.library_id, registration.root, (), full_scan=True
                )
                logger.info(
                    "library_watcher_poll_baseline library_id=%s",
                    registration.library_id,
                )
                return
            diff = DirectorySnapshotDiff(old, snapshot)
            paths: list[str | None] = []
            paths.extend(diff.files_created)
            paths.extend(diff.files_modified)
            paths.extend(diff.files_deleted)
            paths.extend(diff.dirs_created)
            paths.extend(diff.dirs_deleted)
            for source, destination in (*diff.files_moved, *diff.dirs_moved):
                paths.extend((source, destination))
            if paths:
                self.emit(registration.library_id, registration.root, tuple(paths))
                logger.info(
                    "library_watcher_poll_complete library_id=%s changes=%s duration_seconds=%.1f",
                    registration.library_id,
                    len(paths),
                    finished - (registration.poll_started or finished),
                )
        except Exception:
            registration.failures += 1
            registration.status.state = "degraded"
            registration.status.last_error_code = "poll_failed"
            registration.next_due = finished + min(
                300.0, RESTART_BACKOFF_SECONDS[min(registration.failures - 1, 3)]
            )
            logger.exception(
                "library_watcher_poll_failed library_id=%s", registration.library_id
            )
