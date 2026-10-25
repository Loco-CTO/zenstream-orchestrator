import time
import uuid

from app.offline_sync import AccountSyncRequest, AccountSyncService
from test.playlists_test import PlaylistServiceTest


class OfflinePlaylistSyncTest(PlaylistServiceTest):
    def setUp(self):
        super().setUp()
        for sql in (
            "CREATE TABLE offline_server(id INTEGER PRIMARY KEY,instance_id TEXT)",
            "INSERT INTO offline_server VALUES(1,'instance')",
            "CREATE TABLE account_sync_epochs(user_id TEXT PRIMARY KEY,history_epoch INTEGER DEFAULT 0,privacy_epoch INTEGER DEFAULT 0)",
            "CREATE TABLE account_sync_devices(user_id TEXT,device_id TEXT,sequence INTEGER,PRIMARY KEY(user_id,device_id))",
            "CREATE TABLE account_sync_fields(user_id TEXT,target_id TEXT,field TEXT,revision INTEGER,activity_ms INTEGER,event_id TEXT,value_json TEXT,PRIMARY KEY(user_id,target_id,field))",
        ):
            self.db.execute(sql)
        self.sync = AccountSyncService(self.catalog)
        self.now = int(time.time() * 1000) - 10000
        self.playlist = str(uuid.uuid4())
        self.sequence = 0
        self.sequences = {}

    def send(
        self,
        kind,
        payload=None,
        *,
        device="phone",
        at=None,
        target=None,
        user="owner",
        sequence=None,
    ):
        self.sequence += 1
        key = (user, device)
        self.sequences[key] = sequence or self.sequences.get(key, 0) + 1
        operation = {
            "id": str(uuid.uuid4()),
            "sequence": self.sequences[key],
            "kind": kind,
            "targetId": target or self.playlist,
            "activityMs": at or self.now + self.sequence,
            "payload": payload or {},
        }
        return self.sync.synchronize(
            user,
            AccountSyncRequest(
                serverInstanceId="instance", deviceId=device, operations=[operation]
            ),
        )["acknowledgements"][0]

    def create(self):
        result = self.send("playlist_create", {"name": "Offline"})
        self.assertEqual(result["outcome"], "applied")
        self.assertTrue(result["state"]["isPrivate"])

    def test_offline_creation_then_edit_and_delete(self):
        self.create()
        self.assertEqual(
            self.send("playlist_name", {"value": "Renamed"})["state"]["name"], "Renamed"
        )
        self.assertEqual(self.send("playlist_delete")["outcome"], "applied")
        self.assertEqual(
            self.send("playlist_name", {"value": "Resurrect"})["outcome"], "superseded"
        )
        self.assertEqual(
            self.send("playlist_create", {"name": "Resurrect"})["outcome"], "superseded"
        )

    def test_duplicates_share_one_canonical_entry(self):
        self.create()
        first = self.send("playlist_add", {"trackId": "track-a"})
        second = self.send("playlist_add", {"trackId": "track-a"})
        self.assertEqual(first["state"]["entryId"], second["state"]["entryId"])
        self.assertEqual(
            self.service.get_playlist("owner", self.playlist, "en")["itemCount"], 1
        )

    def test_order_snapshot_preserves_concurrent_additions(self):
        self.create()
        self.send("playlist_add", {"trackId": "track-a"})
        self.send("playlist_order", {"trackIds": ["track-a"]})
        self.send("playlist_add", {"trackId": "track-b"})
        self.assertEqual(
            [
                row["item"]["id"]
                for row in self.service.get_playlist("owner", self.playlist, "en")[
                    "items"
                ]
            ],
            ["track-a", "track-b"],
        )
        self.send("playlist_order", {"trackIds": ["track-b", "track-a"]})
        self.assertEqual(
            [
                row["item"]["id"]
                for row in self.service.get_playlist("owner", self.playlist, "en")[
                    "items"
                ]
            ],
            ["track-b", "track-a"],
        )

    def test_older_remove_does_not_reverse_newer_add(self):
        self.create()
        self.send("playlist_add", {"trackId": "track-a"}, at=self.now + 1000)
        result = self.send(
            "playlist_remove",
            {"trackId": "track-a"},
            device="tablet",
            sequence=1,
            at=self.now + 500,
        )
        self.assertEqual(result["outcome"], "superseded")

    def test_ownership_and_track_permissions_are_enforced(self):
        self.create()
        result = self.send(
            "playlist_name", {"value": "Stolen"}, user="viewer", sequence=1
        )
        self.assertEqual(result["outcome"], "rejected")
        self.assertIsNone(result["state"])
        self.assertEqual(
            self.send("playlist_add", {"trackId": "movie"})["outcome"], "rejected"
        )

    def test_public_share_changes_are_not_accepted(self):
        self.send(
            "playlist_create",
            {"name": "Offline", "isPrivate": False, "shareToken": "untrusted"},
        )
        self.assertTrue(
            self.service.get_summary("owner", self.playlist, "en")["isPrivate"]
        )
