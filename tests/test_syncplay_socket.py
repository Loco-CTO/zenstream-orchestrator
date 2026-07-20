import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import app.syncplay_socket as syncplay_socket
from app.syncplay_socket import broadcast_group, socketio


class SyncplaySocketTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SECRET_KEY"] = "test"
        socketio.init_app(self.app)
        syncplay_socket._user_sids.clear()
        syncplay_socket._sid_users.clear()
        syncplay_socket._disconnect_epochs.clear()

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

    @patch("app.syncplay_socket.socketio.start_background_task")
    @patch("app.syncplay_socket.groups", return_value=[])
    @patch("app.syncplay_socket.authenticated_user_id", return_value="viewer")
    def test_last_socket_disconnect_starts_a_grace_period(self, _, __, start_task):
        client = socketio.test_client(
            self.app, namespace="/syncplay", auth={"token": "viewer"}
        )
        client.disconnect(namespace="/syncplay")

        self.assertTrue(start_task.called)
        callback, user_id, epoch = start_task.call_args.args
        self.assertIs(callback, syncplay_socket.expire_disconnected_user)
        self.assertEqual(user_id, "viewer")
        self.assertEqual(epoch, syncplay_socket._disconnect_epochs["viewer"])

    @patch("app.syncplay_socket.broadcast_group")
    @patch("app.syncplay_socket.socketio.sleep")
    @patch("app.syncplay_socket.SyncplayGroup")
    def test_expired_viewer_connection_removes_the_member_and_broadcasts(
        self, group_factory, sleep, broadcast
    ):
        lookup = group_factory.return_value
        lookup.db.execute.return_value = [("group",)]
        group = group_factory.return_value
        group.remove_disconnected_member.return_value = {
            "id": "group",
            "revision": 7,
            "ended": False,
        }
        syncplay_socket._disconnect_epochs["viewer"] = 4

        syncplay_socket.expire_disconnected_user("viewer", 4)

        sleep.assert_called_once_with(syncplay_socket.DISCONNECT_GRACE_SECONDS)
        group.remove_disconnected_member.assert_called_once_with("viewer")
        broadcast.assert_called_once_with(
            {"id": "group", "revision": 7, "ended": False}
        )
