import json
import unittest

from app.database import DatabaseHandler
from app.models.playback_viewer import PlaybackViewerStore


class PlaybackViewerStoreTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        for statement in (
            "CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT)",
            "CREATE TABLE user_sessions(id TEXT PRIMARY KEY,user_id TEXT,token_hash TEXT,expires_at TEXT,created_at TEXT,last_seen_at TEXT,device_id TEXT)",
            "CREATE TABLE user_devices(id TEXT PRIMARY KEY,user_id TEXT,device_key TEXT,device_type TEXT,browser TEXT,operating_system TEXT,device_name TEXT,client_name TEXT,client_version TEXT,ip_address TEXT,first_seen_at TEXT,last_seen_at TEXT)",
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,entity_type TEXT,relative_path TEXT,season_number INTEGER,episode_number INTEGER)",
            "CREATE TABLE media_sources(id TEXT PRIMARY KEY,container TEXT,bitrate INTEGER,width INTEGER,height INTEGER,video_codec TEXT,audio_codec TEXT,probe_payload TEXT)",
            "CREATE TABLE playback_sessions(id TEXT PRIMARY KEY,state TEXT,process_id INTEGER)",
            "CREATE TABLE playback_viewer_sessions(id TEXT PRIMARY KEY,user_id TEXT,auth_session_id TEXT,device_id TEXT,entity_id TEXT,source_id TEXT,worker_session_id TEXT,mode TEXT,state TEXT,engine TEXT,position_seconds REAL,duration_seconds REAL,paused INTEGER,created_at TEXT,last_heartbeat_at TEXT,ended_at TEXT,requested_bitrate INTEGER,audio_stream_id TEXT,requested_mode TEXT)",
            "CREATE TABLE playback_viewer_commands(id TEXT PRIMARY KEY,viewer_session_id TEXT,action TEXT,state TEXT,issued_at TEXT,expires_at TEXT,delivered_at TEXT,acknowledged_at TEXT,error TEXT)",
        ):
            self.db.execute(statement)
        self.db.execute("INSERT INTO users VALUES('user-1','viewer')")
        self.db.execute("INSERT INTO user_sessions VALUES('auth-1','user-1','hash','2099','now','now',NULL)")
        self.db.execute("INSERT INTO library_entities VALUES('entity-1','episode','Show/episode.mkv',2,4)")
        self.db.execute(
            "INSERT INTO media_sources VALUES('source-1','mkv',1000000,1920,1080,'hevc','aac',?)",
            (json.dumps({"streams": [{"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2}]}),),
        )
        self.store = PlaybackViewerStore(self.db)

    def tearDown(self):
        self.db.close()

    def test_direct_viewer_commands_and_diagnostics(self):
        viewer_id = self.store.create_viewer(
            "user-1",
            "auth-1",
            "entity-1",
            "source-1",
            "direct",
            {
                "engine": "web",
                "device": {
                    "deviceId": "browser-1",
                    "deviceType": "browser",
                    "browser": "Chrome",
                    "operatingSystem": "Windows",
                    "clientName": "ZenStream Web",
                    "clientVersion": "v1",
                },
                "startPositionSeconds": 42,
                "durationSeconds": 120,
                "maxStreamingBitrate": 1_000_000,
                "audioStreamId": 1,
            },
        )
        self.assertIsNotNone(viewer_id)
        summary = self.store.list_sessions()
        self.assertEqual(summary["sessions"][0]["item"]["subtitle"], "S02 · E04")
        command = self.store.issue_command(viewer_id, "pause")
        self.assertEqual(command["state"], "pending")
        heartbeat = self.store.heartbeat(
            "user-1",
            "auth-1",
            viewer_id,
            {"positionSeconds": 43, "paused": False},
        )
        self.assertEqual(heartbeat["commands"][0]["action"], "pause")
        self.store.heartbeat(
            "user-1",
            "auth-1",
            viewer_id,
            {"positionSeconds": 44, "paused": True, "commandAcks": [{"id": command["id"], "success": True}]},
        )
        detail = self.store.get_session(viewer_id)
        self.assertEqual(detail["diagnostics"]["container"], "mkv")
        self.assertEqual(detail["diagnostics"]["sourceBitrate"], 1_000_000)
        self.assertEqual(detail["diagnostics"]["audioChannels"], 2)
        stop = self.store.issue_command(viewer_id, "stop")
        self.store.heartbeat(
            "user-1", "auth-1", viewer_id, {"positionSeconds": 44, "paused": True}
        )
        self.store.end_viewer("user-1", "auth-1", viewer_id)
        self.assertEqual(
            self.db.read_execute(
                "SELECT state FROM playback_viewer_commands WHERE id=?", (stop["id"],)
            )[0][0],
            "acknowledged",
        )

    def test_shared_worker_survives_until_last_viewer_ends(self):
        profiles = [{"device": {"deviceId": f"browser-{index}"}} for index in (1, 2)]
        viewers = [
            self.store.create_viewer(
                "user-1", "auth-1", "entity-1", "source-1", "remux", profile, "worker-1"
            )
            for profile in profiles
        ]
        first = self.store.end_viewer("user-1", "auth-1", viewers[0])
        self.assertFalse(first["stopWorker"])
        second = self.store.end_viewer("user-1", "auth-1", viewers[1])
        self.assertTrue(second["stopWorker"])

    def test_stale_viewer_expires_and_device_removal_revokes_sessions(self):
        viewer_id = self.store.create_viewer(
            "user-1",
            "auth-1",
            "entity-1",
            "source-1",
            "remux",
            {"device": {"deviceId": "browser-stale"}},
            "worker-stale",
        )
        self.db.execute(
            "UPDATE playback_viewer_sessions SET last_heartbeat_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", viewer_id),
        )
        self.assertEqual(self.store.list_sessions()["sessions"], [])

        active_viewer = self.store.create_viewer(
            "user-1",
            "auth-1",
            "entity-1",
            "source-1",
            "remux",
            {"device": {"deviceId": "browser-remove"}},
            "worker-remove",
        )
        device = next(
            value
            for value in self.store.list_devices()["devices"]
            if value["deviceKey"] == "browser-remove"
        )
        result = self.store.remove_device(device["id"])
        self.assertEqual(result["workers"], [("user-1", "worker-remove")])
        self.assertEqual(
            self.db.read_execute(
                "SELECT id FROM user_sessions WHERE device_id=?", (device["id"],)
            ),
            [],
        )
        self.assertEqual(
            self.db.read_execute(
                "SELECT id FROM playback_viewer_sessions WHERE id=? AND state='ended'",
                (active_viewer,),
            )[0][0],
            active_viewer,
        )


if __name__ == "__main__":
    unittest.main()
