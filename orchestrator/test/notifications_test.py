import json
import unittest

from app.database import DatabaseHandler
from app.notifications import FollowService, NotificationService


class FollowAndNotificationTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        statements = [
            "CREATE TABLE users(id TEXT PRIMARY KEY)",
            "CREATE TABLE libraries(id TEXT PRIMARY KEY)",
            "CREATE TABLE user_library_access(user_id TEXT,library_id TEXT)",
            "CREATE TABLE account_preferences(user_id TEXT PRIMARY KEY,locale TEXT NOT NULL DEFAULT 'en',metadata_language TEXT)",
            "CREATE TABLE metadata_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)",
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,relative_path TEXT,season_number INTEGER,episode_number INTEGER)",
            "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,identifier_type TEXT,provider_id TEXT,is_primary INTEGER)",
            "CREATE TABLE media_files(entity_id TEXT,role TEXT)",
            "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,payload TEXT)",
            "CREATE TABLE user_follow_targets(id TEXT PRIMARY KEY,user_id TEXT,library_id TEXT,target_type TEXT,provider TEXT,provider_id TEXT,entity_id TEXT,created_at TEXT,updated_at TEXT,UNIQUE(user_id,library_id,target_type,provider,provider_id))",
            "CREATE TABLE catalog_admissions(entity_id TEXT PRIMARY KEY,library_id TEXT,entity_type TEXT,admitted_at TEXT)",
            "CREATE TABLE notifications(id TEXT PRIMARY KEY,user_id TEXT,kind TEXT,entity_id TEXT,series_id TEXT,artist_id TEXT,title TEXT,subtitle TEXT,season_number INTEGER,episode_number INTEGER,navigation_path TEXT,dedupe_key TEXT,created_at TEXT,read_at TEXT,UNIQUE(user_id,dedupe_key))",
            "CREATE TABLE music_artist_credits(track_id TEXT,artist_id TEXT,credit_order INTEGER,credited_name TEXT,PRIMARY KEY(track_id,artist_id))",
            "CREATE TABLE calendar_events(id TEXT PRIMARY KEY,library_id TEXT,kind TEXT,tvdb_id TEXT,tmdb_id TEXT,series_tvdb_id TEXT)",
            "CREATE TABLE calendar_event_entities(event_id TEXT,entity_id TEXT)",
        ]
        for statement in statements:
            self.db.execute(statement)
        self.db.execute("INSERT INTO users VALUES('user')")
        self.db.execute("INSERT INTO libraries VALUES('library')")
        self.db.execute("INSERT INTO user_library_access VALUES('user','library')")

    def tearDown(self):
        self.db.close()

    def seed_series_episode(self):
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?)",
            ("series", "library", None, "series", "Series", None, None),
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?)",
            ("episode", "library", "series", "episode", "S01E01.mkv", 1, 1),
        )
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
            ("series", "tvdb", "series", "series-tvdb", 1),
        )
        self.db.execute("INSERT INTO media_files VALUES('episode','media')")
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            ("series", "en", json.dumps({"title": "Example Series"})),
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            ("episode", "en", json.dumps({"title": "Pilot"})),
        )

    def test_notifications_use_each_users_metadata_locale(self):
        self.seed_series_episode()
        self.db.execute(
            "INSERT INTO metadata_settings(key,value,updated_at) VALUES(?,?,?)",
            ("locales", json.dumps(["en", "ja"]), "now"),
        )
        self.db.execute(
            "INSERT INTO account_preferences(user_id,locale,metadata_language) VALUES(?,?,?)",
            ("user", "ja", "ja"),
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            ("series", "ja", json.dumps({"title": "例のシリーズ"})),
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            ("episode", "ja", json.dumps({"title": "パイロット"})),
        )

        follow = FollowService(self.db)
        self.assertTrue(follow.set_for_entity("user", "series", True))
        notifications = NotificationService(self.db)
        self.assertEqual(notifications.record_admissions({"episode"}), 1)
        item = notifications.list("user")["items"][0]
        self.assertEqual(item["title"], "新しいエピソード: 例のシリーズ")
        self.assertEqual(item["subtitle"], "S01E01 — パイロット")

        # The display snapshot is re-resolved when read, so changing the
        # preference also fixes notifications that were created earlier.
        self.db.execute(
            "UPDATE account_preferences SET locale=?,metadata_language=? WHERE user_id=?",
            ("en", "ja", "user"),
        )
        item = notifications.list("user")["items"][0]
        self.assertEqual(item["title"], "New episode: 例のシリーズ")
        self.assertEqual(item["subtitle"], "S01E01 — パイロット")

    def test_notifications_include_the_selected_primary_thumbnail(self):
        self.seed_series_episode()
        self.db.execute(
            "UPDATE catalog_item_projection SET payload=? WHERE entity_id=? AND locale=?",
            (
                json.dumps(
                    {
                        "title": "Pilot",
                        "images": {
                            "Primary": {
                                "url": "/api/catalog/items/episode/images/Primary?language=en",
                                "blurHash": "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
                            }
                        },
                    }
                ),
                "episode",
                "en",
            ),
        )
        self.assertTrue(FollowService(self.db).set_for_entity("user", "series", True))

        self.assertEqual(NotificationService(self.db).record_admissions({"episode"}), 1)
        item = NotificationService(self.db).list("user")["items"][0]
        self.assertEqual(
            item["thumbnail"],
            {
                "url": "/api/catalog/items/episode/images/Primary?language=en",
                "blurHash": "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
            },
        )

    def test_episode_follow_resolves_to_series_and_notifications_dedupe(self):
        self.seed_series_episode()
        follow = FollowService(self.db)

        self.assertTrue(follow.set_for_entity("user", "episode", True))
        target = self.db.execute(
            "SELECT target_type,provider,provider_id,entity_id FROM user_follow_targets"
        )
        self.assertEqual(target, [("series", "tvdb", "series-tvdb", "series")])
        self.assertTrue(follow.following_for_entity("user", "episode"))

        notifications = NotificationService(self.db)
        self.assertEqual(notifications.record_admissions({"episode"}), 1)
        self.assertEqual(notifications.record_admissions({"episode"}), 0)
        page = notifications.list("user")
        self.assertEqual(page["unreadCount"], 1)
        self.assertEqual(page["items"][0]["kind"], "new_episode")

        notification_id = page["items"][0]["id"]
        self.assertEqual(
            notifications.mark_read("user", notification_id, True)["readAt"]
            is not None,
            True,
        )
        self.assertEqual(notifications.summary("user")["unreadCount"], 0)
        notifications.mark_read("user", notification_id, False)
        self.assertEqual(notifications.mark_all_read("user")["unreadCount"], 0)
        self.assertFalse(follow.set_for_entity("user", "episode", False))
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM user_follow_targets")[0][0],
            0,
        )
        self.assertEqual(len(notifications.list("user")["items"]), 1)

    def test_delete_notification_removes_record(self):
        self.seed_series_episode()
        self.assertTrue(FollowService(self.db).set_for_entity("user", "series", True))
        notifications = NotificationService(self.db)
        self.assertEqual(notifications.record_admissions({"episode"}), 1)
        notification_id = notifications.list("user")["items"][0]["id"]

        self.assertEqual(
            notifications.delete_notification("user", notification_id),
            {"id": notification_id, "removed": True},
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM notifications WHERE id=?", (notification_id,)
            )[0][0],
            0,
        )
        self.assertEqual(notifications.list("user")["items"], [])

    def test_future_movie_calendar_follow_uses_tmdb_identity(self):
        self.db.execute(
            "INSERT INTO calendar_events VALUES(?,?,?,?,?,?)",
            ("movie-event", "library", "movie", None, "movie-tmdb", None),
        )
        follow = FollowService(self.db)
        self.assertTrue(follow.set_for_calendar_event("user", "movie-event", True))
        self.assertEqual(
            self.db.execute(
                "SELECT target_type,provider,provider_id,entity_id FROM user_follow_targets"
            ),
            [("movie", "tmdb", "movie-tmdb", None)],
        )

    def test_future_series_follow_merges_to_admitted_series(self):
        self.db.execute(
            "INSERT INTO calendar_events VALUES(?,?,?,?,?,?)",
            (
                "episode-event",
                "library",
                "episode",
                "episode-tvdb",
                None,
                "series-tvdb",
            ),
        )
        follow = FollowService(self.db)
        self.assertTrue(follow.set_for_calendar_event("user", "episode-event", True))

        self.seed_series_episode()
        self.assertEqual(NotificationService(self.db).record_admissions({"episode"}), 1)
        self.assertEqual(
            self.db.execute(
                "SELECT entity_id FROM user_follow_targets WHERE provider='tvdb' AND provider_id='series-tvdb'"
            ),
            [("series",)],
        )

    def test_artist_follow_uses_musicbrainz_identity_and_fallback_entity_identity(self):
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?)",
            ("artist", "library", None, "artist", "Artist", None, None),
        )
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
            ("artist", "musicbrainz", "artist", "artist-mb", 1),
        )
        follow = FollowService(self.db)
        self.assertTrue(follow.set_for_entity("user", "artist", True))
        self.assertEqual(
            self.db.execute(
                "SELECT target_type,provider,provider_id,entity_id FROM user_follow_targets"
            ),
            [("artist", "musicbrainz", "artist-mb", "artist")],
        )
        self.assertTrue(follow.following_for_entity("user", "artist"))

        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?)",
            ("artist-local", "library", None, "artist", "Local Artist", None, None),
        )
        self.assertTrue(follow.set_for_entity("user", "artist-local", True))
        self.assertEqual(
            self.db.execute(
                "SELECT provider,provider_id,entity_id FROM user_follow_targets "
                "WHERE entity_id='artist-local'"
            ),
            [("entity", "artist-local", "artist-local")],
        )

    def test_artist_release_notifications_group_tracks_and_match_credits(self):
        entities = (
            ("artist-main", "Main Artist", None, "artist"),
            ("artist-feature", "Feature Artist", None, "artist"),
            ("artist-other", "Other Artist", None, "artist"),
            ("release-owned", "Owned Release", "artist-main", "release"),
            ("release-appears", "Appears Release", "artist-other", "release"),
            ("track-1", "Owned/01.mp3", "release-owned", "track"),
            ("track-2", "Owned/02.mp3", "release-owned", "track"),
            ("track-3", "Appears/01.mp3", "release-appears", "track"),
        )
        for entity_id, path, parent_id, entity_type in entities:
            self.db.execute(
                "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?)",
                (entity_id, "library", parent_id, entity_type, path, None, None),
            )
        for track_id in ("track-1", "track-2", "track-3"):
            self.db.execute("INSERT INTO media_files VALUES(?,?)", (track_id, "media"))
        for values in (
            ("track-1", "artist-main", 0, "Main Artist"),
            ("track-1", "artist-feature", 1, "Feature Artist"),
            ("track-2", "artist-main", 0, "Main Artist"),
            ("track-3", "artist-feature", 0, "Feature Artist"),
        ):
            self.db.execute("INSERT INTO music_artist_credits VALUES(?,?,?,?)", values)
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
            ("artist-main", "musicbrainz", "artist", "main-mb", 1),
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            ("release-owned", "en", json.dumps({"title": "Owned Release"})),
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            ("release-appears", "en", json.dumps({"title": "Appears Release"})),
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            ("artist-main", "en", json.dumps({"title": "Main Artist"})),
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            ("artist-feature", "en", json.dumps({"title": "Feature Artist"})),
        )
        follow = FollowService(self.db)
        self.assertTrue(follow.set_for_entity("user", "artist-main", True))
        self.assertTrue(follow.set_for_entity("user", "artist-feature", True))

        notifications = NotificationService(self.db)
        self.assertEqual(
            notifications.record_admissions({"track-1", "track-2", "track-3"}), 2
        )
        page = notifications.list("user")
        self.assertEqual(page["unreadCount"], 2)
        by_item = {item["itemId"]: item for item in page["items"]}
        self.assertEqual(
            by_item["release-owned"]["kind"],
            "new_release",
        )
        self.assertEqual(
            by_item["release-owned"]["subtitle"],
            "New release: Main Artist",
        )
        self.assertEqual(
            by_item["release-owned"]["navigationTarget"],
            "/album/release-owned",
        )
        self.assertEqual(by_item["release-owned"]["artistId"], "artist-main")

        self.assertEqual(notifications.record_admissions({"track-1", "track-2"}), 0)
        self.assertEqual(len(notifications.list("user")["items"]), 2)

        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?)",
            ("track-4", "library", "release-owned", "track", "Owned/03.mp3", None, None),
        )
        self.db.execute("INSERT INTO media_files VALUES(?,?)", ("track-4", "media"))
        self.db.execute(
            "INSERT INTO music_artist_credits VALUES(?,?,?,?)",
            ("track-4", "artist-main", 0, "Main Artist"),
        )
        self.assertEqual(notifications.record_admissions({"track-4"}), 1)
        self.assertEqual(len(notifications.list("user")["items"]), 3)
