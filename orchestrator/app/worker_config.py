from __future__ import annotations

import os


def configured_worker_limit(name: str, maximum: int, default: int = 12) -> int:
    raw = os.getenv(name, str(default))
    try:
        configured = int(raw)
    except (TypeError, ValueError):
        configured = default
    return max(1, min(maximum, configured))
