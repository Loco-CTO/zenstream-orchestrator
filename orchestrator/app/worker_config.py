from __future__ import annotations

import os


def configured_worker_limit(name: str, maximum: int, default: int = 0) -> int | None:
    raw = os.getenv(name, str(default))
    try:
        configured = int(raw)
    except (TypeError, ValueError):
        configured = default
    if configured == 0:
        return None
    return max(1, min(maximum, configured))


def worker_pool_size(limit: int | None, item_count: int) -> int:
    return max(1, item_count) if limit is None else limit
