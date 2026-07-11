import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from app.syncplay_socket import broadcast_group, socketio


class SyncplaySocketTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SECRET_KEY"] = "test"
        socketio.init_app(self.app)

    @patch("app.syncplay_socket.groups", return_value=[])
    @patch("app.syncplay_socket.authenticated_user_id", return_value="user")
    def test_authenticated_connection_receives_group_snapshot(self, _, __):
        client = socketio.test_client(
            self.app, namespace="/syncplay", auth={"token": "valid"}
        )

        self.assertTrue(client.is_connected("/syncplay"))
        self.assertEqual(
            client.get_received("/syncplay"),
            [
                {
                    "name": "syncplay:groups",
                    "args": [{"groups": []}],
                    "namespace": "/syncplay",
                }
            ],
        )

    @patch("app.syncplay_socket.authenticated_user_id", return_value=None)
    def test_invalid_token_is_rejected(self, _):
        client = socketio.test_client(
            self.app, namespace="/syncplay", auth={"token": "invalid"}
        )

        self.assertFalse(client.is_connected("/syncplay"))

    @patch("app.syncplay_socket.groups", return_value=[])
    @patch("app.syncplay_socket.authenticated_user_id", return_value="user")
    def test_group_updates_are_broadcast_to_connected_clients(self, _, __):
        first = socketio.test_client(
            self.app, namespace="/syncplay", auth={"token": "first"}
        )
        second = socketio.test_client(
            self.app, namespace="/syncplay", auth={"token": "second"}
        )
        first.get_received("/syncplay")
        second.get_received("/syncplay")

        with self.app.app_context():
            broadcast_group({"id": "group"})

        expected = [
            {
                "name": "syncplay:group",
                "args": [{"group": {"id": "group"}}],
                "namespace": "/syncplay",
            }
        ]
        self.assertEqual(first.get_received("/syncplay"), expected)
        self.assertEqual(second.get_received("/syncplay"), expected)
