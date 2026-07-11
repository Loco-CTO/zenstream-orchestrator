from flask import request
from flask_socketio import SocketIO, emit

from app.models.syncplay import SyncplayGroup
from jellyfin.api_service import authenticated_user_id


socketio = SocketIO(async_mode="threading", cors_allowed_origins="*")


def groups():
    ids = SyncplayGroup("_").db.execute(
        "SELECT id FROM syncplay_groups WHERE ended=0 ORDER BY updated DESC", ()
    )
    return [SyncplayGroup(row[0]).state() for row in ids]


@socketio.on("connect", namespace="/syncplay")
def connect(auth):
    token = (auth or {}).get("token")
    if not token or not authenticated_user_id(token):
        return False
    emit("syncplay:groups", {"groups": groups()}, to=request.sid)


def broadcast_group(group):
    socketio.emit("syncplay:group", {"group": group}, namespace="/syncplay")


def broadcast_group_ended(group_id):
    socketio.emit(
        "syncplay:group-ended", {"id": group_id}, namespace="/syncplay"
    )
