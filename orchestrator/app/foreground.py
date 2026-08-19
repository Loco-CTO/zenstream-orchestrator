from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import TypeVar

T = TypeVar("T")


def _worker_count(name: str, default: int, minimum: int = 1, maximum: int = 64) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


_workers = _worker_count("FOREGROUND_WORKERS", 16, minimum=1, maximum=128)
_control_workers = _worker_count("CONTROL_WORKERS", 8, minimum=1, maximum=64)
_auth_workers = _worker_count("AUTH_WORKERS", 4, minimum=1, maximum=16)
_control_queue = _worker_count(
    "CONTROL_QUEUE", _control_workers * 4, minimum=0, maximum=256
)
_auth_queue = _worker_count("AUTH_QUEUE", _auth_workers * 4, minimum=0, maximum=128)
_executor = ThreadPoolExecutor(max_workers=_workers, thread_name_prefix="foreground")
_control_executor = ThreadPoolExecutor(
    max_workers=_control_workers, thread_name_prefix="control"
)
_auth_executor = ThreadPoolExecutor(
    max_workers=_auth_workers, thread_name_prefix="auth"
)
_lane_slots = {
    "control": asyncio.Semaphore(_control_workers + _control_queue),
    "auth": asyncio.Semaphore(_auth_workers + _auth_queue),
}
_active = {"foreground": 0, "control": 0, "auth": 0}
_completed = {"foreground": 0, "control": 0, "auth": 0}
_queued_seconds = {"foreground": 0.0, "control": 0.0, "auth": 0.0}
_execution_seconds = {"foreground": 0.0, "control": 0.0, "auth": 0.0}
_queue_depth = {"foreground": 0, "control": 0, "auth": 0}
_lock = threading.Lock()
_pending_futures: set[ConcurrentFuture] = set()
_settlement_tasks: set[asyncio.Task] = set()


def active_requests() -> int:
    with _lock:
        return _active["foreground"]


def active_control_work() -> int:
    with _lock:
        return _active["control"]


def active_auth_work() -> int:
    with _lock:
        return _active["auth"]


def metrics() -> dict[str, dict[str, float | int]]:
    """Return executor timing counters for diagnostics and health telemetry."""
    with _lock:
        return {
            lane: {
                "active": _active[lane],
                "completed": _completed[lane],
                "queued_seconds": _queued_seconds[lane],
                "execution_seconds": _execution_seconds[lane],
                "queue_depth": _queue_depth[lane],
            }
            for lane in _active
        }


async def _run(
    lane: str,
    executor: ThreadPoolExecutor,
    function: Callable[..., T],
    /,
    *args,
    **kwargs,
) -> T:
    queued_at = time.perf_counter()
    loop = asyncio.get_running_loop()
    started_at = 0.0
    completed_at = 0.0
    metrics_recorded = False
    slot = _lane_slots.get(lane)
    waiting = True
    slot_acquired = False
    with _lock:
        _queue_depth[lane] += 1

    def run() -> T:
        nonlocal started_at, completed_at
        started_at = time.perf_counter()
        with _lock:
            _active[lane] += 1
        try:
            return function(*args, **kwargs)
        finally:
            completed_at = time.perf_counter()
            with _lock:
                _active[lane] -= 1

    def record_timing() -> None:
        nonlocal metrics_recorded
        if not started_at or metrics_recorded:
            return
        metrics_recorded = True
        finished_at = completed_at or time.perf_counter()
        queue_seconds = max(0.0, started_at - queued_at)
        execution_seconds = max(0.0, finished_at - started_at)
        with _lock:
            _completed[lane] += 1
            _queued_seconds[lane] += queue_seconds
            _execution_seconds[lane] += execution_seconds
        if queue_seconds >= 0.1 or execution_seconds >= 2.0:
            from app.logging_config import get_logger

            get_logger("foreground").warning(
                "blocking work complete lane=%s queue_duration_ms=%.1f execution_duration_ms=%.1f",
                lane,
                queue_seconds * 1000,
                execution_seconds * 1000,
            )

    defer_timing = False
    try:
        if slot is not None:
            await slot.acquire()
            slot_acquired = True
        waiting = False
        with _lock:
            _queue_depth[lane] -= 1
        # Submit explicitly so shutdown can observe work that outlives an
        # awaiting request. ``run_in_executor`` hides the concurrent future
        # behind an asyncio wrapper.
        future = executor.submit(partial(run))
        with _lock:
            _pending_futures.add(future)

        def forget(done: ConcurrentFuture) -> None:
            with _lock:
                _pending_futures.discard(done)

        future.add_done_callback(forget)
        awaited = asyncio.wrap_future(future, loop=loop)
        try:
            result = await asyncio.shield(awaited)
        except asyncio.CancelledError:
            defer_timing = True
            release_slot = slot if slot_acquired else None
            slot_acquired = False

            async def settle_cancelled():
                try:
                    await awaited
                except BaseException:
                    pass
                finally:
                    if release_slot is not None:
                        release_slot.release()
                    record_timing()

            settlement = asyncio.create_task(settle_cancelled())
            with _lock:
                _settlement_tasks.add(settlement)

            def forget_settlement(done: asyncio.Task) -> None:
                with _lock:
                    _settlement_tasks.discard(done)

            settlement.add_done_callback(forget_settlement)
            raise
        finally:
            if slot_acquired and slot is not None:
                slot.release()
                slot_acquired = False
    finally:
        if waiting:
            with _lock:
                _queue_depth[lane] -= 1
        if slot_acquired and slot is not None:
            slot.release()
            slot_acquired = False
        if not defer_timing:
            record_timing()
    return result


async def run_foreground(function: Callable[..., T], /, *args, **kwargs) -> T:
    return await _run("foreground", _executor, function, *args, **kwargs)


async def run_control(function: Callable[..., T], /, *args, **kwargs) -> T:
    """Run bounded control-plane work away from the ASGI event loop."""
    return await _run("control", _control_executor, function, *args, **kwargs)


async def run_auth(function: Callable[..., T], /, *args, **kwargs) -> T:
    """Run password, session, ticket, and identity workflows off-loop."""
    return await _run("auth", _auth_executor, function, *args, **kwargs)


async def wait_for_shutdown(timeout: float = 5.0) -> None:
    """Give cancelled bridge calls a bounded chance to settle."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        with _lock:
            pending_futures = len(_pending_futures)
            settlement_tasks = len(_settlement_tasks)
        if not pending_futures and not settlement_tasks:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            from app.logging_config import get_logger

            get_logger("foreground").warning(
                "foreground shutdown timed out pending_futures=%s settlement_tasks=%s",
                pending_futures,
                settlement_tasks,
            )
            return
        await asyncio.sleep(min(0.05, remaining))


def shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)
    _control_executor.shutdown(wait=False, cancel_futures=True)
    _auth_executor.shutdown(wait=False, cancel_futures=True)
