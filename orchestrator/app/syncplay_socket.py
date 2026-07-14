from collections import defaultdict
from threading import RLock

from flask import current_app, request
import time
from flask_socketio import SocketIO, emit

from app.models.syncplay import SyncplayGroup
from jellyfin.api_service import authenticated_user_id


socketio = SocketIO(
    async_mode="eventlet", cors_allowed_origins="*", path="api/socket.io"
)

# A member may have more than one tab connected. Only treat them as gone after
# their final authenticated Syncplay socket has remained disconnected long
# enough for Socket.IO to reconnect through a temporary network interruption.
DISCONNECT_GRACE_SECONDS = 30
HOST_DISCONNECT_GRACE_SECONDS = 300
# A route transition can briefly replace the host's Socket.IO connection while
# the player page is loading. Do not pause/broadcast a host disconnect for that
# transient gap; the normal reconnect grace still applies afterward.
HOST_DISCONNECT_MARK_DELAY_SECONDS = 30
_connections_lock = RLock()
_participant_sids = defaultdict(set)
_sid_users = {}
_disconnect_epochs = defaultdict(int)


def groups():
    for state in SyncplayGroup.expire_due_host_disconnects():
        broadcast_group_ended(state["id"], state["revision"])
    ids = SyncplayGroup("_").db.execute(
        "SELECT id FROM syncplay_groups WHERE ended=0 ORDER BY updated DESC", ()
    )
    return [SyncplayGroup(row[0]).state() for row in ids]


@socketio.on("connect", namespace="/syncplay")
def connect(auth):
    token = (auth or {}).get("token")
    participant_id = (auth or {}).get("participantId")
    user_id = authenticated_user_id(token) if token else None
    if not user_id or not isinstance(participant_id, str) or not participant_id:
        current_app.logger.warning("Rejected Syncplay Socket.IO connection")
        return False
    with _connections_lock:
        key = (user_id, participant_id)
        _sid_users[request.sid] = key
        _participant_sids[key].add(request.sid)
        _disconnect_epochs[key] += 1
    current_app.logger.info("Connected Syncplay Socket.IO client for %s", user_id)
    for group in SyncplayGroup.active_groups_for_user(user_id, participant_id):
        state = group.clear_host_disconnected()
        if state:
            broadcast_group(state)
    emit("syncplay:groups", {"groups": groups()}, to=request.sid)


@socketio.on("disconnect", namespace="/syncplay")
def disconnect():
    with _connections_lock:
        key = _sid_users.pop(request.sid, None)
        if not key:
            return
        user_id, participant_id = key
        _participant_sids[key].discard(request.sid)
        if _participant_sids[key]:
            return
        _participant_sids.pop(key, None)
        _disconnect_epochs[key] += 1
        epoch = _disconnect_epochs[key]
    socketio.start_background_task(
        mark_disconnected_host,
        user_id,
        participant_id,
        epoch,
    )
    socketio.start_background_task(expire_disconnected_user, user_id, participant_id, epoch)
    socketio.start_background_task(expire_disconnected_host, user_id, participant_id, epoch)


@socketio.on("syncplay:clock", namespace="/syncplay")
def clock_probe(message):
    """Return server timestamps for an NTP-style client clock estimate."""
    received = time.time()
    return {"clientSentAt": (message or {}).get("clientSentAt"), "serverReceivedAt": received, "serverSentAt": time.time()}


def expire_disconnected_user(user_id, participant_id, epoch):
    socketio.sleep(DISCONNECT_GRACE_SECONDS)
    with _connections_lock:
        if _participant_sids.get((user_id, participant_id)) or _disconnect_epochs[(user_id, participant_id)] != epoch:
            return
    ids = SyncplayGroup("_").db.execute(
        "SELECT group_id FROM syncplay_members WHERE user_id=? AND participant_id=?", (user_id, participant_id)
    )
    for row in ids:
        state = SyncplayGroup(row[0]).remove_disconnected_member(user_id, participant_id)
        if not state:
            continue
        if state["ended"]:
            broadcast_group_ended(state["id"], state["revision"])
        else:
            broadcast_group(state)


def expire_disconnected_host(user_id, participant_id, epoch):
    socketio.sleep(HOST_DISCONNECT_GRACE_SECONDS)
    with _connections_lock:
        if _participant_sids.get((user_id, participant_id)) or _disconnect_epochs[(user_id, participant_id)] != epoch:
            return
    for group in SyncplayGroup.active_groups_for_user(user_id, participant_id):
        state = group.expire_host_disconnect()
        if state:
            broadcast_group_ended(state["id"], state["revision"])


def mark_disconnected_host(user_id, participant_id, epoch):
    """Delay host pause broadcasts until a route-transition reconnect settles."""
    socketio.sleep(HOST_DISCONNECT_MARK_DELAY_SECONDS)
    with _connections_lock:
        if _participant_sids.get((user_id, participant_id)) or _disconnect_epochs[(user_id, participant_id)] != epoch:
            return
    for group in SyncplayGroup.active_groups_for_user(user_id, participant_id):
        state = group.state()
        if state and state["hostUserId"] == user_id:
            marked = group.mark_host_disconnected()
            if marked:
                broadcast_group(marked)


def broadcast_group(group):
    socketio.emit("syncplay:group", {"group": group}, namespace="/syncplay")


def broadcast_group_ended(group_id, revision):
    current_app.logger.info("Broadcasting Syncplay group end for %s", group_id)
    socketio.emit(
        "syncplay:group-ended", {"id": group_id, "revision": revision}, namespace="/syncplay"
    )


def notify_participant_replaced(group_id, participant_id, revision):
    """Tell only the displaced tab to leave its player immediately."""
    with _connections_lock:
        sids = tuple(_participant_sids.get((None, participant_id), ()))
        if not sids:
            sids = tuple(
                sid for (user_id, current_participant), connected in _participant_sids.items()
                if current_participant == participant_id for sid in connected
            )
    for sid in sids:
        socketio.emit(
            "syncplay:participant-replaced",
            {"id": group_id, "revision": revision},
            namespace="/syncplay",
            to=sid,
        )
