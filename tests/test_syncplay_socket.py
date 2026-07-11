import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from app.syncplay_socket import socketio


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
            [{"name": "syncplay:groups", "args": [{"groups": []}], "namespace": "/syncplay"}],
        )

    @patch("app.syncplay_socket.authenticated_user_id", return_value=None)
    def test_invalid_token_is_rejected(self, _):
        client = socketio.test_client(
            self.app, namespace="/syncplay", auth={"token": "invalid"}
        )

        self.assertFalse(client.is_connected("/syncplay"))
