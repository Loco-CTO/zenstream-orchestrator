"""Platform filesystem observers used by the library runtime."""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import TYPE_CHECKING

try:
    from watchdog.observers import Observer as _Observer
    from watchdog.observers.polling import PollingObserver as _PollingObserver
except ImportError:  # pragma: no cover - optional in minimal installations
    _Observer = None
    _PollingObserver = None


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver


_WindowsApiObserver = None

if sys.platform == "win32":
    try:
        from ctypes.wintypes import DWORD, HANDLE

        from watchdog.observers import winapi
        from watchdog.observers.api import (
            DEFAULT_OBSERVER_TIMEOUT,
            BaseObserver,
        )
        from watchdog.observers.read_directory_changes import WindowsApiEmitter

        _LIBRARY_NOTIFY_FLAGS = (
            winapi.FILE_NOTIFY_CHANGE_FILE_NAME
            | winapi.FILE_NOTIFY_CHANGE_DIR_NAME
            | winapi.FILE_NOTIFY_CHANGE_ATTRIBUTES
            | winapi.FILE_NOTIFY_CHANGE_SIZE
            | winapi.FILE_NOTIFY_CHANGE_LAST_WRITE
            | winapi.FILE_NOTIFY_CHANGE_SECURITY
            | winapi.FILE_NOTIFY_CHANGE_CREATION
        )

        def _parse_event_buffer(
            read_buffer: bytes, n_bytes: int
        ) -> list[tuple[int, str]]:
            results: list[tuple[int, str]] = []
            while n_bytes > 0:
                file_notify = ctypes.cast(
                    read_buffer,
                    ctypes.POINTER(winapi.FileNotifyInformation),
                )[0]
                pointer = (
                    ctypes.addressof(file_notify)
                    + winapi.FileNotifyInformation.FileName.offset
                )
                filename = ctypes.string_at(pointer, file_notify.FileNameLength)
                results.append((file_notify.Action, filename.decode("utf-16")))
                offset = file_notify.NextEntryOffset
                if offset <= 0:
                    break
                read_buffer = read_buffer[offset:]
                n_bytes -= offset
            return results

        def _read_directory_changes(
            handle: HANDLE,
            path: str,
            *,
            recursive: bool,
        ) -> tuple[bytes, int]:
            event_buffer = ctypes.create_string_buffer(winapi.BUFFER_SIZE)
            n_bytes = DWORD()
            try:
                winapi.ReadDirectoryChangesW(
                    handle,
                    ctypes.byref(event_buffer),
                    len(event_buffer),
                    recursive,
                    _LIBRARY_NOTIFY_FLAGS,
                    ctypes.byref(n_bytes),
                    None,
                    None,
                )
            except OSError as error:
                if getattr(error, "winerror", None) == winapi.ERROR_OPERATION_ABORTED:
                    return event_buffer.raw, 0
                if winapi._is_observed_path_deleted(handle, path):
                    return winapi._generate_observed_path_deleted_event()
                raise
            return event_buffer.raw, int(n_bytes.value)

        def _read_events_without_last_access(
            handle: HANDLE,
            path: str,
            *,
            recursive: bool,
        ) -> list:
            buffer, n_bytes = _read_directory_changes(
                handle,
                path,
                recursive=recursive,
            )
            return [
                winapi.WinAPINativeEvent(action, source_path)
                for action, source_path in _parse_event_buffer(buffer, n_bytes)
            ]

        class _LibraryWindowsApiEmitter(WindowsApiEmitter):
            def _read_events(self):
                if not self._whandle:
                    return []
                return _read_events_without_last_access(
                    self._whandle,
                    self.watch.path,
                    recursive=self.watch.is_recursive,
                )

        class _LibraryWindowsApiObserver(BaseObserver):
            def __init__(self, *, timeout: float = DEFAULT_OBSERVER_TIMEOUT) -> None:
                super().__init__(_LibraryWindowsApiEmitter, timeout=timeout)

        _WindowsApiObserver = _LibraryWindowsApiObserver
    except (
        AttributeError,
        ImportError,
        OSError,
    ):  # pragma: no cover - platform fallback
        logger.warning(
            "custom Windows library watcher unavailable; using polling observer",
            exc_info=True,
        )


def create_library_observer() -> BaseObserver | None:
    """Create an observer that cannot react to file access-time updates."""
    if _WindowsApiObserver is not None:
        return _WindowsApiObserver()
    if sys.platform == "win32" and _PollingObserver is not None:
        return _PollingObserver()
    if _Observer is not None:
        return _Observer()
    return None
