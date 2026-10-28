import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.ffmpeg_supervisor import run_ffmpeg
from app.offline_assets import OfflineAssetRequest
from app.offline_bundle import (
    artwork_descriptors,
    bundle,
    prepare_subtitles,
    selected_subtitles,
)
from app.offline_conversion import probe_file
from app.playback import ffmpeg_path, ffprobe_path
from fastapi import HTTPException


class OfflineBundleTest(unittest.TestCase):
    def setUp(self):
        self.item = {
            "id": "movie",
            "type": "movie",
            "name": "Movie",
            "metadata": {
                "images": {
                    "Primary": {
                        "url": "/api/catalog/items/movie/images/Primary?language=en&v=first&access=secret",
                        "blurHash": "hash",
                    }
                }
            },
            "userState": {"played": False},
        }
        catalog = MagicMock()
        catalog.require_entity.return_value = ("movie", "library", None, "movie")
        catalog.items_by_ids.side_effect = lambda *_args: [copy.deepcopy(self.item)]
        catalog._has_table.return_value = False
        db = MagicMock()

        def read(sql, _params=None):
            if sql.startswith("WITH RECURSIVE"):
                return [("movie",)]
            if sql.startswith("SELECT probe_payload"):
                return [
                    (
                        json.dumps(
                            {
                                "chapters": [
                                    {
                                        "start_time": "0",
                                        "end_time": "10",
                                        "tags": {"title": "Opening"},
                                    }
                                ]
                            }
                        ),
                        "media",
                    )
                ]
            if sql.startswith("SELECT revision"):
                return []
            raise AssertionError(sql)

        db.read_execute.side_effect = read
        catalog.db = db
        self.assets = SimpleNamespace(
            catalog=catalog,
            db=db,
            _owned=MagicMock(
                return_value=(
                    "movie",
                    json.dumps({"sourceId": "source"}),
                    "ready",
                    json.dumps(
                        {
                            "supplements": [
                                {
                                    "index": 2,
                                    "mode": "text",
                                    "storageName": "internal-name.vtt",
                                    "sha256": "digest",
                                }
                            ]
                        }
                    ),
                    None,
                )
            ),
        )

    def test_artwork_is_same_origin_and_never_persists_access_tickets(self):
        result = artwork_descriptors([self.item], "en")[0]
        self.assertNotIn("access=", result["url"])
        self.assertIn("v=first", result["url"])
        self.assertIn("w=320", result["url"])
        self.item["metadata"]["images"]["Primary"]["url"] = (
            "https://provider.invalid/private"
        )
        self.assertTrue(
            artwork_descriptors([self.item], "en")[0]["url"].startswith(
                "/api/catalog/items/movie/"
            )
        )

    def test_bundle_hides_storage_locations_and_versions_presentation_separately(self):
        first = bundle(self.assets, "viewer", "asset", "en")
        self.assertNotIn("internal-name", str(first))
        self.assertEqual(first["chapters"][0]["name"], "Opening")
        self.item["userState"]["played"] = True
        second = bundle(self.assets, "viewer", "asset", "en")
        self.assertEqual(first["presentationRevision"], second["presentationRevision"])
        self.assertTrue(second["items"][0]["userState"]["played"])
        self.item["name"] = "Corrected title"
        third = bundle(self.assets, "viewer", "asset", "en")
        self.assertNotEqual(
            second["presentationRevision"], third["presentationRevision"]
        )

    def test_selected_subtitles_bind_to_the_exact_source_and_sidecar_identity(self):
        assets = SimpleNamespace(media=MagicMock())
        assets.media.sources.return_value = [
            {
                "id": "selected",
                "streams": [
                    {
                        "index": 1000,
                        "codec_type": "subtitle",
                        "codec_name": "subrip",
                        "fileId": "sidecar",
                        "tags": {"language": "eng"},
                    }
                ],
            }
        ]
        request = OfflineAssetRequest(
            selectionId="selection",
            itemId="movie",
            sourceId="selected",
            subtitleStreamIndices=[1000],
        )
        self.assertEqual(
            selected_subtitles(assets, "viewer", request)[0]["fileId"], "sidecar"
        )
        with self.assertRaises(HTTPException):
            selected_subtitles(
                assets, "viewer", request.model_copy(update={"sourceId": "different"})
            )

    def test_bitmap_subtitles_require_the_original_profile(self):
        assets = SimpleNamespace(media=MagicMock())
        assets.media.sources.return_value = [
            {
                "id": "source",
                "streams": [
                    {
                        "index": 2,
                        "codec_type": "subtitle",
                        "codec_name": "hdmv_pgs_subtitle",
                    }
                ],
            }
        ]
        request = OfflineAssetRequest(
            selectionId="selection",
            itemId="movie",
            sourceId="source",
            profile="video-720p",
            subtitleStreamIndices=[2],
        )
        with self.assertRaises(HTTPException):
            selected_subtitles(assets, "viewer", request)

    def test_embedded_text_subtitles_are_prepared_without_a_live_session(self):
        if not ffmpeg_path() or not ffprobe_path():
            self.skipTest("FFmpeg and FFprobe are required")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitle = root / "text.srt"
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:00,900\nOffline caption\n", encoding="utf-8"
            )
            video = root / "source.mkv"
            run_ffmpeg(
                [
                    ffmpeg_path(),
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=red:size=32x32:rate=1",
                    "-i",
                    str(subtitle),
                    "-t",
                    "1",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "1",
                    "-c:s",
                    "srt",
                    str(video),
                ],
                timeout_seconds=30,
            )
            index = next(
                stream["index"]
                for stream in probe_file(video)["streams"]
                if stream["codec_type"] == "subtitle"
            )
            db = MagicMock()
            db.read_execute.return_value = [("viewer",)]
            assets = SimpleNamespace(
                db=db,
                queue=SimpleNamespace(cache_directory=root / "cache"),
                source=lambda *_args: {"path": video},
            )
            request = {
                "entityId": "movie",
                "sourceId": "source",
                "subtitles": [{"index": index, "codec": "subrip"}],
            }
            supplements, warnings = prepare_subtitles(
                assets, "a" * 64, request, lambda: False
            )
            self.assertEqual(warnings, [])
            text = (
                assets.queue.cache_directory / supplements[0]["storageName"]
            ).read_text(encoding="utf-8")
            self.assertIn("WEBVTT", text)
            self.assertIn("Offline caption", text)
