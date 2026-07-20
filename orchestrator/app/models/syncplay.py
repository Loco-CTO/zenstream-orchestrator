import json
import time
import uuid

from app.config import Config


class StaleSyncplayState(Exception):
    def __init__(self, state): self.state = state


class SyncplayMembershipConflict(Exception):
    pass


def projected_position(state, now=None):
    now = time.time() if now is None else now
    if state["playbackState"] != "playing" or now < state["effectiveAt"]:
        return state["anchorPosition"]
    return state["anchorPosition"] + max(0, now - state["anchorServerTime"])


def schedule(group, cursor, state, position, reason=None):
    now = time.time(); effective = now + 1.0
    group.transition(cursor, state, timeline=True, position=position, playing=1, resume=0,
                     anchor_position=position, anchor_time=effective, effective_at=effective,
                     playback_state="playing", pause_reason=reason)


def pause(group, cursor, state, reason):
    now = time.time(); position = projected_position(state, now)
    group.transition(cursor, state, timeline=True, position=position, playing=0,
                     resume=1 if reason == "buffering" else 0,
                     anchor_position=position, anchor_time=now, effective_at=0,
                     playback_state="paused", pause_reason=reason)


class SyncplayGroup:
    def __init__(self, group_id): self.id, self.db = group_id, Config().database

    @classmethod
    def create(cls, user_id, participant_id, username=None):
        if username is None:
            username, participant_id = participant_id, "legacy"
        group = cls(str(uuid.uuid4())); now = time.time()
        with group.db.transaction() as cursor:
            cursor.execute("SELECT 1 FROM syncplay_members m JOIN syncplay_groups g ON g.id=m.group_id WHERE m.user_id=? AND g.ended=0 LIMIT 1", (user_id,))
            if cursor.fetchone(): raise SyncplayMembershipConflict
            cursor.execute("INSERT INTO syncplay_groups (id,host_user_id,host_name,updated) VALUES (?,?,?,?)", (group.id, user_id, username, now))
            cursor.execute("INSERT INTO syncplay_members (group_id,user_id,participant_id,username) VALUES (?,?,?,?)", (group.id, user_id, participant_id, username))
        return group

    def _state(self, cursor, include_ended=False):
        ended = "" if include_ended else " AND ended=0"
        cursor.execute("SELECT host_user_id,host_name,allow_controls,item_id,position,playing,resume,revision,timeline_revision,media_generation,anchor_position,anchor_time,effective_at,playback_state,pause_reason,host_disconnected_at,updated,ended FROM syncplay_groups WHERE id=?" + ended, (self.id,))
        r = cursor.fetchone()
        if not r: return None
        cursor.execute("SELECT user_id,participant_id,username,watching_together,viewing,loading,ready_generation FROM syncplay_members WHERE group_id=?", (self.id,))
        members = cursor.fetchall()
        return {"id": self.id, "name": f"{r[1]}'s group", "hostUserId": r[0], "hostName": r[1], "allowViewerControls": bool(r[2]), "itemId": r[3], "position": r[4], "playing": bool(r[5]), "resumeWhenReady": bool(r[6]), "revision": r[7], "groupRevision": r[7], "timelineRevision": r[8], "mediaGeneration": r[9], "anchorPosition": r[10], "anchorServerTime": r[11], "effectiveAt": r[12], "playbackState": r[13], "pauseReason": r[14], "hostDisconnectedAt": r[15], "updatedAt": r[16], "ended": bool(r[17]), "members": [{"userId": m[0], "participantId": m[1], "username": m[2], "watchingTogether": bool(m[3]), "viewing": bool(m[4]), "loading": bool(m[5]), "readyGeneration": m[6], "role": "host" if m[0] == r[0] else "viewer"} for m in members]}

    def state(self):
        with self.db.transaction() as cursor: return self._state(cursor)

    def member(self, user, participant_id):
        with self.db.transaction() as cursor:
            cursor.execute("SELECT 1 FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id=?", (self.id, user, participant_id))
            return bool(cursor.fetchone())

    @classmethod
    def active_groups_for_user(cls, user, participant_id=None):
        group = cls("_")
        query = "SELECT g.id FROM syncplay_groups g JOIN syncplay_members m ON m.group_id=g.id WHERE m.user_id=? AND g.ended=0"
        args = [user]
        if participant_id is not None: query += " AND m.participant_id=?"; args.append(participant_id)
        rows = group.db.execute(query, tuple(args))
        return [cls(row[0]) for row in rows]

    def _remembered(self, cursor, operation_id, user):
        if not operation_id: return None
        cursor.execute("SELECT state FROM syncplay_operations WHERE operation_id=? AND group_id=? AND user_id=?", (operation_id, self.id, user))
        row = cursor.fetchone()
        return json.loads(row[0]) if row else None

    def _store(self, cursor, operation_id, user, state):
        if operation_id:
            cursor.execute("INSERT INTO syncplay_operations (operation_id,group_id,user_id,state) VALUES (?,?,?,?)", (operation_id, self.id, user, json.dumps(state)))

    def mutate(self, user, expected_revision, operation_id, fn):
        """Serialize a complete Syncplay transition and make retries idempotent."""
        with self.db.transaction() as cursor:
            remembered = self._remembered(cursor, operation_id, user)
            if remembered is not None: return remembered
            state = self._state(cursor)
            if not state: return None
            if expected_revision is not None and expected_revision != state["revision"]:
                raise StaleSyncplayState(state)
            fn(cursor, state)
            result = self._state(cursor, include_ended=True)
            self._store(cursor, operation_id, user, result)
            return result

    def transition(self, cursor, state, timeline=False, **values):
        values["revision"] = state["revision"] + 1
        if timeline: values["timeline_revision"] = state["timelineRevision"] + 1
        values["updated"] = time.time()
        fields = ",".join(f"{key}=?" for key in values)
        cursor.execute(f"UPDATE syncplay_groups SET {fields} WHERE id=?", tuple(values.values()) + (self.id,))

    def waiting_for_members(self, cursor, generation):
        cursor.execute("SELECT viewing,loading,ready_generation FROM syncplay_members WHERE group_id=? AND watching_together=1", (self.id,))
        return any(not viewing or loading or ready_generation != generation for viewing, loading, ready_generation in cursor.fetchall())

    def reconcile_readiness(self, cursor, state):
        """Pause for opted-in buffering members and resume when all are ready."""
        waiting = self.waiting_for_members(cursor, state["mediaGeneration"])
        if waiting and (state["playing"] or state["playbackState"] == "playing"):
            now = time.time()
            position = projected_position(state, now)
            self.transition(
                cursor,
                state,
                timeline=True,
                position=position,
                playing=0,
                resume=1,
                anchor_position=position,
                anchor_time=now,
                effective_at=0,
                playback_state="paused",
                pause_reason="buffering",
            )
        elif (
            not waiting
            and state["resumeWhenReady"]
            and state["pauseReason"] != "command"
        ):
            schedule(self, cursor, state, projected_position(state), "buffering")
        else:
            self.transition(cursor, state)
        return waiting

    def apply_presence(
        self,
        cursor,
        state,
        user_id,
        participant_id,
        generation,
        timeline_revision,
        sequence,
        viewing,
        loading,
    ):
        """Apply presence only if it belongs to the current playback timeline."""
        if (
            generation != state["mediaGeneration"]
            or timeline_revision != state["timelineRevision"]
        ):
            return False
        cursor.execute(
            "SELECT presence_sequence,watching_together FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id=?",
            (self.id, user_id, participant_id),
        )
        row = cursor.fetchone()
        if not row or sequence <= row[0] or not row[1]:
            return False
        loading = bool(viewing and loading)
        cursor.execute(
            "UPDATE syncplay_members SET viewing=?,loading=?,ready_generation=?,presence_sequence=? WHERE group_id=? AND user_id=? AND participant_id=?",
            (
                int(bool(viewing)),
                int(loading),
                generation if viewing and not loading else -1,
                sequence,
                self.id,
                user_id,
                participant_id,
            ),
        )
        self.reconcile_readiness(cursor, state)
        return True

    def set_participation(self, user_id, participant_id, watching, operation_id=None):
        """Update durable viewing intent without trusting a caller-supplied identity."""
        def apply(cursor, state):
            loading = int(watching and state["itemId"] is not None and state["resumeWhenReady"])
            cursor.execute(
                "UPDATE syncplay_members SET watching_together=?,viewing=0,loading=?,ready_generation=-1,presence_sequence=0 WHERE group_id=? AND user_id=? AND participant_id=?",
                (int(watching), loading, self.id, user_id, participant_id),
            )
            self.reconcile_readiness(cursor, state)
        return self.mutate(user_id, None, operation_id, apply)

    def mark_host_disconnected(self):
        with self.db.transaction() as cursor:
            state = self._state(cursor)
            if not state or state["hostDisconnectedAt"] is not None: return state
            now = time.time(); position = projected_position(state, now)
            self.transition(cursor, state, timeline=True, position=position, playing=0, resume=0,
                            anchor_position=position, anchor_time=now, effective_at=0,
                            playback_state="paused", pause_reason="host-disconnected",
                            host_disconnected_at=now)
            return self._state(cursor, include_ended=True)

    def clear_host_disconnected(self):
        with self.db.transaction() as cursor:
            state = self._state(cursor)
            if not state or state["hostDisconnectedAt"] is None: return state
            self.transition(cursor, state, host_disconnected_at=None)
            return self._state(cursor, include_ended=True)

    def expire_host_disconnect(self, now=None):
        now = time.time() if now is None else now
        with self.db.transaction() as cursor:
            state = self._state(cursor)
            if not state or state["hostDisconnectedAt"] is None or now < state["hostDisconnectedAt"] + 300:
                return None
            self.transition(cursor, state, timeline=True, ended=1, playing=0, resume=0,
                            playback_state="paused", effective_at=0,
                            pause_reason="host-disconnected", host_disconnected_at=None)
            return self._state(cursor, include_ended=True)

    @classmethod
    def expire_due_host_disconnects(cls, now=None):
        now = time.time() if now is None else now
        rows = cls("_").db.execute("SELECT id FROM syncplay_groups WHERE ended=0 AND host_disconnected_at IS NOT NULL AND host_disconnected_at<=?", (now - 300,))
        states = []
        for row in rows:
            state = cls(row[0]).expire_host_disconnect(now)
            if state: states.append(state)
        return states

    def remove_disconnected_member(self, user_id, participant_id="legacy"):
        """Remove a member whose final Syncplay socket did not reconnect."""
        with self.db.transaction() as cursor:
            state = self._state(cursor)
            if not state: return None
            cursor.execute("SELECT 1 FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id=?", (self.id, user_id, participant_id))
            if not cursor.fetchone(): return None
            if state["hostUserId"] == user_id: return None
            cursor.execute("DELETE FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id=?", (self.id, user_id, participant_id))
            self.reconcile_readiness(cursor, state)
            return self._state(cursor, include_ended=True)
