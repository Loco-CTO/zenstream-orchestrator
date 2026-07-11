from flask import current_app, request
from flask_socketio import SocketIO, emit

from app.models.syncplay import SyncplayGroup
from jellyfin.api_service import authenticated_user_id


socketio = SocketIO(
    async_mode="eventlet", cors_allowed_origins="*", path="api/socket.io"
)


def groups():
    ids = SyncplayGroup("_").db.execute(
        "SELECT id FROM syncplay_groups WHERE ended=0 ORDER BY updated DESC", ()
    )
    return [SyncplayGroup(row[0]).state() for row in ids]


@socketio.on("connect", namespace="/syncplay")
def connect(auth):
    token = (auth or {}).get("token")
    user_id = authenticated_user_id(token) if token else None
    if not user_id:
        current_app.logger.warning("Rejected Syncplay Socket.IO connection")
        return False
    current_app.logger.info("Connected Syncplay Socket.IO client for %s", user_id)
    emit("syncplay:groups", {"groups": groups()}, to=request.sid)


def broadcast_group(group):
    current_app.logger.info("Broadcasting Syncplay group update for %s", group["id"])
    socketio.emit("syncplay:group", {"group": group}, namespace="/syncplay")


def broadcast_group_ended(group_id, revision):
    current_app.logger.info("Broadcasting Syncplay group end for %s", group_id)
    socketio.emit(
        "syncplay:group-ended", {"id": group_id, "revision": revision}, namespace="/syncplay"
    )
