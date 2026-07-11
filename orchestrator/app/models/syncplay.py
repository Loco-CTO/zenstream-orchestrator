import json
import time
import uuid

from app.config import Config


class StaleSyncplayState(Exception):
    def __init__(self, state): self.state = state


class SyncplayGroup:
    def __init__(self, group_id): self.id, self.db = group_id, Config().database

    @classmethod
    def create(cls, user_id, username):
        group = cls(str(uuid.uuid4())); now = time.time()
        group.db.execute("INSERT INTO syncplay_groups (id,host_user_id,host_name,updated) VALUES (?,?,?,?)", (group.id, user_id, username, now))
        group.db.execute("INSERT INTO syncplay_members (group_id,user_id,username) VALUES (?,?,?)", (group.id, user_id, username))
        return group

    def _state(self, cursor, include_ended=False):
        ended = "" if include_ended else " AND ended=0"
        cursor.execute("SELECT host_user_id,host_name,allow_controls,item_id,position,playing,resume,revision,timeline_revision,media_generation,anchor_position,anchor_time,effective_at,playback_state,pause_reason,updated,ended FROM syncplay_groups WHERE id=?" + ended, (self.id,))
        r = cursor.fetchone()
        if not r: return None
        cursor.execute("SELECT user_id,username,viewing,loading,ready_generation FROM syncplay_members WHERE group_id=?", (self.id,))
        members = cursor.fetchall()
        return {"id": self.id, "name": f"{r[1]}'s group", "hostUserId": r[0], "hostName": r[1], "allowViewerControls": bool(r[2]), "itemId": r[3], "position": r[4], "playing": bool(r[5]), "resumeWhenReady": bool(r[6]), "revision": r[7], "groupRevision": r[7], "timelineRevision": r[8], "mediaGeneration": r[9], "anchorPosition": r[10], "anchorServerTime": r[11], "effectiveAt": r[12], "playbackState": r[13], "pauseReason": r[14], "updatedAt": r[15], "ended": bool(r[16]), "members": [{"userId": m[0], "username": m[1], "viewing": bool(m[2]), "loading": bool(m[3]), "readyGeneration": m[4], "role": "host" if m[0] == r[0] else "viewer"} for m in members]}

    def state(self):
        with self.db.transaction() as cursor: return self._state(cursor)

    def member(self, user):
        with self.db.transaction() as cursor:
            cursor.execute("SELECT 1 FROM syncplay_members WHERE group_id=? AND user_id=?", (self.id, user))
            return bool(cursor.fetchone())

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
        cursor.execute("SELECT viewing,loading,ready_generation FROM syncplay_members WHERE group_id=?", (self.id,))
        return any(not viewing or loading or ready_generation != generation for viewing, loading, ready_generation in cursor.fetchall())
