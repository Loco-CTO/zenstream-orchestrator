import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

from app.account_sync import clear_history_barrier
from app.catalog import Catalog
from app.database import DatabaseHandler
from app.offline_sync import AccountSyncRequest, AccountSyncService
from fastapi import HTTPException
from pydantic import ValidationError


class OfflinePlaybackSyncTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.addCleanup(self.db.close)
        for sql in (
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT)",
            "CREATE TABLE user_library_access(user_id TEXT,library_id TEXT)",
            "INSERT INTO library_entities VALUES('movie','library',NULL,'movie')",
            "INSERT INTO library_entities VALUES('track','library',NULL,'track')",
            "INSERT INTO user_library_access VALUES('viewer','library')",
            "CREATE TABLE offline_server(id INTEGER PRIMARY KEY,instance_id TEXT)",
            "INSERT INTO offline_server VALUES(1,'instance')",
            "CREATE TABLE account_sync_epochs(user_id TEXT PRIMARY KEY,history_epoch INTEGER DEFAULT 0,privacy_epoch INTEGER DEFAULT 0)",
            "CREATE TABLE account_sync_devices(user_id TEXT,device_id TEXT,sequence INTEGER,PRIMARY KEY(user_id,device_id))",
            "CREATE TABLE account_sync_fields(user_id TEXT,target_id TEXT,field TEXT,revision INTEGER,activity_ms INTEGER,event_id TEXT,PRIMARY KEY(user_id,target_id,field))",
            "CREATE TABLE user_item_state(user_id TEXT,entity_id TEXT,favorite INTEGER,played INTEGER,play_count INTEGER,position_seconds REAL,duration_seconds REAL,last_played_at TEXT,updated_at TEXT,PRIMARY KEY(user_id,entity_id))",
            "CREATE TABLE user_play_events(user_id TEXT,entity_id TEXT,playback_instance_id TEXT,started_at TEXT,PRIMARY KEY(user_id,playback_instance_id))",
        ):
            self.db.execute(sql)
        self.catalog = Catalog.__new__(Catalog)
        self.catalog.db = self.db
        self.catalog._context = lambda _user: None
        self.catalog.require_entity = self.require_entity
        self.catalog._entity_row = lambda target: (
            target,
            "library",
            None,
            "track" if target == "track" else "movie",
        )
        self.catalog._read_model_ready = lambda: False
        self.catalog._audio_fields = lambda _target: (None, None, 100)
        self.catalog._following_for_entity = lambda *_args: False
        self.catalog._invalidate_home_cache = MagicMock()
        self.catalog._watch_history_enabled = lambda _user: True
        self.catalog._state = lambda user, target: self.catalog._direct_state(
            self.catalog._state_row(user, target)
        )
        self.catalog.update_state = lambda user, target, changes: (
            self.catalog._update_without_explicit_played(
                user, target, changes, forced_played=changes.get("played")
            )
        )
        self.service = AccountSyncService(self.catalog)
        self.now = int(time.time() * 1000) - 10000

    def require_entity(self, user, target):
        if user != "viewer" or target not in {"movie", "track"}:
            raise HTTPException(404, "Item not found")
        return self.catalog._entity_row(target)

    def operation(self, sequence=1, **values):
        return {
            "id": str(uuid.uuid4()),
            "sequence": sequence,
            "kind": "checkpoint",
            "targetId": "movie",
            "activityMs": self.now,
            "payload": {"positionSeconds": 20, "durationSeconds": 100},
            **values,
        }

    def send(self, *operations, device="phone", user="viewer"):
        batch = AccountSyncRequest(
            serverInstanceId="instance", deviceId=device, operations=list(operations)
        )
        return self.service.synchronize(user, batch)["acknowledgements"]

    def test_retry_does_not_apply_state_twice(self):
        operation = self.operation()
        self.assertEqual(self.send(operation)[0]["outcome"], "applied")
        self.assertEqual(self.send(operation)[0]["outcome"], "duplicate")
        self.assertEqual(self.catalog._state("viewer", "movie")["positionSeconds"], 20)

    def test_older_device_cannot_overwrite_newer_position(self):
        newer = self.operation(
            activityMs=self.now + 1000,
            payload={"positionSeconds": 40, "durationSeconds": 100},
        )
        older = self.operation()
        self.send(newer, device="tablet")
        result = self.send(older)[0]
        self.assertEqual(result["outcome"], "superseded")
        self.assertEqual(result["state"]["positionSeconds"], 40)

    def test_later_rewind_wins_instead_of_maximum_position(self):
        self.send(
            self.operation(payload={"positionSeconds": 80, "durationSeconds": 100})
        )
        result = self.send(self.operation(sequence=2, activityMs=self.now + 1000))[0]
        self.assertEqual(result["state"]["positionSeconds"], 20)

    def test_explicit_unwatched_and_completion_form_coherent_states(self):
        self.send(self.operation(kind="complete"))
        result = self.send(
            self.operation(sequence=2, kind="unwatched", activityMs=self.now + 1000)
        )[0]
        self.assertFalse(result["state"]["played"])
        self.assertEqual(result["state"]["positionSeconds"], 0)

    def test_disabled_history_blocks_automatic_events_but_allows_explicit_watched(self):
        self.catalog._watch_history_enabled = lambda _user: False
        self.assertEqual(self.send(self.operation())[0]["outcome"], "privacy_blocked")
        self.assertTrue(
            self.send(self.operation(sequence=2, kind="watched"))[0]["state"]["played"]
        )

    def test_cleared_history_cannot_be_resurrected(self):
        clear_history_barrier(self.db, "viewer")
        self.assertEqual(self.send(self.operation())[0]["outcome"], "privacy_blocked")

    def test_clock_anomaly_does_not_replace_changed_server_state(self):
        self.send(self.operation(), device="tablet")
        result = self.send(self.operation(activityMs=self.now + 86400000))[0]
        self.assertEqual(result["outcome"], "superseded")

    def test_music_events_count_once_and_preserve_occurrence_time(self):
        operation = self.operation(kind="music_play", targetId="track")
        self.send(operation)
        self.send(operation)
        self.send(
            self.operation(
                sequence=2,
                kind="music_play",
                targetId="track",
                activityMs=self.now - 1000,
            )
        )
        self.assertEqual(self.catalog._state("viewer", "track")["playCount"], 2)
        dates = self.db.read_execute("SELECT started_at FROM user_play_events")
        self.assertEqual(len(dates), 2)
        self.assertNotEqual(dates[0][0], dates[1][0])

    def test_invalid_item_is_terminal_without_leaking_state(self):
        result = self.send(self.operation(targetId="hidden"))[0]
        self.assertEqual(result["outcome"], "rejected")
        self.assertIsNone(result["state"])
        self.assertEqual(self.send(self.operation(sequence=2))[0]["outcome"], "applied")

    def test_sequence_gap_does_not_mutate_state(self):
        with self.assertRaises(HTTPException) as raised:
            self.send(self.operation(sequence=2))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            self.db.read_execute("SELECT count(*) FROM user_item_state")[0][0], 0
        )

    def test_favorites_are_independent_of_playback_and_privacy(self):
        self.catalog._watch_history_enabled = lambda _user: False
        self.send(self.operation(kind="favorite", activityMs=self.now - 1000))
        self.assertTrue(self.catalog._state("viewer", "movie")["favorite"])
        self.send(self.operation(sequence=2, kind="unfavorite", activityMs=self.now))
        self.assertFalse(self.catalog._state("viewer", "movie")["favorite"])

    def test_older_favorite_cannot_reverse_a_newer_toggle(self):
        self.send(
            self.operation(kind="favorite", activityMs=self.now + 1000), device="tablet"
        )
        self.assertEqual(
            self.send(self.operation(kind="unfavorite"))[0]["outcome"], "superseded"
        )

    @patch("app.offline_sync.FollowService")
    def test_follow_is_explicit_idempotent_and_permission_filtered(self, follow):
        operation = self.operation(kind="follow")
        self.send(operation)
        self.send(operation)
        follow.return_value.set_for_entity.assert_called_once_with(
            "viewer", "movie", True
        )
        self.assertEqual(
            self.send(self.operation(sequence=2, kind="unfollow", targetId="hidden"))[
                0
            ]["outcome"],
            "rejected",
        )
        self.assertEqual(follow.return_value.set_for_entity.call_count, 1)

    @patch("app.offline_sync.FollowService")
    def test_track_follow_is_rejected(self, follow):
        self.assertEqual(
            self.send(self.operation(kind="follow", targetId="track"))[0]["outcome"],
            "rejected",
        )
        follow.assert_not_called()

    def test_batches_are_bounded(self):
        with self.assertRaises(ValidationError):
            AccountSyncRequest(
                serverInstanceId="instance",
                deviceId="phone",
                operations=[self.operation()] * 101,
            )
