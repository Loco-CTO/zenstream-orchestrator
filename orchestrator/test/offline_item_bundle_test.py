import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from api.zenstream import offline_routes
from app.netease_lyrics import NetEaseUnavailable
from app.offline_bundle import external_subtitles, item_bundle
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.responses import Response


class OfflineItemBundleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.subtitle = Path(self.directory.name) / "japanese.vtt"
        self.subtitle.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n")
        self.streams = []
        self.assets = SimpleNamespace(
            catalog=MagicMock(),
            db=MagicMock(),
            media=MagicMock(),
            source=MagicMock(),
            _owned=MagicMock(
                side_effect=AssertionError(
                    "item refresh must not depend on an asset reservation"
                )
            ),
        )
        self.assets.catalog.require_entity.return_value = (
            "movie",
            "library",
            None,
            "track",
        )
        self.assets.catalog.items_by_ids.return_value = [
            {"id": "movie", "type": "track", "name": "Title"}
        ]
        self.assets.catalog._has_table.return_value = False
        self.assets.db.read_execute.return_value = []
        self.assets.source.return_value = {
            "id": "source",
            "revision": "original",
            "size": 100,
            "durationSeconds": 120,
            "height": 1080,
        }
        self.assets.media.sources.side_effect = lambda *_: [
            {"id": "source", "streams": self.streams}
        ]
        self.assets.media._file_path.return_value = (None, self.subtitle)
        self.assets.media.lyrics.return_value = {"lyrics": {"text": "First lyrics"}}

    def add_subtitle(self, identity="japanese", index=1000):
        self.streams.append(
            {
                "index": index,
                "codec_type": "subtitle",
                "codec_name": "webvtt",
                "kind": "subtitle",
                "fileId": identity,
                "tags": {"language": "ja", "title": "Japanese"},
            }
        )

    def refresh(self, revision="original"):
        return item_bundle(self.assets, "viewer", "movie", "source", revision, "en")

    def test_new_language_is_discovered_without_a_prepared_asset(self):
        before = self.refresh()
        self.add_subtitle()
        after = self.refresh()
        self.assertEqual(after["subtitleScope"], "external")
        self.assertEqual(after["sourceStatus"], "unchanged")
        self.assertEqual(after["subtitles"][0]["id"], "file:japanese")
        self.assertIn(
            "/api/offline/items/movie/subtitles/japanese.vtt?",
            after["subtitles"][0]["url"],
        )
        self.assertNotEqual(before["supplementRevision"], after["supplementRevision"])
        self.assets._owned.assert_not_called()

    def test_corrections_labels_and_removed_sidecars_change_revision(self):
        self.add_subtitle()
        first = self.refresh()
        self.subtitle.write_text("WEBVTT\n\nCorrected text")
        corrected = self.refresh()
        self.assertNotEqual(
            first["supplementRevision"], corrected["supplementRevision"]
        )
        self.streams[0]["tags"]["title"] = "Japanese SDH"
        renamed = self.refresh()
        self.assertNotEqual(
            corrected["supplementRevision"], renamed["supplementRevision"]
        )
        self.streams.clear()
        self.assertEqual(self.refresh()["subtitles"], [])

    def test_subtitle_identity_survives_catalog_index_changes(self):
        self.add_subtitle()
        first = self.refresh()["subtitles"][0]
        self.streams[0]["index"] = 1005
        second = self.refresh()["subtitles"][0]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["revision"], second["revision"])

    def test_added_and_corrected_lyrics_change_attachment_revision(self):
        self.assets.media.lyrics.return_value = {"lyrics": None}
        first = self.refresh()
        self.assets.media.lyrics.return_value = {
            "lyrics": {"text": "New lyrics", "lines": []}
        }
        second = self.refresh()
        self.assertEqual(second["lyrics"]["text"], "New lyrics")
        self.assertNotEqual(first["supplementRevision"], second["supplementRevision"])
        self.assets.media.lyrics.return_value = {
            "lyrics": {"text": "Corrected lyrics", "lines": []}
        }
        self.assertNotEqual(
            second["supplementRevision"], self.refresh()["supplementRevision"]
        )

    def test_changed_source_does_not_attach_new_timing_data_to_old_media(self):
        self.add_subtitle()
        result = self.refresh("older")
        self.assertEqual(result["sourceStatus"], "changed")
        self.assertEqual(result["sourceRevision"], "original")
        self.assertEqual(result["subtitles"], [])
        self.assertEqual(result["chapters"], [])
        self.assertIsNone(result["lyrics"])
        self.assertEqual(result["source"]["id"], "source")

    def test_other_editions_lyrics_and_bitmap_sidecars_are_excluded(self):
        self.add_subtitle()
        self.streams[0]["kind"] = "lyrics"
        self.assertEqual(self.refresh()["subtitles"], [])
        self.streams[0].update(kind="subtitle", codec_name="sup")
        self.assertEqual(self.refresh()["subtitles"], [])
        self.assets.media.sources.return_value = [
            {"id": "other", "streams": self.streams}
        ]
        self.assets.media.sources.side_effect = None
        self.assertEqual(
            external_subtitles(self.assets, "viewer", "movie", "source", "original")[0],
            "unavailable",
        )

    def test_access_is_checked_before_source_or_filesystem_work(self):
        self.assets.catalog.require_entity.side_effect = HTTPException(
            403, "Access denied"
        )
        with self.assertRaises(HTTPException):
            self.refresh()
        self.assets.source.assert_not_called()
        self.assets.media._file_path.assert_not_called()

    def test_public_source_options_do_not_expose_probe_paths_or_private_tags(self):
        self.add_subtitle()
        self.streams[0]["path"] = "private-filesystem-location"
        self.streams[0]["tags"]["filename"] = "private-probe-tag"
        public = self.refresh()["availableSources"]
        self.assertNotIn("private-filesystem-location", str(public))
        self.assertNotIn("private-probe-tag", str(public))


class OfflineItemBundleRoutesTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(offline_routes.router)
        self.client = TestClient(app)
        self.auth = patch.object(
            offline_routes,
            "_require_account",
            AsyncMock(return_value=({"id": "viewer"}, "session")),
        )
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def test_subtitle_delivery_validates_source_membership_and_revision(self):
        url = "/api/offline/items/movie/subtitles/japanese.vtt?sourceId=source&sourceRevision=revision&v=current"
        with (
            patch.object(
                offline_routes,
                "run_foreground",
                AsyncMock(
                    return_value=(
                        "unchanged",
                        "revision",
                        [{"fileId": "japanese", "revision": "current"}],
                    )
                ),
            ),
            patch.object(
                offline_routes,
                "subtitle",
                AsyncMock(return_value=Response("WEBVTT", media_type="text/vtt")),
            ) as deliver,
        ):
            result = self.client.get(url)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.text, "WEBVTT")
            deliver.assert_awaited_once()
            self.assertEqual(
                self.client.get(url.replace("v=current", "v=stale")).status_code, 409
            )
            self.assertEqual(
                self.client.get(url.replace("/japanese.vtt", "/other.vtt")).status_code,
                404,
            )

    def test_prepared_download_opts_into_local_first_netease_lyrics(self):
        url = "/api/offline/assets/asset/bundle"
        track = {"id": "track", "type": "track"}
        local = {"source": "sidecar", "lines": [{"text": "Local"}]}
        remote = {"source": "netease", "lines": [{"text": "Remote"}]}
        payload = {
            "itemId": "track",
            "items": [track],
            "lyrics": None,
            "subtitles": [],
            "chapters": [],
            "segments": [],
            "episodeNeighbors": None,
            "warnings": [],
            "supplementRevision": "local-revision",
        }
        with (
            patch.object(
                offline_routes,
                "run_foreground",
                AsyncMock(
                    side_effect=[
                        dict(payload),
                        {**payload, "lyrics": local},
                        dict(payload),
                        dict(payload),
                    ]
                ),
            ),
            patch.object(offline_routes, "netease_enabled", return_value=True),
            patch.object(
                offline_routes,
                "fetch_netease_lyrics",
                AsyncMock(side_effect=[remote, NetEaseUnavailable()]),
            ) as lookup,
        ):
            self.assertIsNone(self.client.get(url).json()["lyrics"])
            self.assertEqual(
                self.client.get(url, params={"fallback": "netease"}).json()["lyrics"],
                local,
            )
            lookup.assert_not_awaited()
            result = self.client.get(url, params={"fallback": "netease"}).json()
            self.assertEqual(result["lyrics"], remote)
            self.assertNotEqual(result["supplementRevision"], "local-revision")
            lookup.assert_awaited_once_with(track)
            response = self.client.get(url, params={"fallback": "netease"})
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["lyrics"])
            self.assertIn("lyrics_unavailable", response.json()["warnings"])

    def test_source_replacement_blocks_attachment_delivery(self):
        with (
            patch.object(
                offline_routes,
                "run_foreground",
                AsyncMock(return_value=("changed", "new", [])),
            ),
            patch.object(offline_routes, "subtitle", AsyncMock()) as deliver,
        ):
            response = self.client.get(
                "/api/offline/items/movie/subtitles/japanese.vtt?sourceId=source&sourceRevision=old"
            )
            self.assertEqual(response.status_code, 409)
            deliver.assert_not_awaited()

    def test_item_bundle_requires_authenticated_account(self):
        with patch.object(
            offline_routes,
            "_require_account",
            AsyncMock(side_effect=HTTPException(401, "Sign in")),
        ):
            response = self.client.get(
                "/api/offline/items/movie/bundle?sourceId=source&sourceRevision=revision"
            )
            self.assertEqual(response.status_code, 401)
