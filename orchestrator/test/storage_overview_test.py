import asyncio
import os
import stat
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api.zenstream import application_routes
from app import storage_overview as storage
from fastapi import FastAPI
from fastapi.testclient import TestClient


class StorageMeasurementTest(unittest.TestCase):
    def test_reports_seven_categories_without_recounting_shared_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            fixtures = {
                "movies/movie.mp4": 113,
                "data/orchestrator.db": 17,
                "data/orchestrator.db-wal": 5,
                "data/orchestrator.db.pre-20261126-shm": 13,
                "data/subtitle-cache/subtitle.vtt": 7,
                "data/metadata-cache/images/poster.webp": 11,
                "data/artwork-variant-cache/poster.webp": 19,
                "data/trickplay-cache/sheet.webp": 23,
                "temp/transcodes/session/segment.ts": 29,
                "data/offline-prepared-cache/bundle.zip": 31,
                "data/lumi/models/model/weights.gguf": 37,
                "ignored-collection/movie.mp4": 41,
            }
            for relative, size in fixtures.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bytes(size))
            store = SimpleNamespace(
                db=SimpleNamespace(db_file=str(data / "orchestrator.db")),
                list=lambda: [
                    {"type": "movies", "directory": str(root / "movies")},
                    {"type": "music", "directory": str(root / "data")},
                    {"type": "tv_series", "directory": str(root / "movies")},
                    {"type": "movies", "directory": str(root / "missing")},
                    {
                        "type": "collection",
                        "directory": str(root / "ignored-collection"),
                    },
                ],
            )
            with (
                patch.dict(os.environ, {"METADATA_PATH": str(data)}),
                patch.object(storage, "LibraryStore", return_value=store),
                patch.object(
                    storage,
                    "transcode_directory",
                    return_value=root / "temp/transcodes",
                ),
                patch.object(storage.lumi_host, "data_directory", data / "lumi"),
            ):
                result = storage.scan_storage()

        self.assertEqual(
            [
                (entry.category, entry.bytes, entry.unreadablePaths)
                for entry in result.categories
            ],
            [
                ("media", 113, 1),
                ("metadata", 42, 0),
                ("artwork", 30, 0),
                ("trickplay", 23, 0),
                ("transcodes", 29, 0),
                ("offline", 31, 0),
                ("lumi", 37, 0),
            ],
        )
        self.assertEqual(result.totalBytes, 305)

    def test_file_identity_counts_hard_links_once(self):
        info = os.stat_result((stat.S_IFREG, 42, 7, 2, 0, 0, 113, 0, 0, 0))
        walk = storage._StorageWalk()
        self.assertEqual(walk.file_size(info), 113)
        self.assertEqual(walk.file_size(info), 0)

    def test_continues_after_a_directory_becomes_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "locked").mkdir()
            (root / "visible.webm").write_bytes(b"video")
            real_scandir = os.scandir

            def scandir(path):
                if path == root / "locked":
                    raise PermissionError(path)
                return real_scandir(path)

            with patch.object(storage.os, "scandir", side_effect=scandir):
                result = storage._StorageWalk().measure("media", [root])
            self.assertEqual((result.bytes, result.unreadablePaths), (5, 1))


class StorageSnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_share_pending_scan_and_recent_snapshot(self):
        ready = asyncio.Event()
        calls = 0

        async def collect():
            nonlocal calls
            calls += 1
            await ready.wait()
            overview = storage.StorageOverview(
                measuredAt=datetime.now(UTC), totalBytes=0, categories=[]
            )
            return time.monotonic(), overview

        with (
            patch.object(storage, "_scan", None),
            patch.object(storage, "_collect", collect),
        ):
            pending = await asyncio.gather(
                storage.storage_overview(False), storage.storage_overview(True)
            )
            self.assertEqual(pending, [(True, None), (True, None)])
            self.assertEqual(calls, 1)
            ready.set()
            _, snapshot = await storage.storage_overview(False)
            self.assertIsNotNone(snapshot)
            self.assertEqual(await storage.storage_overview(False), (False, snapshot))
            self.assertEqual(calls, 1)


class StorageRouteTest(unittest.TestCase):
    def test_admin_authentication_and_pending_response(self):
        app = FastAPI()
        app.include_router(application_routes.router)
        with (
            TestClient(app) as client,
            patch.object(
                application_routes, "storage_overview", return_value=(True, None)
            ) as overview,
        ):
            self.assertEqual(client.get("/api/admin/storage").status_code, 401)
            overview.assert_not_called()
            with patch.object(
                application_routes.Admin,
                "from_token",
                return_value=SimpleNamespace(username="admin"),
            ):
                response = client.get(
                    "/api/admin/storage?refresh=true",
                    headers={"Username": "admin", "TOKEN": "test-session"},
                )
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.headers["Retry-After"], "2")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            overview.assert_awaited_once_with(True)
