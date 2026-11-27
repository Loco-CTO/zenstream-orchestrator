"""Measure the server's configured media folders and managed storage."""

import asyncio
import os
import stat
import time
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.artwork_variants import VARIANT_CACHE_DIRECTORY
from app.library import LibraryStore
from app.logging_config import get_logger
from app.lumi_host import lumi_host
from app.offline_asset_queue import OfflineAssetQueue
from app.paths import transcode_directory
from app.trickplay import TrickplayStore
from pydantic import BaseModel

StorageCategory = Literal[
    "media", "metadata", "artwork", "trickplay", "transcodes", "offline", "lumi"
]

logger = get_logger("storage")
_CACHE_SECONDS = 60
_REFRESH_INTERVAL_SECONDS = 5
_MOUNT_POINT_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)


class StorageUsage(BaseModel):
    category: StorageCategory
    bytes: int
    unreadablePaths: int


class StorageOverview(BaseModel):
    measuredAt: datetime
    totalBytes: int
    categories: list[StorageUsage]


class _StorageWalk:
    def __init__(self):
        self.directories: set[Path] = set()
        self.root_files: set[Path] = set()
        self.hard_links: set[tuple[int, int]] = set()

    def file_size(self, info: os.stat_result) -> int:
        if info.st_nlink > 1 and info.st_ino:
            identity = (info.st_dev, info.st_ino)
            if identity in self.hard_links:
                return 0
            self.hard_links.add(identity)
        return info.st_size

    def measure(
        self,
        category: StorageCategory,
        paths: list[Path],
        *,
        required: Collection[Path] = (),
    ) -> StorageUsage:
        usage = StorageUsage(category=category, bytes=0, unreadablePaths=0)
        pending = []
        for path in paths:
            try:
                root = path.resolve()
                info = root.stat()
            except FileNotFoundError:
                usage.unreadablePaths += int(path in required)
                continue
            except (OSError, RuntimeError, ValueError):
                usage.unreadablePaths += 1
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.append(root)
            elif stat.S_ISREG(info.st_mode) and category != "media":
                if root not in self.root_files:
                    self.root_files.add(root)
                    usage.bytes += self.file_size(info)
            else:
                usage.unreadablePaths += 1

        while pending:
            directory = pending.pop()
            if directory in self.directories:
                continue
            self.directories.add(directory)
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = directory / entry.name
                        if path in self.root_files:
                            continue
                        try:
                            # Windows DirEntry.stat() omits file and link identities.
                            info = (
                                path.lstat()
                                if os.name == "nt"
                                else entry.stat(follow_symlinks=False)
                            )
                        except FileNotFoundError:
                            continue  # A cache or playback file can expire during the scan.
                        except OSError:
                            usage.unreadablePaths += 1
                            continue
                        if (
                            _MOUNT_POINT_TAG is not None
                            and getattr(info, "st_reparse_tag", None)
                            == _MOUNT_POINT_TAG
                        ):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(path)
                        elif stat.S_ISREG(info.st_mode):
                            usage.bytes += self.file_size(info)
            except FileNotFoundError:
                continue
            except OSError:
                usage.unreadablePaths += 1
        return usage


def scan_storage() -> StorageOverview:
    libraries = LibraryStore()
    db = libraries.db
    database = Path(db.db_file)
    data = database.parent
    media = [
        Path(library["directory"])
        for library in libraries.list()
        if library["type"] != "collection" and library["directory"]
    ]
    walk = _StorageWalk()
    metadata = walk.measure(
        "metadata",
        [
            database,
            database.with_name(f"{database.name}-wal"),
            database.with_name(f"{database.name}-shm"),
            database.with_name(f"{database.name}-journal"),
            *data.glob(f"{database.name}.pre-*"),
            data / "subtitle-cache",
        ],
        required=(database,),
    )
    # Claim managed files before walking library folders, which may contain them.
    managed = [
        metadata,
        walk.measure(
            "artwork",
            [
                data / "image-cache",
                data / VARIANT_CACHE_DIRECTORY,
                data / "metadata-cache" / "images",
                data / "future-metadata-cache",
                data / "people-cache",
                data / "screen-extractor-cache",
            ],
        ),
        walk.measure("trickplay", [TrickplayStore(db).cache_root()]),
        walk.measure("transcodes", [transcode_directory()]),
        walk.measure("offline", [OfflineAssetQueue(db).cache_directory]),
        walk.measure("lumi", [lumi_host.data_directory / "models"]),
    ]
    categories = [walk.measure("media", media, required=set(media)), *managed]
    return StorageOverview(
        measuredAt=datetime.now(UTC),
        totalBytes=sum(usage.bytes for usage in categories),
        categories=categories,
    )


_scan: asyncio.Task[tuple[float, StorageOverview | None]] | None = None


async def _collect() -> tuple[float, StorageOverview | None]:
    try:
        result = await asyncio.to_thread(scan_storage)
    except Exception:
        logger.exception("could not measure server storage")
        result = None
    return time.monotonic(), result


async def storage_overview(refresh: bool) -> tuple[bool, StorageOverview | None]:
    """Share one scan across requests and let clients poll while it runs."""
    global _scan
    if _scan is not None and _scan.done() and not _scan.cancelled():
        completed, result = _scan.result()
        max_age = (
            _REFRESH_INTERVAL_SECONDS if refresh or result is None else _CACHE_SECONDS
        )
        if time.monotonic() - completed < max_age:
            return False, result
    if _scan is None or _scan.done():
        _scan = asyncio.create_task(_collect())
    done, _ = await asyncio.wait({_scan}, timeout=0.25)
    if done:
        return False, next(iter(done)).result()[1]
    return True, None
