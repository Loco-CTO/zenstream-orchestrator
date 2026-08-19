"""Cancellable, non-interactive supervision for analysis FFmpeg workers."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Sequence

DEFAULT_FFMPEG_TIMEOUT_SECONDS = 900.0
MAX_STDERR_BYTES = 1_048_576


class FFmpegError(RuntimeError):
    """Base class for an unsuccessful FFmpeg invocation."""


class FFmpegCancelled(FFmpegError):
    """The owning background job was cancelled."""


class FFmpegTimedOut(FFmpegError):
    """FFmpeg exceeded the bounded containment timeout."""


class FFmpegFailed(FFmpegError):
    """FFmpeg exited unsuccessfully."""


def _stop_process(process: subprocess.Popen, *, timeout: float = 10.0) -> None:
    """Terminate, wait, and finally kill a child within bounded cleanup."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # A process that ignores kill is still waited on by the reader cleanup;
        # do not block the worker indefinitely trying to reap it.
        pass


def run_ffmpeg(
    command: Sequence[str],
    *,
    should_terminate: Callable[[], bool] | None = None,
    timeout_seconds: float = DEFAULT_FFMPEG_TIMEOUT_SECONDS,
    progress: Callable[[dict[str, str]], None] | None = None,
) -> bytes:
    """Run FFmpeg with detached stdin and bounded, cancellable supervision.

    When ``progress`` is supplied FFmpeg is expected to emit machine-readable
    ``-progress pipe:1`` records on stdout.  Without it stdout is returned as
    binary data (used by Chromaprint fingerprinting).  Both stdout and stderr
    are drained on reader threads so a noisy child cannot deadlock on a pipe.
    """
    should_terminate = should_terminate or (lambda: False)
    timeout = max(1.0, float(timeout_seconds))
    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": False,
    }
    if progress is not None:
        popen_kwargs.update(text=True, encoding="utf-8", errors="replace", bufsize=1)
    process = subprocess.Popen(list(command), **popen_kwargs)
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    progress_records: list[dict[str, str]] = []
    reader_errors: list[BaseException] = []

    def read_stdout() -> None:
        try:
            if progress is not None:
                assert process.stdout is not None
                record: dict[str, str] = {}
                for line in process.stdout:
                    value = (
                        line.decode("utf-8", "replace")
                        if isinstance(line, bytes)
                        else str(line)
                    ).strip()
                    if "=" not in value:
                        continue
                    key, item = value.split("=", 1)
                    record[key.strip()] = item.strip()
                    if key.strip() == "progress":
                        progress_records.append(record)
                        record = {}
                if record:
                    progress_records.append(record)
                return
            assert process.stdout is not None
            stdout_chunks.append(process.stdout.read() or b"")
        except BaseException as error:  # pragma: no cover - defensive cleanup
            reader_errors.append(error)

    def read_stderr() -> None:
        try:
            assert process.stderr is not None
            tail = bytearray()
            while True:
                chunk = process.stderr.read(64 * 1024)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                tail.extend(chunk)
                if len(tail) > MAX_STDERR_BYTES:
                    del tail[:-MAX_STDERR_BYTES]
            # FFmpeg can emit repeated progress/diagnostic lines for a very
            # long job.  Keep a diagnostic tail rather than retaining all of
            # stderr until the process exits.
            stderr_chunks.append(bytes(tail))
        except BaseException as error:  # pragma: no cover - defensive cleanup
            reader_errors.append(error)

    stdout_thread = threading.Thread(
        target=read_stdout, name="ffmpeg-stdout", daemon=True
    )
    stderr_thread = threading.Thread(
        target=read_stderr, name="ffmpeg-stderr", daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    started = time.monotonic()
    try:
        while process.poll() is None:
            if should_terminate():
                _stop_process(process)
                raise FFmpegCancelled("Terminated by administrator")
            if time.monotonic() - started >= timeout:
                _stop_process(process)
                raise FFmpegTimedOut("FFmpeg analysis timed out.")
            if progress is not None:
                while progress_records:
                    progress(progress_records.pop(0))
            time.sleep(0.1)
        process.wait()
        if progress is not None:
            while progress_records:
                progress(progress_records.pop(0))
    finally:
        # A reader can only finish after the pipes close.  The child has been
        # reaped above (or stopped on the exceptional path), so these joins are
        # bounded cleanup rather than a second source of worker hangs.
        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)
        if process.poll() is None:
            _stop_process(process)
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if progress is not None:
            while progress_records:
                progress(progress_records.pop(0))
    if reader_errors:
        raise FFmpegError(str(reader_errors[0]))
    stderr = b"".join(stderr_chunks).decode("utf-8", "replace").strip()
    if process.returncode:
        raise FFmpegFailed((stderr or "FFmpeg analysis failed.")[-1000:])
    return b"".join(stdout_chunks)
