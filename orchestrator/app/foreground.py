from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import TypeVar

T = TypeVar("T")


_workers = max(2, min(32, int(os.getenv("FOREGROUND_WORKERS", "16"))))
_executor = ThreadPoolExecutor(max_workers=_workers, thread_name_prefix="foreground")
_active = 0
_lock = threading.Lock()


def active_requests() -> int:
    with _lock:
        return _active


async def run_foreground(function: Callable[..., T], /, *args, **kwargs) -> T:
    global _active
    queued_at = time.perf_counter()
    loop = asyncio.get_running_loop()
    started_at = 0.0

    def run() -> T:
        nonlocal started_at
        global _active
        started_at = time.perf_counter()
        with _lock:
            _active += 1
        try:
            return function(*args, **kwargs)
        finally:
            with _lock:
                _active -= 1

    result = await loop.run_in_executor(_executor, partial(run))
    finished_at = time.perf_counter()
    queue_ms = (started_at - queued_at) * 1000
    execution_ms = (finished_at - started_at) * 1000
    if queue_ms >= 100 or execution_ms >= 2000:
        from app.logging_config import get_logger

        get_logger("foreground").warning(
            "foreground work complete queue_duration_ms=%.1f execution_duration_ms=%.1f",
            queue_ms,
            execution_ms,
        )
    return result


def shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)
