from __future__ import annotations

import os
import sys
from pathlib import Path


def application_root() -> Path:
    """Return the source/container root or PyInstaller resource directory."""
    bundled = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundled:
        return Path(bundled).resolve()
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = application_root()


def metadata_directory() -> Path:
    """Return the persistent root for SQLite, caches, and logs."""
    configured = os.getenv("METADATA_PATH")
    if not configured:
        if getattr(sys, "frozen", False):
            local_app_data = os.getenv("LOCALAPPDATA")
            base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
            return base / "ZenStream Orchestrator" / "metadata"
        return PROJECT_ROOT / "sqlite"
    directory = Path(configured).expanduser()
    return directory if directory.is_absolute() else PROJECT_ROOT / directory
