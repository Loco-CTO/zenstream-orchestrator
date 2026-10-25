import copy
import unittest
from unittest.mock import MagicMock, patch

from app.offline_reconcile import OfflineReconciler, OfflineReconcileRequest
from fastapi import HTTPException
from pydantic import ValidationError


class OfflineReconcileTest(unittest.TestCase):
    def setUp(self):
        self.catalog = MagicMock()
        self.catalog.allowed_libraries.return_value = {"allowed"}
        self.catalog._watch_history_enabled.return_value = True
        self.item = {"id": "movie", "name": "Movie", "userState": {"played": False}}
        self.catalog.items_by_ids.side_effect = lambda *_args: [
            copy.deepcopy(self.item)
        ]

        def read(sql, _params=None):
            if sql.startswith("SELECT instance_id"):
                return [("instance",)]
            if sql.startswith("SELECT history_epoch"):
                return [(3, 4)]
            if sql.startswith("SELECT revision"):
                return [(2, 1000, "event")]
            if sql.startswith("SELECT COUNT"):
                return [(1, 1)]
            raise AssertionError(sql)

        self.catalog.db.read_execute.side_effect = read
        self.service = OfflineReconciler(self.catalog)

    def send(self, objects=None, **values):
        body = OfflineReconcileRequest(
            serverInstanceId="instance",
            objects=objects or [{"id": "movie", "libraryId": "allowed"}],
            **values,
        )
        return self.service.reconcile("viewer", body)

    def test_presentation_refresh_preserves_separate_canonical_state_and_privacy(self):
        first = self.send()
        self.assertEqual(first["historyEpoch"], 3)
        self.assertEqual(first["privacyEpoch"], 4)
        self.assertTrue(first["libraryAccess"]["allowed"])
        version = first["objects"][0]["presentationRevision"]
        self.item["userState"]["played"] = True
        second = self.send([{"id": "movie", "presentationRevision": version}])
        result = second["objects"][0]
        self.assertEqual(result["status"], "unchanged")
        self.assertNotIn("item", result)
        self.assertTrue(result["state"]["played"])
        self.assertEqual(result["stateRevisions"]["playback"]["revision"], 2)
        self.item["name"] = "Corrected"
        self.assertEqual(self.send()["objects"][0]["item"]["name"], "Corrected")

    def test_missing_objects_and_revoked_library_are_distinct(self):
        self.catalog.items_by_ids.return_value = []
        self.catalog.items_by_ids.side_effect = None
        response = self.send(
            [
                {"id": "missing", "libraryId": "allowed"},
                {"id": "inaccessible", "libraryId": "revoked"},
            ]
        )
        self.assertEqual(response["libraryAccess"], {"allowed": True, "revoked": False})
        self.assertTrue(
            all(row["status"] == "unavailable" for row in response["objects"])
        )
        self.catalog.items_by_ids.assert_called_once()

    @patch("app.offline_reconcile.OfflineAssets")
    def test_source_changes_are_reported_without_preparing_or_delivering(self, assets):
        assets.return_value.source.return_value = {"revision": "new"}
        result = self.send(
            [{"id": "movie", "sourceId": "source", "sourceRevision": "old"}]
        )
        self.assertEqual(result["objects"][0]["sourceStatus"], "changed")
        assets.return_value.request.assert_not_called()
        assets.return_value.open_delivery.assert_not_called()

    @patch("app.offline_reconcile.PlaylistService")
    def test_playlist_pages_are_bounded_and_share_tokens_are_removed(self, playlists):
        playlists.return_value.get_playlist.return_value = {
            "id": "playlist",
            "updatedAt": "now",
            "itemCount": 201,
            "hasMore": True,
            "items": [],
            "shareToken": "secret",
        }
        result = self.send([{"id": "playlist", "kind": "playlist", "offset": 100}])
        row = result["objects"][0]
        self.assertEqual(row["nextOffset"], 200)
        self.assertNotIn("shareToken", row["playlist"])
        playlists.return_value.get_playlist.assert_called_once_with(
            "viewer", "playlist", "en", page=2, page_size=100
        )

    def test_batch_limits_and_recreated_server_identity(self):
        for objects in (
            [{"id": str(i)} for i in range(101)],
            [{"id": "same"}, {"id": "same"}],
            [{"id": "p", "kind": "playlist", "offset": 1}],
            [{"id": "p", "kind": "playlist"}, {"id": "movie"}],
        ):
            with self.assertRaises(ValidationError):
                OfflineReconcileRequest(serverInstanceId="instance", objects=objects)
        body = OfflineReconcileRequest(serverInstanceId="recreated")
        with self.assertRaises(HTTPException) as error:
            self.service.reconcile("viewer", body)
        self.assertEqual(error.exception.status_code, 409)
