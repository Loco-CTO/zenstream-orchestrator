from __future__ import annotations

import os
import time
from pathlib import Path

from app.config import Config
from app.logging_config import get_logger

logger = get_logger("retention")
DEFAULT_RETENTION_DAYS = 30


def configured_retention_days() -> int:
    try:
        value = int(os.getenv("RESOURCE_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
    except (TypeError, ValueError):
        value = DEFAULT_RETENTION_DAYS
    return max(1, min(3650, value))


def _prune_subtitle_cache(db, retention_days: int) -> int:
    db_file = getattr(db, "db_file", None)
    if not db_file or db_file == ":memory:":
        return 0
    root = Path(db_file).parent / "subtitle-cache"
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    cutoff = time.time() - max(1, retention_days) * 86400
    removed = 0
    for entry in entries:
        try:
            if not entry.is_file() or entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            logger.debug("could not prune subtitle cache path=%s", entry, exc_info=True)
    return removed


def _run(label: str, function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception:
        logger.warning("resource retention step failed step=%s", label, exc_info=True)
        return 0


def run_resource_retention(job_store=None) -> dict[str, int]:
    """Run one bounded retention pass.

    The pass is synchronous by design and is called through the control lane,
    so its SQLite/filesystem work never crosses an async route boundary.
    """
    from app.library import runtime as library_runtime
    from app.metadata_services import asset_executor
    from app.models.account import Account
    from app.models.admin import Admin
    from app.models.playback_viewer import PlaybackViewerStore
    from app.models.syncplay import SyncplayGroup
    from app.notifications import NotificationService
    from app.playback import PlaybackManager
    from app.providers import MetadataService

    days = configured_retention_days()
    db = Config().database
    result = {
        "user_sessions": _run("user_sessions", Account.cleanup_expired_sessions),
        "admin_sessions": _run("admin_sessions", Admin.cleanup_expired_sessions),
        "viewer_history": _run(
            "viewer_history", PlaybackViewerStore(db).cleanup_history, days
        ),
        "syncplay_expiry": _run(
            "syncplay_expiry", SyncplayGroup.expire_due_host_disconnects
        ),
        "syncplay_history": _run(
            "syncplay_history", SyncplayGroup.cleanup_history, days
        ),
        "subtitle_cache": _run("subtitle_cache", _prune_subtitle_cache, db, days),
        "notifications": _run("notifications", NotificationService(db).cleanup, days),
    }
    if job_store is not None:
        history = _run("job_history", job_store.cleanup_history, days)
        if isinstance(history, dict):
            result.update(history)

    # These are process-lifetime indexes, so they need no database round trip.
    _run("playback_indexes", PlaybackManager.prune_runtime_state)
    _run("library_runtime_state", library_runtime.prune_runtime_state)
    _run("metadata_asset_states", asset_executor.prune)
    _run("provider_fetch_locks", MetadataService.prune_fetch_locks)
    return result
