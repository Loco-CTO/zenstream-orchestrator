import math
from flask import request
from flask_restx import Resource
from app.models.syncplay import SyncplayGroup
from jellyfin.api_service import authenticated_user_id
from . import api_namespace_zs

def identity():
    token=request.headers.get("X-Jellyfin-Token"); return authenticated_user_id(token) if token else None
def name(): return request.headers.get("X-ZenStream-Username","ZenStream")
def group_for(group_id):
    user=identity(); group=SyncplayGroup(group_id)
    if not user:return None,None,({"message":"Authentication required."},401)
    if not group.state():return None,None,({"message":"Group not found."},404)
    if not group.member(user):return None,None,({"message":"Join this group first."},403)
    return user,group,None

@api_namespace_zs.route("zenstream/syncplay/groups")
class Groups(Resource):
    def get(self):
        if not identity():return {"message":"Authentication required."},401
        ids=SyncplayGroup("_").db.execute("SELECT id FROM syncplay_groups WHERE ended=0 ORDER BY updated DESC",())
        return {"groups":[SyncplayGroup(x[0]).state() for x in ids]},200
    def post(self):
        user=identity()
        if not user:return {"message":"Authentication required."},401
        return SyncplayGroup.create(user,name()).state(),201

@api_namespace_zs.route("zenstream/syncplay/groups/<string:group_id>/join")
class Join(Resource):
    def post(self,group_id):
        user=identity(); group=SyncplayGroup(group_id)
        if not user:return {"message":"Authentication required."},401
        if not group.state():return {"message":"Group not found."},404
        group.join(user,name()); return group.state(),200

@api_namespace_zs.route("zenstream/syncplay/groups/<string:group_id>")
class Group(Resource):
    def get(self,group_id):
        _,group,error=group_for(group_id); return error or (group.state(),200)
    def delete(self,group_id):
        user,group,error=group_for(group_id)
        if error:return error
        if group.state()["hostUserId"]==user:group.db.execute("UPDATE syncplay_groups SET ended=1 WHERE id=?",(group_id,))
        else:group.leave(user)
        return "",204
    def patch(self,group_id):
        user,group,error=group_for(group_id)
        if error:return error
        value=(request.get_json(silent=True) or {}).get("allowViewerControls")
        if group.state()["hostUserId"]!=user:return {"message":"Only the host can change settings."},403
        if not isinstance(value,bool):return {"message":"allowViewerControls must be boolean."},400
        return group.update(allow_controls=int(value)),200

@api_namespace_zs.route("zenstream/syncplay/groups/<string:group_id>/command")
class Command(Resource):
    def post(self,group_id):
        user,group,error=group_for(group_id)
        if error:return error
        state=group.state(); data=request.get_json(silent=True) or {}
        if user!=state["hostUserId"] and not state["allowViewerControls"]:return {"message":"Only the host can control playback."},403
        if data.get("revision")!=state["revision"]:return {"message":"Playback state is out of date."},409
        pos=data.get("position",state["position"])
        if not isinstance(pos,(int,float)) or not math.isfinite(pos) or pos<0:return {"message":"Invalid playback position."},400
        item=data.get("itemId",state["itemId"])
        if data.get("action")=="media" and not isinstance(item,str):return {"message":"A media item is required."},400
        if data.get("action")=="media":return group.begin_media(item,float(pos)),200
        if data.get("playing") and group.waiting_for_members():
            return group.update(item_id=item,position=float(pos),playing=0,resume=1),200
        return group.update(item_id=item,position=float(pos),playing=int(bool(data.get("playing",state["playing"]))),resume=0),200

@api_namespace_zs.route("zenstream/syncplay/groups/<string:group_id>/presence")
class Presence(Resource):
    def post(self,group_id):
        user,group,error=group_for(group_id)
        if error:return error
        data=request.get_json(silent=True) or {}; viewing=bool(data.get("viewing")); loading=bool(data.get("loading")) if viewing else False
        group.db.execute("UPDATE syncplay_members SET viewing=?,loading=? WHERE group_id=? AND user_id=?",(int(viewing),int(loading),group_id,user))
        state=group.state(); blocked=group.waiting_for_members()
        if blocked and state["playing"]: return group.update(playing=0,resume=1),200
        if not blocked and state["resumeWhenReady"]: return group.update(playing=1,resume=0),200
        return state,200
