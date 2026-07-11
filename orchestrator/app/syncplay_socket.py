import json
import os
import threading
from urllib.parse import parse_qs, urlparse

from websockets.sync.server import serve
from websockets.exceptions import ConnectionClosed

from app.models.syncplay import SyncplayGroup
from jellyfin.api_service import authenticated_user_id


class SyncplayWebSocketServer:
    def __init__(self):
        self.connections = set()
        self.lock = threading.Lock()
        self.started = False

    def groups(self):
        ids = SyncplayGroup("_").db.execute(
            "SELECT id FROM syncplay_groups WHERE ended=0 ORDER BY updated DESC", ()
        )
        return [SyncplayGroup(row[0]).state() for row in ids]

    def start(self):
        if self.started:
            return
        self.started = True
        thread = threading.Thread(target=self._serve, name="syncplay-websocket", daemon=True)
        thread.start()

    def _serve(self):
        host = os.getenv("ORCHESTRATOR_HOST", "127.0.0.1")
        port = int(os.getenv("ORCHESTRATOR_WEBSOCKET_PORT", "9091"))
        with serve(self._handle, host, port) as server:
            server.serve_forever()

    def _handle(self, websocket):
        query = parse_qs(urlparse(websocket.request.path).query)
        token = query.get("token", [None])[0]
        if not token or not authenticated_user_id(token):
            websocket.close(code=1008, reason="Authentication required")
            return
        with self.lock:
            self.connections.add(websocket)
        try:
            websocket.send(json.dumps({"type": "syncplay:groups", "groups": self.groups()}))
            for _ in websocket:
                # State is server-authoritative; commands continue through the
                # authenticated REST API, which broadcasts their result here.
                pass
        except ConnectionClosed:
            # The browser can navigate away while the initial snapshot is being
            # prepared; that is an expected WebSocket lifecycle event.
            pass
        finally:
            with self.lock:
                self.connections.discard(websocket)

    def broadcast(self, message):
        payload = json.dumps(message)
        with self.lock:
            connections = list(self.connections)
        for websocket in connections:
            try:
                websocket.send(payload)
            except Exception:
                with self.lock:
                    self.connections.discard(websocket)


syncplay_websocket = SyncplayWebSocketServer()


def broadcast_group(group):
    syncplay_websocket.broadcast({"type": "syncplay:group", "group": group})


def broadcast_group_ended(group_id):
    syncplay_websocket.broadcast({"type": "syncplay:group-ended", "id": group_id})
