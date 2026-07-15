import math
import time

from flask import request
from flask_restx import Resource

from app.models.syncplay import (
    StaleSyncplayState,
    SyncplayMembershipConflict,
    SyncplayGroup,
    pause,
    projected_position,
    schedule,
)
from app.syncplay_socket import broadcast_group, broadcast_group_ended, notify_participant_replaced
from jellyfin.api_service import authenticated_user_id
from . import api_namespace_zs


def identity():
    token = request.headers.get("X-Jellyfin-Token")
    user = authenticated_user_id(token) if token else None
    participant = request.headers.get("X-ZenStream-Participant", "")
    return (user, participant) if user and participant else (None, None)


def name(): return request.headers.get("X-ZenStream-Username", "ZenStream")
def body(): return request.get_json(silent=True) or {}
def operation(data): return data.get("operationId") if isinstance(data.get("operationId"), str) else None
def expected(data): return data.get("expectedRevision") if isinstance(data.get("expectedRevision"), int) else None


def group_for(group_id):
    user, participant = identity(); group = SyncplayGroup(group_id)
    if not user: return None, None, ({"message": "Authentication required."}, 401)
    if not group.state(): return None, None, ({"message": "Group not found."}, 404)
    if not group.member(user, participant): return None, None, ({"message": "Join this group first."}, 403)
    return (user, participant), group, None


def stale(error): return {"message": "Playback state is out of date.", "group": error.state}, 409


@api_namespace_zs.route("zenstream/syncplay/groups")
class Groups(Resource):
    def get(self):
        if not identity()[0]: return {"message": "Authentication required."}, 401
        for state in SyncplayGroup.expire_due_host_disconnects(): broadcast_group_ended(state["id"], state["revision"])
        ids = SyncplayGroup("_").db.execute("SELECT id FROM syncplay_groups WHERE ended=0 ORDER BY updated DESC", ())
        return {"groups": [SyncplayGroup(x[0]).state() for x in ids]}, 200

    def post(self):
        user, participant = identity()
        if not user: return {"message": "Authentication required."}, 401
        try:
            state = SyncplayGroup.create(user, participant, name()).state()
        except SyncplayMembershipConflict:
            return {"message": "You already belong to an active Syncplay group."}, 409
        broadcast_group(state)
        return state, 201


@api_namespace_zs.route("zenstream/syncplay/groups/<string:group_id>/join")
class Join(Resource):
    def post(self, group_id):
        user, participant = identity(); group = SyncplayGroup(group_id); data = body()
        if not user: return {"message": "Authentication required."}, 401
        if not group.state(): return {"message": "Group not found."}, 404
        replaced_participant = None
        try:
            def apply(cursor, state):
                nonlocal replaced_participant
                cursor.execute("SELECT 1 FROM syncplay_members m JOIN syncplay_groups g ON g.id=m.group_id WHERE m.user_id=? AND g.ended=0 AND m.group_id<>? LIMIT 1", (user, group_id))
                if cursor.fetchone(): raise SyncplayMembershipConflict
                cursor.execute("SELECT 1 FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id=?", (group_id, user, participant))
                if cursor.fetchone(): return
                cursor.execute("SELECT participant_id FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id<>?", (group_id, user, participant))
                replaced_participant = cursor.fetchone()
                cursor.execute("DELETE FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id<>?", (group_id, user, participant))
                cursor.execute("INSERT INTO syncplay_members (group_id,user_id,participant_id,username) VALUES (?,?,?,?)", (group_id, user, participant, name()))
                group.transition(cursor, state)
            state = group.mutate(user, expected(data), operation(data), apply)
        except SyncplayMembershipConflict: return {"message": "You must leave your current Syncplay group before joining another."}, 409
        except StaleSyncplayState as error: return stale(error)
        if replaced_participant:
            notify_participant_replaced(group_id, replaced_participant[0], state["revision"])
        broadcast_group(state); return state, 200


@api_namespace_zs.route("zenstream/syncplay/groups/<string:group_id>")
class Group(Resource):
    def get(self, group_id):
        _, group, error = group_for(group_id)
        return error or (group.state(), 200)

    def delete(self, group_id):
        identity_value, group, error = group_for(group_id)
        if error: return error
        user, participant = identity_value
        data = body()
        try:
            def apply(cursor, state):
                if state["hostUserId"] == user:
                    group.transition(cursor, state, timeline=True, ended=1, playing=0, resume=0, playback_state="paused", effective_at=0, host_disconnected_at=None)
                else:
                    cursor.execute("DELETE FROM syncplay_members WHERE group_id=? AND participant_id=?", (group_id, participant))
                    group.transition(cursor, state)
            state = group.mutate(user, expected(data), operation(data), apply)
        except StaleSyncplayState as error: return stale(error)
        if state["ended"]: broadcast_group_ended(group_id, state["revision"])
        else: broadcast_group(state)
        return "", 204

    def patch(self, group_id):
        identity_value, group, error = group_for(group_id)
        if error: return error
        user, participant = identity_value
        data = body(); value = data.get("allowViewerControls")
        if not isinstance(value, bool): return {"message": "allowViewerControls must be boolean."}, 400
        try:
            def apply(cursor, state):
                if state["hostUserId"] != user: raise PermissionError
                group.transition(cursor, state, allow_controls=int(value))
            state = group.mutate(user, expected(data), operation(data), apply)
        except PermissionError: return {"message": "Only the host can change settings."}, 403
        except StaleSyncplayState as error: return stale(error)
        broadcast_group(state); return state, 200


@api_namespace_zs.route("zenstream/syncplay/groups/<string:group_id>/members/<string:member_id>")
class Member(Resource):
    def delete(self, group_id, member_id):
        identity_value, group, error = group_for(group_id)
        if error: return error
        user, participant = identity_value
        data = body()
        try:
            def apply(cursor, state):
                if state["hostUserId"] != user: raise PermissionError
                if member_id == user: raise ValueError
                cursor.execute("DELETE FROM syncplay_members WHERE group_id=? AND user_id=?", (group_id, member_id))
                generation = state["mediaGeneration"]
                group.reconcile_readiness(cursor, state)
            state = group.mutate(user, expected(data), operation(data), apply)
        except PermissionError: return {"message": "Only the host can remove members."}, 403
        except ValueError: return {"message": "The host cannot remove themselves."}, 400
        except StaleSyncplayState as error: return stale(error)
        broadcast_group(state); return state, 200


@api_namespace_zs.route("zenstream/syncplay/groups/<string:group_id>/command")
class Command(Resource):
    def post(self, group_id):
        identity_value, group, error = group_for(group_id)
        if error: return error
        user, participant = identity_value
        data = body(); pos = data.get("position"); action = data.get("action")
        if not isinstance(pos, (int, float)) or not math.isfinite(pos) or pos < 0: return {"message": "Invalid playback position."}, 400
        if action not in {"media", "play", "pause", "seek"}: return {"message": "Invalid playback command."}, 400
        try:
            def apply(cursor, state):
                if user != state["hostUserId"] and not state["allowViewerControls"]: raise PermissionError
                item = data.get("itemId", state["itemId"])
                if action == "media":
                    if not isinstance(item, str): raise ValueError
                    generation = state["mediaGeneration"] + 1
                    cursor.execute("UPDATE syncplay_members SET watching_together=1 WHERE group_id=? AND participant_id=?", (group_id, participant))
                    cursor.execute("UPDATE syncplay_members SET viewing=0,loading=CASE WHEN watching_together=1 THEN 1 ELSE 0 END,ready_generation=-1,presence_sequence=0 WHERE group_id=?", (group_id,))
                    group.transition(cursor, state, timeline=True, item_id=item, position=float(pos), playing=0, resume=1, media_generation=generation, anchor_position=float(pos), anchor_time=time.time(), effective_at=0, playback_state="paused", pause_reason="readiness")
                    return
                requested = bool(data.get("playing", state["playing"]))
                # A seek made while media is loading must keep the pending start.
                # The local player is paused during readiness, so its raw `playing`
                # value alone would otherwise cancel the group release.
                if action == "seek" and state["resumeWhenReady"]:
                    requested = True
                elif action == "play":
                    requested = True
                elif action == "pause":
                    requested = False
                waiting = group.waiting_for_members(cursor, state["mediaGeneration"])
                if requested and not waiting: schedule(group, cursor, state, float(pos))
                elif requested: group.transition(cursor, state, timeline=True, item_id=item, position=float(pos), playing=0, resume=1, anchor_position=float(pos), anchor_time=time.time(), effective_at=0, playback_state="paused", pause_reason="readiness")
                else: pause(group, cursor, state, "command")
            state = group.mutate(user, expected(data), operation(data), apply)
        except PermissionError: return {"message": "Only the host can control playback."}, 403
        except ValueError: return {"message": "A media item is required."}, 400
        except StaleSyncplayState as error: return stale(error)
        broadcast_group(state); return state, 200


@api_namespace_zs.route("zenstream/syncplay/groups/<string:group_id>/presence")
class Presence(Resource):
    def post(self, group_id):
        identity_value, group, error = group_for(group_id)
        if error: return error
        user, participant = identity_value
        data = body(); generation = data.get("mediaGeneration"); sequence = data.get("presenceSequence")
        if not isinstance(generation, int) or not isinstance(sequence, int): return {"message": "mediaGeneration and presenceSequence are required."}, 400
        # Presence is intentionally an idempotent no-op when a delayed browser callback
        # belongs to a previous item or has already been superseded.
        def apply(cursor, state):
            if generation != state["mediaGeneration"]: return
            cursor.execute("SELECT presence_sequence,watching_together FROM syncplay_members WHERE group_id=? AND participant_id=?", (group_id, participant))
            row = cursor.fetchone()
            if not row or sequence <= row[0] or not row[1]: return
            viewing = bool(data.get("viewing")); loading = bool(data.get("loading")) if viewing else False
            cursor.execute("UPDATE syncplay_members SET viewing=?,loading=?,ready_generation=?,presence_sequence=? WHERE group_id=? AND participant_id=?", (int(viewing), int(loading), generation if viewing and not loading else -1, sequence, group_id, participant))
            group.reconcile_readiness(cursor, state)
        # Presence must be allowed to land after a state update; the media generation and
        # sequence are its concurrency controls, so do not reject it by group revision.
        state = group.mutate(user, None, operation(data), apply)
        broadcast_group(state); return state, 200


@api_namespace_zs.route("zenstream/syncplay/groups/<string:group_id>/participation")
class Participation(Resource):
    def post(self, group_id):
        identity_value, group, error = group_for(group_id)
        if error: return error
        user, participant = identity_value
        data = body(); watching = data.get("watchingTogether")
        if not isinstance(watching, bool): return {"message": "watchingTogether must be boolean."}, 400
        try:
            def apply(cursor, state):
                loading = int(watching and state["itemId"] is not None and state["resumeWhenReady"])
                cursor.execute(
                    "UPDATE syncplay_members SET watching_together=?,viewing=0,loading=?,ready_generation=-1,presence_sequence=0 WHERE group_id=? AND participant_id=?",
                    (int(watching), loading, group_id, participant),
                )
                group.reconcile_readiness(cursor, state)
            state = group.mutate(user, None, operation(data), apply)
        except StaleSyncplayState as error:
            return stale(error)
        broadcast_group(state); return state, 200
