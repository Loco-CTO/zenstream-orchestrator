from __future__ import annotations

import logging
import multiprocessing
import os
import sys
import threading
import time
from collections.abc import Iterator

import uvicorn
from dotenv import load_dotenv

logger = logging.getLogger("zenstream.launcher")


def _launcher_commands() -> Iterator[str]:
    """Read launcher commands without an outstanding synchronous Windows pipe read.

    An idle stdin read can block native DLL initialization that inspects the same
    standard handle, including NumPy's OpenBLAS dependency.
    """
    if sys.platform != "win32":
        yield from sys.stdin
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    handle = kernel32.GetStdHandle(-10)
    buffer = ctypes.create_string_buffer(4096)
    available = wintypes.DWORD()
    received = wintypes.DWORD()
    pending = b""
    while True:
        if not kernel32.PeekNamedPipe(
            handle, None, 0, None, ctypes.byref(available), None
        ):
            error = ctypes.get_last_error()
            if error == 109:
                break
            raise ctypes.WinError(error)
        if not available.value:
            time.sleep(0.05)
            continue
        if not kernel32.ReadFile(
            handle,
            buffer,
            min(len(buffer), available.value),
            ctypes.byref(received),
            None,
        ):
            error = ctypes.get_last_error()
            if error == 109:
                break
            raise ctypes.WinError(error)
        if not received.value:
            break
        pending += buffer.raw[: received.value]
        while b"\n" in pending:
            command, pending = pending.split(b"\n", 1)
            yield command.decode("utf-8", errors="replace")
    if pending:
        yield pending.decode("utf-8", errors="replace")


def _watch_launcher(server: uvicorn.Server) -> None:
    """Stop Uvicorn when the launcher requests shutdown or closes its pipe."""
    try:
        for command in _launcher_commands():
            if command.strip().lower() == "shutdown":
                logger.info("launcher shutdown requested source=control_pipe")
                return
        logger.info("launcher control pipe closed; stopping backend")
    except (OSError, ValueError):
        logger.warning("launcher control pipe failed; stopping backend", exc_info=True)
    finally:
        server.should_exit = True


def main() -> int:
    load_dotenv()
    config = uvicorn.Config(
        "app.app:app",
        host=os.getenv("ORCHESTRATOR_HOST", "127.0.0.1"),
        port=int(os.getenv("ORCHESTRATOR_PORT", "9088")),
        reload=False,
    )
    server = uvicorn.Server(config)
    watcher = threading.Thread(
        target=_watch_launcher,
        args=(server,),
        name="zenstream-launcher-control",
        daemon=True,
    )
    watcher.start()
    server.run()
    return 0 if server.started else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
