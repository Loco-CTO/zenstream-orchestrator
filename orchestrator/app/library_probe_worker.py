"""Small, SQLite-free filesystem probe worker used by the delta verifier."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _stat(path: Path) -> dict:
    try:
        info = path.stat()
    except FileNotFoundError:
        return {"kind": "missing"}
    except OSError as error:
        return {"kind": "error", "error": type(error).__name__}
    return {
        "kind": "ok",
        "size": info.st_size,
        "mtime_ns": getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
        "is_dir": path.is_dir(),
    }


def _directory(root: Path, record: dict) -> dict:
    relative = str(record.get("relative_path") or "")
    path = root / relative if relative else root
    result = _stat(path)
    result["relative_path"] = relative
    if result.get("kind") != "ok" or not result.get("is_dir"):
        return result
    old_mtime = record.get("mtime_ns")
    if record.get("complete") and old_mtime == result.get("mtime_ns"):
        return result
    entries: list[dict] = []
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                try:
                    info = entry.stat(follow_symlinks=False)
                    entries.append(
                        {
                            "name": entry.name,
                            "kind": "d" if entry.is_dir(follow_symlinks=False) else "f",
                            "size": info.st_size,
                            "mtime_ns": getattr(
                                info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)
                            ),
                        }
                    )
                except OSError:
                    # An incomplete shallow listing is not a safe basis for cleanup.
                    return {
                        "relative_path": relative,
                        "kind": "error",
                        "error": "entry_stat",
                    }
    except OSError as error:
        return {
            "relative_path": relative,
            "kind": "error",
            "error": type(error).__name__,
        }
    result["entries"] = entries
    return result


def _file(root: Path, relative: str) -> dict:
    result = _stat(root / relative)
    result["relative_path"] = relative
    return result


def main() -> int:
    request = json.load(sys.stdin)
    root = Path(request["root"])
    directories = [
        _directory(root, record) for record in request.get("directories", [])
    ]
    files = [_file(root, relative) for relative in request.get("files", [])]
    json.dump({"directories": directories, "files": files}, sys.stdout)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocesses
    raise SystemExit(main())
