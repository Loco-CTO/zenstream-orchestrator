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
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,relative_path TEXT,season_number INTEGER,episode_number INTEGER)",
            "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,identifier_type TEXT,provider_id TEXT,is_primary INTEGER)",
            "CREATE TABLE media_files(entity_id TEXT,role TEXT)",
            "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,payload TEXT)",
            "CREATE TABLE user_follow_targets(id TEXT PRIMARY KEY,user_id TEXT,library_id TEXT,target_type TEXT,provider TEXT,provider_id TEXT,entity_id TEXT,created_at TEXT,updated_at TEXT,UNIQUE(user_id,library_id,target_type,provider,provider_id))",
            "CREATE TABLE catalog_admissions(entity_id TEXT PRIMARY KEY,library_id TEXT,entity_type TEXT,admitted_at TEXT)",
            "CREATE TABLE notifications(id TEXT PRIMARY KEY,user_id TEXT,kind TEXT,entity_id TEXT,series_id TEXT,title TEXT,subtitle TEXT,season_number INTEGER,episode_number INTEGER,navigation_path TEXT,dedupe_key TEXT,created_at TEXT,read_at TEXT,UNIQUE(user_id,dedupe_key))",
            "CREATE TABLE notification_push_subscriptions(id TEXT PRIMARY KEY,user_id TEXT,endpoint TEXT UNIQUE,p256dh TEXT,auth TEXT,expiration_time TEXT,created_at TEXT,updated_at TEXT)",
            "CREATE TABLE notification_push_outbox(id TEXT PRIMARY KEY,notification_id TEXT,subscription_id TEXT,state TEXT,attempts INTEGER,next_attempt_at TEXT,last_error TEXT,created_at TEXT,delivered_at TEXT,UNIQUE(notification_id,subscription_id))",
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
        self.assertEqual(
            notifications.put_subscription(
                "user",
                {
                    "endpoint": "https://push.example/subscription",
                    "keys": {"p256dh": "key", "auth": "auth"},
                },
            ),
            {"registered": True},
        )
        self.assertEqual(notifications.record_admissions({"episode"}), 1)
        self.assertEqual(notifications.record_admissions({"episode"}), 0)
        page = notifications.list("user")
        self.assertEqual(page["unreadCount"], 1)
        self.assertEqual(page["items"][0]["kind"], "new_episode")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM notification_push_outbox")[0][0],
            1,
        )

        notification_id = page["items"][0]["id"]
        self.assertEqual(notifications.mark_read("user", notification_id, True)["readAt"] is not None, True)
        self.assertEqual(notifications.summary("user")["unreadCount"], 0)
        notifications.mark_read("user", notification_id, False)
        self.assertEqual(notifications.mark_all_read("user")["unreadCount"], 0)
        self.assertFalse(follow.set_for_entity("user", "episode", False))
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM user_follow_targets")[0][0],
            0,
        )
        self.assertEqual(len(notifications.list("user")["items"]), 1)

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
