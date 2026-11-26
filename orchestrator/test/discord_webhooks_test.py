import importlib
import json
import sqlite3
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from alembic.migration import MigrationContext
from alembic.operations import Operations
from api.zenstream.webhook_routes import router
from app import discord_webhooks as webhooks
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine


class Database:
    def __init__(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}
        )
        with self.engine.begin() as connection:
            migration = importlib.import_module(
                "migrations.versions.0070_discord_webhooks"
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        self.connection = self.engine.raw_connection()
        with self.transaction() as cursor:
            cursor.execute(
                "CREATE TABLE account_preferences(user_id TEXT, watch_history_enabled INT)"
            )

    def read_execute(self, sql, args=()):
        return self.connection.execute(sql, args).fetchall()

    @contextmanager
    def transaction(self):
        with self.connection.driver_connection:
            yield self.connection.cursor()

    def close(self):
        self.connection.close()
        self.engine.dispose()


class DiscordWebhookTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.close)
        env = patch.dict("os.environ", {"SECRET_KEY": "discord-webhook-test-secret"})
        env.start()
        self.addCleanup(env.stop)

    def create(self, events):
        settings = webhooks.save_webhook(
            self.db,
            None,
            SimpleNamespace(
                name="Living room",
                url="https://discord.com/api/webhooks/123/" + "a" * 32,
                events=events,
            ),
        )
        return settings["webhooks"][0]

    def test_subscriptions_and_milestones(self):
        hook = self.create([])
        webhooks.publish("user.login", db=self.db, user="user")
        self.assertEqual(webhooks.settings(self.db)["webhooks"][0]["events"], [])
        self.assertIsNone(webhooks._claim(self.db))
        self.assertNotIn("url", hook)
        ciphertext = self.db.read_execute(
            "SELECT url_ciphertext FROM discord_webhooks"
        )[0][0]
        self.assertNotIn("https://discord", ciphertext)
        with self.assertRaises(sqlite3.IntegrityError):
            self.create([])
        webhooks.save_webhook(
            self.db,
            hook["id"],
            SimpleNamespace(name="Activity", url=None, events=list(webhooks.EVENTS)),
        )
        with self.db.transaction() as cursor:
            for position in (1, 25, 25, 10, 30, 54, 77, 99, 100):
                webhooks.record_viewer_sample(
                    cursor, "viewer", "user", "episode", position, 100, False, "direct"
                )
        rows = self.db.read_execute(
            "SELECT kind FROM discord_webhook_deliveries ORDER BY created,id"
        )
        self.assertEqual(
            [row[0] for row in rows],
            [
                "playback.started",
                "playback.progress",
                "playback.progress",
                "playback.progress",
                "playback.completed",
            ],
        )

    def test_every_route_requires_admin_cookie(self):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            for method, suffix in (
                ("GET", ""),
                ("POST", ""),
                ("PUT", "/id"),
                ("DELETE", "/id"),
                ("POST", "/id/test"),
            ):
                result = client.request(
                    method,
                    "/api/admin/webhooks" + suffix,
                    headers={"Authorization": "Bearer ordinary-client-token"},
                )
                self.assertEqual(result.status_code, 401)

    def test_event_color_and_episode_metadata(self):
        with self.db.transaction() as cursor:
            cursor.executescript(
                "CREATE TABLE library_entities(id TEXT,entity_type TEXT,parent_id TEXT,relative_path TEXT,season_number INT,episode_number INT);"
                "CREATE TABLE catalog_metadata_projection(entity_id TEXT,locale TEXT,payload TEXT);"
                "CREATE TABLE catalog_artwork_selection(entity_id TEXT,image_type TEXT,locale TEXT,local_path TEXT);"
                "INSERT INTO library_entities VALUES('episode','episode','series','Episode.mkv',2,3);"
                "INSERT INTO library_entities VALUES('series','series',NULL,'Series',NULL,NULL);"
            )
        message, _ = webhooks.build_message(
            self.db,
            "playback.progress",
            {"entity": "episode", "milestone": 25, "position": 10, "duration": 40},
            0,
        )
        embed = message["embeds"][0]
        self.assertIn("Episode", embed["description"])
        self.assertEqual(embed["color"], 0xD1D5DB)
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(
            fields,
            {
                "Series": "Series",
                "Season": "2",
                "Episode": "3",
                "Milestone": "25%",
                "Position": "0:10 / 0:40",
            },
        )

    async def test_separate_embeds_and_rate_limit(self):
        hook = self.create(["user.login", "user.logout"])
        webhooks.publish("user.login", db=self.db, client="Web")
        webhooks.publish("user.logout", db=self.db)
        sent = []

        def respond(request):
            sent.append(json.loads(request.content))
            if len(sent) == 1:
                return httpx.Response(429, json={"retry_after": 30})
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            row = webhooks._claim(self.db)
            await webhooks._deliver(self.db, client, row)
            self.assertIsNone(webhooks._claim(self.db))
            with self.db.transaction() as cursor:
                cursor.execute("UPDATE discord_webhooks SET send_after=0")
                cursor.execute("UPDATE discord_webhook_deliveries SET due=0")
            await webhooks._deliver(self.db, client, webhooks._claim(self.db))
            with self.db.transaction() as cursor:
                cursor.execute("UPDATE discord_webhooks SET send_after=0")
            await webhooks._deliver(self.db, client, webhooks._claim(self.db))

        self.assertTrue(all(len(message["embeds"]) == 1 for message in sent))
        self.assertEqual(sent[0], sent[1])
        self.assertNotEqual(
            sent[1]["embeds"][0]["title"], sent[2]["embeds"][0]["title"]
        )
        self.assertEqual(webhooks.settings(self.db)["webhooks"][0]["id"], hook["id"])
