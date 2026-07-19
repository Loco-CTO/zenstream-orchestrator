import asyncio
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import load_config
from app.models import Invite
from app.models.user import User
from app.models.admin import Admin
from app.models.syncplay import SyncplayGroup, SyncplayMembershipConflict, StaleSyncplayState, pause, schedule
from api.zenstream.version import _main_version
from jellyfin.api_service import authenticated_user_id
from version import __version__


class WebSocketHub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.identities: dict[WebSocket, tuple[str, str]] = {}
        self.disconnect_epochs: dict[tuple[str, str], int] = {}
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, user: str, participant: str):
        await websocket.accept()
        async with self.lock:
            self.clients.add(websocket)
            self.identities[websocket] = (user, participant)
            key = (user, participant)
            self.disconnect_epochs[key] = self.disconnect_epochs.get(key, 0) + 1

    async def epoch(self, user: str, participant: str):
        async with self.lock:
            return self.disconnect_epochs.get((user, participant), 0)

    async def sockets_for(self, user: str, participant: str):
        async with self.lock:
            return tuple(socket for socket, identity in self.identities.items() if identity == (user, participant))

    async def remove(self, websocket: WebSocket):
        async with self.lock:
            self.clients.discard(websocket)
            identity = self.identities.pop(websocket, None)
            if identity and not any(value == identity for value in self.identities.values()):
                self.disconnect_epochs[identity] = self.disconnect_epochs.get(identity, 0) + 1
            return identity, self.disconnect_epochs.get(identity, 0) if identity else None

    async def broadcast(self, message: dict):
        async with self.lock:
            clients = tuple(self.clients)
        dead = []
        for client in clients:
            try:
                await client.send_json(message)
            except Exception:
                dead.append(client)
        for client in dead:
            await self.remove(client)


hub = WebSocketHub()


async def _broadcast_group(state):
    if state:
        await hub.broadcast({"version": 1, "type": "group", "group": state})


async def _disconnect_cleanup(user, participant, epoch):
    await asyncio.sleep(30)
    if await hub.epoch(user, participant) != epoch or await hub.sockets_for(user, participant):
        return
    for group in SyncplayGroup.active_groups_for_user(user, participant):
        state = group.state()
        if state and state["hostUserId"] == user:
            await _broadcast_group(group.mark_host_disconnected())
        else:
            await _broadcast_group(group.remove_disconnected_member(user, participant))

    await asyncio.sleep(270)
    if await hub.epoch(user, participant) != epoch or await hub.sockets_for(user, participant):
        return
    for group in SyncplayGroup.active_groups_for_user(user, participant):
        state = group.expire_host_disconnect()
        if state:
            await hub.broadcast({"version": 1, "type": "group-ended", "id": state["id"], "revision": state["revision"]})


def _static_roots():
    root = Path(__file__).resolve().parents[1]
    web = root / "web"
    if not web.is_dir():
        web = root.parent / "frontend" / "out"
    return web, root.parent / "assets"


def _header_error(message: str, status: int):
    raise HTTPException(status_code=status, detail=message)


def _user_headers(username: str | None, token: str | None):
    if not isinstance(username, str) or not isinstance(token, str):
        _header_error("Authentication required.", 403)
    return username.strip(), token


def _admin_headers(username: str | None, token: str | None):
    username, token = _user_headers(username, token)
    if not Admin(username).authenticate(token):
        raise HTTPException(403, "Invalid administrator credentials.")
    return username, token


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_config()
    if not os.getenv("SECRET_KEY"):
        raise RuntimeError("Environment variable `SECRET_KEY` not set")
    yield
    await hub.broadcast({"type": "system", "event": "shutdown"})


app = FastAPI(
    title="ZenStream API",
    description="ZenStream Orchestrator API",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/swagger/",
    redoc_url="/api/redoc/",
    openapi_url="/api/openapi.json",
)

origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
origins += [x for x in ("http://localhost:3000", "http://127.0.0.1:3000") if x not in origins]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["TOKEN"])

web_root, assets_root = _static_roots()
if assets_root.is_dir():
    app.mount("/assets", StaticFiles(directory=assets_root), name="assets")
if (web_root / "_next").is_dir():
    app.mount("/_next", StaticFiles(directory=web_root / "_next"), name="next-assets")
if (web_root / "icons").is_dir():
    app.mount("/icons", StaticFiles(directory=web_root / "icons"), name="web-icons")


@app.get("/")
async def root():
    return {"status": "ok"}


@app.get("/api/docs/")
async def docs_alias():
    return RedirectResponse("/api/swagger/")


@app.get("/favicon.ico")
async def favicon():
    path = web_root / "favicon.ico"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/web/{path:path}")
async def dashboard(path: str = ""):
    requested = web_root / path
    if requested.is_file():
        return FileResponse(requested)
    route = path.strip("/") or "login"
    page = web_root / "web" / route / "index.html"
    if page.is_file():
        return FileResponse(page)
    fallback = web_root / "web" / "login" / "index.html"
    if fallback.is_file():
        return FileResponse(fallback)
    return JSONResponse({"message": "Dashboard assets are not installed."}, status_code=503)


@app.post("/api/user/login")
async def login(Username: str | None = Header(None), Password: str | None = Header(None)):
    username, password = _user_headers(Username, Password)
    from hashlib import sha256
    token = User(username).login(sha256(password.encode()).hexdigest())
    if not token:
        raise HTTPException(403, "Invalid credentials.")
    return JSONResponse({}, status_code=202, headers={"TOKEN": token})


@app.post("/api/admin/login")
async def admin_login(Username: str | None = Header(None), Password: str | None = Header(None)):
    username, password = _user_headers(Username, Password)
    token = Admin(username).login(password)
    if not token:
        raise HTTPException(403, "Invalid administrator credentials.")
    return JSONResponse({}, status_code=202, headers={"TOKEN": token})


@app.post("/api/admin/logout", status_code=204)
async def admin_logout(Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    username, token = _admin_headers(Username, TOKEN)
    Admin(username).logout(token)


@app.get("/api/admin/overview")
async def admin_overview(Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    username, _ = _admin_headers(Username, TOKEN)
    db = Admin(username)._db
    counts = db.execute("SELECT COUNT(*), SUM(CASE WHEN COALESCE(disabled, 0) = 0 THEN 1 ELSE 0 END), SUM(CASE WHEN COALESCE(disabled, 0) = 1 THEN 1 ELSE 0 END) FROM users")[0]
    return {"users": counts[0] or 0, "active_users": counts[1] or 0, "disabled_users": counts[2] or 0, "administrators": db.execute("SELECT COUNT(*) FROM admins WHERE disabled = 0")[0][0], "pending_invites": db.execute("SELECT COUNT(*) FROM invites")[0][0]}


@app.get("/api/admin/accounts")
async def admin_accounts(Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    username, _ = _admin_headers(Username, TOKEN)
    return Admin(username).list_accounts()


@app.get("/api/admin/users")
async def admin_users(Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    _admin_headers(Username, TOKEN)
    return User.list_accounts()


@app.post("/api/admin/accounts", status_code=201)
async def admin_create_account(Target_Username: str | None = Header(None), New_Password: str | None = Header(None), Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    username, _ = _admin_headers(Username, TOKEN)
    if not Target_Username or not New_Password or len(New_Password) < 8 or not Admin(username).create(Target_Username, New_Password):
        raise HTTPException(400, "Invalid or duplicate administrator account.")


@app.patch("/api/admin/users/{target_username}")
async def admin_update_user(target_username: str, disabled: bool | None = Query(None), New_Password: str | None = Header(None), Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    _admin_headers(Username, TOKEN)
    if New_Password is not None:
        if len(New_Password) < 8 or not User.reset_password(target_username, New_Password):
            raise HTTPException(400, "Invalid password or user not found.")
    elif disabled is None or not User.set_disabled_account(target_username, disabled):
        raise HTTPException(404, "User not found.")


@app.delete("/api/admin/users/{target_username}", status_code=204)
async def admin_delete_user(target_username: str, Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    _admin_headers(Username, TOKEN)
    if not User.delete_account(target_username):
        raise HTTPException(404, "User not found.")


@app.post("/api/user/authenticate")
async def authenticate(Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    username, token = _user_headers(Username, TOKEN)
    if not User(username).authenticate(token):
        raise HTTPException(403, "Invalid credentials.")
    return JSONResponse({}, status_code=202)


@app.get("/api/user/me")
async def me(Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    username, token = _user_headers(Username, TOKEN)
    if not User(username).authenticate(token):
        raise HTTPException(403, "Invalid credentials.")
    return User(username).info()


@app.get("/api/user/logout")
async def logout(Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    username, token = _user_headers(Username, TOKEN)
    if not User(username).authenticate(token):
        raise HTTPException(403, "Invalid credentials.")
    if not User(username).logout(token):
        raise HTTPException(500, "Logout failed.")
    return JSONResponse({}, status_code=200)


@app.get("/api/user/check_invite")
async def check_invite(url: str | None = Header(None)):
    if not isinstance(url, str) or not Invite().validate(url.strip()):
        raise HTTPException(403, "Invalid invite.")
    return JSONResponse({}, status_code=202)


@app.get("/api/version")
async def version():
    return {"version": __version__, "main": _main_version()}


@app.get("/api/config")
async def mobile_config():
    jellyfin_url = os.getenv("JELLYFIN_URL", "").rstrip("/")
    if not jellyfin_url:
        raise HTTPException(503, "Jellyfin server is not configured.")
    return {"jellyfinUrl": jellyfin_url}


@app.get("/api/preferences/locale")
async def get_locale(x_jellyfin_token: str | None = Header(None)):
    user_id = authenticated_user_id(x_jellyfin_token) if x_jellyfin_token else None
    if not user_id: raise HTTPException(401, "Invalid token.")
    from app.models.preference import UserPreference
    return {"locale": UserPreference(user_id).get_locale()}


@app.patch("/api/preferences/locale")
async def set_locale(request: Request, x_jellyfin_token: str | None = Header(None)):
    user_id = authenticated_user_id(x_jellyfin_token) if x_jellyfin_token else None
    if not user_id: raise HTTPException(401, "Invalid token.")
    from app.models.preference import UserPreference, SUPPORTED_LOCALES
    locale = (await request.json()).get("locale")
    if locale not in SUPPORTED_LOCALES: raise HTTPException(400, "Unsupported locale.")
    return {"locale": UserPreference(user_id).set_locale(locale)}


@app.get("/api/preferences/subtitles")
async def get_subtitles(x_jellyfin_token: str | None = Header(None)):
    user_id = authenticated_user_id(x_jellyfin_token) if x_jellyfin_token else None
    if not user_id: raise HTTPException(401, "Invalid token.")
    from app.models.preference import UserPreference
    return UserPreference(user_id).get_subtitle_style()


@app.patch("/api/preferences/subtitles")
async def set_subtitles(request: Request, x_jellyfin_token: str | None = Header(None)):
    user_id = authenticated_user_id(x_jellyfin_token) if x_jellyfin_token else None
    if not user_id: raise HTTPException(401, "Invalid token.")
    from app.models.preference import UserPreference
    try: return UserPreference(user_id).set_subtitle_style(await request.json())
    except ValueError as error: raise HTTPException(400, str(error))


def _sync_identity(token: str | None, participant: str | None):
    user = authenticated_user_id(token) if token else None
    if not user or not participant:
        raise HTTPException(401, "Authentication required.")
    return user, participant


@app.get("/api/syncplay/groups")
async def syncplay_groups(x_jellyfin_token: str | None = Header(None), x_zenstream_participant: str | None = Header(None)):
    _sync_identity(x_jellyfin_token, x_zenstream_participant)
    rows = SyncplayGroup("_").db.execute("SELECT id FROM syncplay_groups WHERE ended=0 ORDER BY updated DESC", ())
    return {"groups": [SyncplayGroup(row[0]).state() for row in rows]}


@app.post("/api/syncplay/groups")
async def syncplay_create(request: Request):
    user, participant = _sync_identity(request.headers.get("x-jellyfin-token"), request.headers.get("x-zenstream-participant"))
    try:
        state = SyncplayGroup.create(user, participant, request.headers.get("x-zenstream-username", "ZenStream")).state()
    except SyncplayMembershipConflict:
        raise HTTPException(409, "You already belong to an active Syncplay group.")
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    return JSONResponse(state, status_code=201)


@app.get("/api/syncplay/groups/{group_id}")
async def syncplay_group(group_id: str, request: Request):
    user, participant = _sync_identity(request.headers.get("x-jellyfin-token"), request.headers.get("x-zenstream-participant"))
    group = SyncplayGroup(group_id)
    state = group.state()
    if not state: raise HTTPException(404, "Group not found.")
    if not group.member(user, participant): raise HTTPException(403, "Join this group first.")
    return state


async def _sync_group_context(group_id: str, request: Request):
    user, participant = _sync_identity(request.headers.get("x-jellyfin-token"), request.headers.get("x-zenstream-participant"))
    group = SyncplayGroup(group_id)
    if not group.state(): raise HTTPException(404, "Group not found.")
    if not group.member(user, participant): raise HTTPException(403, "Join this group first.")
    return user, participant, group, await request.json() if request.headers.get("content-length", "0") != "0" else {}


@app.post("/api/syncplay/groups/{group_id}/join")
async def syncplay_join(group_id: str, request: Request):
    user, participant = _sync_identity(request.headers.get("x-jellyfin-token"), request.headers.get("x-zenstream-participant"))
    group = SyncplayGroup(group_id)
    if not group.state(): raise HTTPException(404, "Group not found.")
    data = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    replaced = []
    try:
        def apply(cursor, state):
            cursor.execute("SELECT 1 FROM syncplay_members m JOIN syncplay_groups g ON g.id=m.group_id WHERE m.user_id=? AND g.ended=0 AND m.group_id<>? LIMIT 1", (user, group_id))
            if cursor.fetchone(): raise SyncplayMembershipConflict
            cursor.execute("SELECT participant_id FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id<>?", (group_id, user, participant))
            replaced.extend(row[0] for row in cursor.fetchall())
            cursor.execute("DELETE FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id<>?", (group_id, user, participant))
            cursor.execute("INSERT OR IGNORE INTO syncplay_members (group_id,user_id,participant_id,username) VALUES (?,?,?,?)", (group_id, user, participant, request.headers.get("x-zenstream-username", "ZenStream")))
            group.transition(cursor, state)
        state = group.mutate(user, data.get("expectedRevision"), data.get("operationId"), apply)
    except SyncplayMembershipConflict: raise HTTPException(409, "You must leave your current Syncplay group first.")
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    for old_participant in replaced:
        for socket in await hub.sockets_for(user, old_participant):
            try:
                await socket.send_json({"version": 1, "type": "participant-replaced", "id": group_id, "revision": state["revision"]})
            except Exception:
                pass
    return state


@app.delete("/api/syncplay/groups/{group_id}")
async def syncplay_leave(group_id: str, request: Request):
    user, participant, group, data = await _sync_group_context(group_id, request)
    def apply(cursor, state):
        if state["hostUserId"] == user:
            group.transition(cursor, state, timeline=True, ended=1, playing=0, resume=0, playback_state="paused", effective_at=0, host_disconnected_at=None)
        else:
            cursor.execute("DELETE FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id=?", (group_id, user, participant))
            group.transition(cursor, state)
    state = group.mutate(user, data.get("expectedRevision"), data.get("operationId"), apply)
    await hub.broadcast({"version": 1, "type": "group-ended" if state["ended"] else "group", "id": group_id, "revision": state["revision"], "group": state})
    return JSONResponse(content=None, status_code=204)


@app.patch("/api/syncplay/groups/{group_id}")
async def syncplay_settings(group_id: str, request: Request):
    user, participant, group, data = await _sync_group_context(group_id, request)
    value = data.get("allowViewerControls")
    if not isinstance(value, bool): raise HTTPException(400, "allowViewerControls must be boolean.")
    def apply(cursor, state):
        if state["hostUserId"] != user: raise PermissionError
        group.transition(cursor, state, allow_controls=int(value))
    try: state = group.mutate(user, data.get("expectedRevision"), data.get("operationId"), apply)
    except PermissionError: raise HTTPException(403, "Only the host can change settings.")
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    return state


@app.delete("/api/syncplay/groups/{group_id}/members/{member_id}")
async def syncplay_remove_member(group_id: str, member_id: str, request: Request):
    user, participant, group, data = await _sync_group_context(group_id, request)
    def apply(cursor, state):
        if state["hostUserId"] != user: raise PermissionError
        if member_id == user: raise ValueError
        cursor.execute("DELETE FROM syncplay_members WHERE group_id=? AND user_id=?", (group_id, member_id))
        group.reconcile_readiness(cursor, state)
    try: state = group.mutate(user, data.get("expectedRevision"), data.get("operationId"), apply)
    except PermissionError: raise HTTPException(403, "Only the host can remove members.")
    except ValueError: raise HTTPException(400, "The host cannot remove themselves.")
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    return state


@app.post("/api/syncplay/groups/{group_id}/command")
async def syncplay_command(group_id: str, request: Request):
    user, participant, group, data = await _sync_group_context(group_id, request)
    position, action = data.get("position"), data.get("action")
    if not isinstance(position, (int, float)) or not math.isfinite(position) or position < 0: raise HTTPException(400, "Invalid playback position.")
    if action not in {"media", "play", "pause", "seek"}: raise HTTPException(400, "Invalid playback command.")
    def apply(cursor, state):
        if user != state["hostUserId"] and not state["allowViewerControls"]: raise PermissionError
        item = data.get("itemId", state["itemId"])
        if action == "media":
            if not isinstance(item, str): raise ValueError
            generation = state["mediaGeneration"] + 1
            cursor.execute("UPDATE syncplay_members SET watching_together=1 WHERE group_id=? AND participant_id=?", (group_id, participant))
            cursor.execute("UPDATE syncplay_members SET viewing=0,loading=CASE WHEN watching_together=1 THEN 1 ELSE 0 END,ready_generation=-1,presence_sequence=0 WHERE group_id=?", (group_id,))
            group.transition(cursor, state, timeline=True, item_id=item, position=float(position), playing=0, resume=1, media_generation=generation, anchor_position=float(position), anchor_time=time.time(), effective_at=0, playback_state="paused", pause_reason="readiness")
            return
        requested = bool(data.get("playing", state["playing"]))
        if action == "seek" and state["resumeWhenReady"]: requested = True
        elif action == "play": requested = True
        elif action == "pause": requested = False
        waiting = group.waiting_for_members(cursor, state["mediaGeneration"])
        if requested and not waiting: schedule(group, cursor, state, float(position))
        elif requested: group.transition(cursor, state, timeline=True, position=float(position), playing=0, resume=1, anchor_position=float(position), anchor_time=time.time(), effective_at=0, playback_state="paused", pause_reason="readiness")
        else: pause(group, cursor, state, "command")
    try: state = group.mutate(user, data.get("expectedRevision"), data.get("operationId"), apply)
    except PermissionError: raise HTTPException(403, "Only the host can control playback.")
    except ValueError: raise HTTPException(400, "A media item is required.")
    except StaleSyncplayState as error: raise HTTPException(409, detail={"message": "Playback state is out of date.", "group": error.state})
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    return state


@app.post("/api/syncplay/groups/{group_id}/presence")
async def syncplay_presence(group_id: str, request: Request):
    user, participant, group, data = await _sync_group_context(group_id, request)
    generation, sequence = data.get("mediaGeneration"), data.get("presenceSequence")
    if not isinstance(generation, int) or not isinstance(sequence, int): raise HTTPException(400, "mediaGeneration and presenceSequence are required.")
    def apply(cursor, state):
        if generation != state["mediaGeneration"]: return
        cursor.execute("SELECT presence_sequence,watching_together FROM syncplay_members WHERE group_id=? AND participant_id=?", (group_id, participant))
        row = cursor.fetchone()
        if not row or sequence <= row[0] or not row[1]: return
        viewing, loading = bool(data.get("viewing")), bool(data.get("loading")) if data.get("viewing") else False
        cursor.execute("UPDATE syncplay_members SET viewing=?,loading=?,ready_generation=?,presence_sequence=? WHERE group_id=? AND participant_id=?", (int(viewing), int(loading), generation if viewing and not loading else -1, sequence, group_id, participant))
        group.reconcile_readiness(cursor, state)
    state = group.mutate(user, None, data.get("operationId"), apply)
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    return state


@app.post("/api/syncplay/groups/{group_id}/participation")
async def syncplay_participation(group_id: str, request: Request):
    user, participant, group, data = await _sync_group_context(group_id, request)
    watching = data.get("watchingTogether")
    if not isinstance(watching, bool): raise HTTPException(400, "watchingTogether must be boolean.")
    try: state = group.set_participation(user, participant, watching, data.get("operationId"))
    except StaleSyncplayState as error: raise HTTPException(409, detail={"message": "Playback state is out of date.", "group": error.state})
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    return state


@app.websocket("/api/ws/syncplay")
async def syncplay_socket(websocket: WebSocket):
    token = websocket.headers.get("x-jellyfin-token") or websocket.query_params.get("token")
    participant = websocket.headers.get("x-zenstream-participant") or websocket.query_params.get("participantId")
    user_id = authenticated_user_id(token) if token else None
    if not user_id or not participant:
        await websocket.close(code=1008)
        return
    await hub.connect(websocket, user_id, participant)
    try:
        for group in SyncplayGroup.active_groups_for_user(user_id, participant):
            state = group.clear_host_disconnected()
            if state:
                await _broadcast_group(state)
        rows = SyncplayGroup("_").db.execute("SELECT id FROM syncplay_groups WHERE ended=0 ORDER BY updated DESC", ())
        await websocket.send_json({"version": 1, "type": "groups", "groups": [SyncplayGroup(row[0]).state() for row in rows]})
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "clock":
                received = time.time()
                await websocket.send_json({"version": 1, "type": "clock", "clientSentAt": message.get("clientSentAt"), "serverReceivedAt": received, "serverSentAt": time.time()})
    except WebSocketDisconnect:
        pass
    finally:
        identity, epoch = await hub.remove(websocket)
        if identity and not await hub.sockets_for(*identity):
            asyncio.create_task(_disconnect_cleanup(*identity, epoch))
