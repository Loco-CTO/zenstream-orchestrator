import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from api.zenstream import offline_routes
from app.offline_assets import OfflineAssetRequest, OfflineAssets, byte_range
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test.offline_asset_queue_test import OfflineAssetQueueTest


class OfflineOriginalAssetTest(OfflineAssetQueueTest):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "fixture.flac"
        self.content = bytes(range(256)) * 32
        self.path.write_bytes(self.content)
        self.db.execute(
            "CREATE TABLE media_sources(id TEXT,entity_id TEXT,container TEXT,duration_seconds REAL,bitrate INTEGER,width INTEGER,height INTEGER,video_codec TEXT,audio_codec TEXT,probe_payload TEXT)"
        )
        probe = json.dumps(
            {
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "audio",
                        "codec_name": "flac",
                        "tags": {"language": "eng", "filename": "private-path"},
                    }
                ]
            }
        )
        self.db.execute(
            "INSERT INTO media_sources VALUES('source','movie','flac',100,800000,NULL,NULL,NULL,'flac',?)",
            (probe,),
        )
        self.catalog = MagicMock()
        self.catalog.db = self.db
        media = MagicMock()
        media.direct_path_and_metadata.side_effect = lambda *_args: (
            self.path,
            self.path.stat().st_size,
            "audio/flac",
        )
        self.assets = OfflineAssets(self.catalog, media, self.queue)

    def prepare(self):
        body = OfflineAssetRequest(selectionId="selection", itemId="movie")
        result = self.assets.request("viewer", body)
        self.queue.process_next(self.assets.produce, self.stop)
        return self.assets.get("viewer", result["id"])

    def client(self):
        app = FastAPI()
        app.include_router(offline_routes.router)
        auth = patch.object(
            offline_routes,
            "_require_account",
            AsyncMock(return_value=({"id": "viewer"}, "test")),
        )
        assets = patch.object(offline_routes, "OfflineAssets", return_value=self.assets)
        auth.start()
        assets.start()
        self.addCleanup(auth.stop)
        self.addCleanup(assets.stop)
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def test_original_digest_and_manifest_are_safe(self):
        result = self.prepare()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["sha256"], hashlib.sha256(self.content).hexdigest())
        self.assertEqual(result["sizeBytes"], len(self.content))
        self.assertNotIn(str(self.path), str(result))
        self.assertNotIn("filename", str(result))

    def test_video_manifest_preserves_hdr_recovery_restrictions(self):
        tracks = OfflineAssets.tracks(
            {
                "probe": {
                    "streams": [
                        {
                            "index": 0,
                            "codec_type": "video",
                            "codec_name": "hevc",
                            "color_transfer": "smpte2084",
                            "color_primaries": "bt2020",
                        },
                        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
                    ]
                }
            }
        )
        self.assertTrue(tracks[0]["isHdr"])
        self.assertEqual(tracks[0]["colorTransfer"], "smpte2084")
        self.assertEqual(tracks[0]["colorPrimaries"], "bt2020")
        self.assertNotIn("isHdr", tracks[1])

    def test_range_delivery_and_head_match(self):
        result = self.prepare()
        client = self.client()
        url = result["contentUrl"]
        response = client.get(
            url,
            headers={
                "Range": "bytes=100-199",
                "If-Range": '"' + result["sha256"] + '"',
            },
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, self.content[100:200])
        self.assertEqual(
            response.headers["content-range"], f"bytes 100-199/{len(self.content)}"
        )
        head = client.head(url, headers={"Range": "bytes=100-199"})
        self.assertEqual(head.status_code, 206)
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["content-length"], "100")

    def test_validator_mismatch_returns_the_entire_representation(self):
        result = self.prepare()
        response = self.client().get(
            result["contentUrl"],
            headers={"Range": "bytes=100-", "If-Range": '"old-version"'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.content)

    def test_invalid_range_is_416(self):
        result = self.prepare()
        response = self.client().get(
            result["contentUrl"], headers={"Range": "bytes=999999-"}
        )
        self.assertEqual(response.status_code, 416)
        self.assertEqual(
            response.headers["content-range"], f"bytes */{len(self.content)}"
        )

    def test_replaced_source_cannot_resume_under_old_identity(self):
        result = self.prepare()
        self.path.write_bytes(b"changed")
        with self.assertRaises(HTTPException) as raised:
            self.assets.open_delivery("viewer", result["id"])
        self.assertEqual(raised.exception.status_code, 409)

    def test_another_account_cannot_read_asset_metadata(self):
        result = self.prepare()
        with self.assertRaises(HTTPException):
            self.assets.get("other", result["id"])

    def test_source_grants_are_rechecked_before_delivery(self):
        result = self.prepare()
        self.catalog.require_entity.side_effect = HTTPException(404, "Item not found")
        with self.assertRaises(HTTPException):
            self.assets.open_delivery("viewer", result["id"])

    def test_suffix_and_open_ended_ranges(self):
        self.assertEqual(byte_range("bytes=-10", 100), (90, 99))
        self.assertEqual(byte_range("bytes=80-", 100), (80, 99))
        for value in (
            "bytes=-0",
            "bytes=0-1,3-4",
            "bytes=90-20",
            "bytes=-",
            "items=0-1",
        ):
            with self.assertRaises(ValueError):
                byte_range(value, 100)
