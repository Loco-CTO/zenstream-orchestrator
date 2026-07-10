import time
import uuid
from app.config import Config


class SyncplayGroup:
    def __init__(self, group_id): self.id, self.db = group_id, Config().database

    @classmethod
    def create(cls, user_id, username):
        group=cls(str(uuid.uuid4())); now=time.time()
        group.db.execute("INSERT INTO syncplay_groups (id,host_user_id,host_name,updated) VALUES (?,?,?,?)",(group.id,user_id,username,now))
        group.db.execute("INSERT INTO syncplay_members (group_id,user_id,username) VALUES (?,?,?)",(group.id,user_id,username)); return group

    def state(self):
        rows=self.db.execute("SELECT host_user_id,host_name,allow_controls,item_id,position,playing,resume,revision,updated FROM syncplay_groups WHERE id=? AND ended=0",(self.id,))
        if not rows:return None
        r=rows[0]; members=self.db.execute("SELECT user_id,username,viewing,loading FROM syncplay_members WHERE group_id=?",(self.id,))
        return {"id":self.id,"name":f"{r[1]}'s group","hostUserId":r[0],"hostName":r[1],"allowViewerControls":bool(r[2]),"itemId":r[3],"position":r[4],"playing":bool(r[5]),"resumeWhenReady":bool(r[6]),"revision":r[7],"updatedAt":r[8],"members":[{"userId":m[0],"username":m[1],"viewing":bool(m[2]),"loading":bool(m[3]),"role":"host" if m[0]==r[0] else "viewer"} for m in members]}

    def member(self,user): return bool(self.db.execute("SELECT 1 FROM syncplay_members WHERE group_id=? AND user_id=?",(self.id,user)))
    def join(self,user,name): self.db.execute("INSERT INTO syncplay_members (group_id,user_id,username) VALUES (?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET username=excluded.username",(self.id,user,name))
    def leave(self,user): self.db.execute("DELETE FROM syncplay_members WHERE group_id=? AND user_id=?",(self.id,user))
    def update(self, **values):
        state=self.state(); values["revision"]=state["revision"]+1; values["updated"]=time.time()
        fields=','.join(f"{key}=?" for key in values); self.db.execute(f"UPDATE syncplay_groups SET {fields} WHERE id=?",tuple(values.values())+(self.id,)); return self.state()
