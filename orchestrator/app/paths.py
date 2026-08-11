from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def metadata_directory() -> Path:
    """Return the persistent root for SQLite, caches, and logs."""
    configured = os.getenv("METADATA_PATH")
    if not configured:
        return PROJECT_ROOT / "sqlite"
    directory = Path(configured).expanduser()
    return directory if directory.is_absolute() else PROJECT_ROOT / directory
