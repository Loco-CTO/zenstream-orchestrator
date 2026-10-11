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

from fastapi import HTTPException

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
_foreground_queue = min(512, _workers * 4)
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
    "foreground": threading.BoundedSemaphore(_workers + _foreground_queue),
    "control": threading.BoundedSemaphore(_control_workers + _control_queue),
    "auth": threading.BoundedSemaphore(_auth_workers + _auth_queue),
}
_active = {"foreground": 0, "control": 0, "auth": 0}
_completed = {"foreground": 0, "control": 0, "auth": 0}
_queued_seconds = {"foreground": 0.0, "control": 0.0, "auth": 0.0}
_execution_seconds = {"foreground": 0.0, "control": 0.0, "auth": 0.0}
_queue_depth = {"foreground": 0, "control": 0, "auth": 0}
_lock = threading.Lock()
_pending_futures: set[ConcurrentFuture] = set()
_settlement_tasks: set[asyncio.Task] = set()


class WorkerLaneOverloaded(HTTPException):
    """Raised when a worker lane has no active or pending admission capacity."""

    def __init__(self, lane: str):
        self.lane = lane
        super().__init__(
            status_code=503,
            detail="Orchestrator is busy. Retry shortly.",
            headers={"Retry-After": "1"},
        )


class _WorkerCall:
    def __init__(self, lane, function, args, kwargs, slot, queued_at):
        self.lane = lane
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.slot = slot
        self.queued_at = queued_at
        self.started_at = 0.0
        self.completed_at = 0.0
        self.metrics_recorded = False
        self.slot_acquired = False
        self.queue_registered = False
        self.defer_timing = False

    def acquire(self) -> None:
        if not self.slot.acquire(blocking=False):
            raise WorkerLaneOverloaded(self.lane)
        self.slot_acquired = True
        with _lock:
            _queue_depth[self.lane] += 1
            self.queue_registered = True

    def run(self) -> T:
        self.started_at = time.perf_counter()
        with _lock:
            if self.queue_registered:
                _queue_depth[self.lane] -= 1
                self.queue_registered = False
            _active[self.lane] += 1
        try:
            return self.function(*self.args, **self.kwargs)
        finally:
            self.completed_at = time.perf_counter()
            with _lock:
                _active[self.lane] -= 1

    def record_timing(self) -> None:
        if not self.started_at or self.metrics_recorded:
            return
        self.metrics_recorded = True
        finished_at = self.completed_at or time.perf_counter()
        queue_seconds = max(0.0, self.started_at - self.queued_at)
        execution_seconds = max(0.0, finished_at - self.started_at)
        with _lock:
            _completed[self.lane] += 1
            _queued_seconds[self.lane] += queue_seconds
            _execution_seconds[self.lane] += execution_seconds
        if queue_seconds >= 0.1 or execution_seconds >= 2.0:
            from app.logging_config import get_logger

            get_logger("foreground").warning(
                "blocking work complete lane=%s operation=%s "
                "queue_duration_ms=%.1f execution_duration_ms=%.1f",
                self.lane,
                getattr(self.function, "__qualname__", type(self.function).__name__),
                queue_seconds * 1000,
                execution_seconds * 1000,
            )

    def submit(self, executor: ThreadPoolExecutor) -> ConcurrentFuture:
        future = executor.submit(partial(self.run))
        with _lock:
            _pending_futures.add(future)
        future.add_done_callback(_forget_future)
        return future

    def defer_cancelled(self, awaited: asyncio.Future) -> None:
        self.defer_timing = True
        release_slot = self.slot if self.slot_acquired else None
        self.slot_acquired = False
        settlement = asyncio.create_task(
            _settle_cancelled_worker(awaited, release_slot, self)
        )
        with _lock:
            _settlement_tasks.add(settlement)
        settlement.add_done_callback(_forget_settlement)

    def release(self) -> None:
        if self.queue_registered:
            with _lock:
                if self.queue_registered:
                    _queue_depth[self.lane] -= 1
                    self.queue_registered = False
        if self.slot_acquired:
            self.slot.release()
            self.slot_acquired = False
        if not self.defer_timing:
            self.record_timing()


def _forget_future(done: ConcurrentFuture) -> None:
    with _lock:
        _pending_futures.discard(done)


def _forget_settlement(done: asyncio.Task) -> None:
    with _lock:
        _settlement_tasks.discard(done)


async def _settle_cancelled_worker(
    awaited: asyncio.Future, release_slot, work: _WorkerCall
) -> None:
    try:
        await awaited
    except BaseException:
        pass
    finally:
        if release_slot is not None:
            release_slot.release()
        work.record_timing()


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
    slot = _lane_slots[lane]
    work = _WorkerCall(lane, function, args, kwargs, slot, queued_at)
    work.acquire()
    try:
        # Submit explicitly so shutdown can observe work that outlives an
        # awaiting request. ``run_in_executor`` hides the concurrent future
        # behind an asyncio wrapper.
        future = work.submit(executor)
        awaited = asyncio.wrap_future(future, loop=loop)
        try:
            return await asyncio.shield(awaited)
        except asyncio.CancelledError:
            work.defer_cancelled(awaited)
            raise
    finally:
        work.release()


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
