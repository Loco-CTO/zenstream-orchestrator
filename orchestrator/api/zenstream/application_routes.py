import asyncio
import math
import time
from pathlib import Path

from app.client_auth import (
    administrator_origin_allowed,
    cookie_secure,
    require_account,
    websocket_account,
)
from app.intro_outro import IntroOutroStore
from app.jobs import scheduler
from app.models import Invite
from app.models.account import Account
from app.models.admin import ADMIN_SESSION_COOKIE, Admin
from app.models.playback_settings import PlaybackSettings
from app.models.syncplay import (
    StaleSyncplayState,
    SyncplayGroup,
    SyncplayMembershipConflict,
    pause,
    schedule,
)
from app.paths import PROJECT_ROOT
from app.playback import ffmpeg_path, ffprobe_path
from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from version import __version__

from api.zenstream.client_routes import _bounded_json_object, _enforce_rate_limit
from api.zenstream.version import _main_version


def _redact_syncplay_state(state: dict, user_id: str, participant: str) -> dict:
    """Return full state to members and a safe lobby projection to everyone else."""
    group_id = state.get("id")
    if group_id and SyncplayGroup(group_id).member(user_id, participant):
        return state
    return {
        **state,
        "hostUserId": None,
        "itemId": None,
        "position": 0,
        "playing": False,
        "members": [
            {
                "role": member.get("role"),
                "watchingTogether": bool(member.get("watchingTogether")),
                "viewing": False,
                "loading": False,
            }
            for member in state.get("members", [])
        ],
    }


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
            return tuple(
                socket
                for socket, identity in self.identities.items()
                if identity == (user, participant)
            )

    async def remove(self, websocket: WebSocket):
        async with self.lock:
            self.clients.discard(websocket)
            identity = self.identities.pop(websocket, None)
            if identity and not any(
                value == identity for value in self.identities.values()
            ):
                self.disconnect_epochs[identity] = (
                    self.disconnect_epochs.get(identity, 0) + 1
                )
            return identity, self.disconnect_epochs.get(
                identity, 0
            ) if identity else None

    async def broadcast(self, message: dict):
        async with self.lock:
            clients = tuple(self.clients)
        dead = []
        for client in clients:
            try:
                payload = message
                if message.get("type") == "group" and isinstance(
                    message.get("group"), dict
                ):
                    user_id, participant = self.identities.get(client, ("", ""))
                    payload = {
                        **message,
                        "group": _redact_syncplay_state(
                            message["group"], user_id, participant
                        ),
                    }
                await client.send_json(payload)
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
    if await hub.epoch(user, participant) != epoch or await hub.sockets_for(
        user, participant
    ):
        return
    for group in SyncplayGroup.active_groups_for_user(user, participant):
        state = group.state()
        if state and state["hostUserId"] == user:
            await _broadcast_group(group.mark_host_disconnected())
        else:
            await _broadcast_group(group.deactivate_member(user, participant))

    await asyncio.sleep(270)
    if await hub.epoch(user, participant) != epoch or await hub.sockets_for(
        user, participant
    ):
        return
    for group in SyncplayGroup.active_groups_for_user(user, participant):
        state = group.remove_disconnected_member(user, participant)
        if state and state["hostUserId"] != user:
            await _broadcast_group(state)
            continue
        state = group.expire_host_disconnect()
        if state:
            await hub.broadcast(
                {
                    "version": 1,
                    "type": "group-ended",
                    "id": state["id"],
                    "revision": state["revision"],
                }
            )


def _static_roots():
    candidates = (
        PROJECT_ROOT / "web",
        PROJECT_ROOT / "orchestrator" / "web",
        PROJECT_ROOT / "frontend" / "out",
    )
    web = next(
        (candidate for candidate in candidates if candidate.is_dir()), candidates[0]
    )
    return web, PROJECT_ROOT / "assets"


def _header_error(message: str, status: int):
    raise HTTPException(status_code=status, detail=message)


def _user_headers(username: str | None, token: str | None):
    if not isinstance(username, str) or not isinstance(token, str):
        _header_error("Authentication required.", 403)
    return username.strip(), token


def _admin_headers(username: str | None, token: str | None):
    admin = Admin.from_token(token)
    if admin is None:
        raise HTTPException(401, "Invalid administrator credentials.")
    return admin.username, token


def _admin_request(
    request: Request, username: str | None = None, token: str | None = None
):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if not administrator_origin_allowed(request):
            raise HTTPException(403, "Cross-site administrator request rejected.")
    cookie_name = (
        ADMIN_SESSION_COOKIE if cookie_secure(request) else "zenstream-admin-session"
    )
    cookie_token = request.cookies.get(cookie_name)
    return _admin_headers(username, cookie_token or token)


def _root_admin_request(
    request: Request, username: str | None = None, token: str | None = None
):
    username, token = _admin_request(request, username, token)
    profile = Admin(username).profile()
    if not profile or not profile["is_root"]:
        raise HTTPException(403, "Root administrator access is required.")
    return username, token


def _contained_static_file(root: Path, *parts: str) -> Path | None:
    resolved_root = root.resolve()
    try:
        candidate = resolved_root.joinpath(*parts).resolve()
        candidate.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _static_path_is_contained(root: Path, path: str) -> bool:
    resolved_root = root.resolve()
    try:
        resolved_root.joinpath(path).resolve().relative_to(resolved_root)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


router = APIRouter()
web_root, _assets_root = _static_roots()


@router.get("/")
async def root():
    return {"status": "ok"}


@router.get("/api/docs/")
async def docs_alias():
    return RedirectResponse("/api/swagger/")


@router.get("/favicon.ico")
async def favicon():
    path = _contained_static_file(web_root, "favicon.ico")
    if path is None:
        raise HTTPException(404)
    return FileResponse(path)


@router.get("/web/{path:path}")
async def dashboard(path: str = ""):
    if not _static_path_is_contained(web_root, path):
        raise HTTPException(404)
    requested = _contained_static_file(web_root, path)
    if requested is not None:
        return FileResponse(requested)
    route = path.strip("/") or "login"
    page = _contained_static_file(web_root, "web", route, "index.html")
    if page is not None:
        return FileResponse(page)
    fallback = _contained_static_file(web_root, "web", "login", "index.html")
    if fallback is not None:
        return FileResponse(fallback)
    return JSONResponse(
        {"message": "Dashboard assets are not installed."}, status_code=503
    )


@router.post("/api/admin/login")
async def admin_login(
    request: Request,
    Username: str | None = Header(None),
    Password: str | None = Header(None),
):
    username, password = _user_headers(Username, Password)
    token = Admin(username).login(password)
    if not token:
        raise HTTPException(403, "Invalid administrator credentials.")
    response = JSONResponse({"username": username}, status_code=202)
    response.set_cookie(
        ADMIN_SESSION_COOKIE if cookie_secure(request) else "zenstream-admin-session",
        token,
        max_age=7 * 24 * 60 * 60,
        secure=cookie_secure(request),
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@router.post("/api/admin/logout", status_code=204)
async def admin_logout(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    username, token = _admin_request(request, Username, TOKEN)
    Admin(username).logout(token)
    response = Response(status_code=204)
    response.delete_cookie(
        ADMIN_SESSION_COOKIE if cookie_secure(request) else "zenstream-admin-session",
        secure=cookie_secure(request),
        httponly=True,
        samesite="strict",
        path="/",
    )
    if not cookie_secure(request):
        response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
    return response


@router.get("/api/admin/profile")
async def admin_profile(
    request: Request = None,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    username, _ = (
        _admin_request(request, Username, TOKEN)
        if request is not None
        else _admin_headers(Username, TOKEN)
    )
    profile = Admin(username).profile()
    if not profile:
        raise HTTPException(404, "Administrator account not found.")
    return profile


@router.patch("/api/admin/profile")
async def admin_update_profile(
    request: Request = None,
    New_Username: str | None = Header(None),
    New_Password: str | None = Header(None),
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    username, token = (
        _admin_request(request, Username, TOKEN)
        if request is not None
        else _admin_headers(Username, TOKEN)
    )
    try:
        result = Admin(username).update_profile(New_Username, New_Password, token)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"username": result["username"]}


@router.get("/api/admin/overview")
async def admin_overview(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    username, _ = _admin_request(request, Username, TOKEN)
    db = Admin(username)._db
    counts = db.execute(
        "SELECT COUNT(*), SUM(CASE WHEN COALESCE(disabled, 0) = 0 THEN 1 ELSE 0 END), SUM(CASE WHEN COALESCE(disabled, 0) = 1 THEN 1 ELSE 0 END) FROM users"
    )[0]
    return {
        "users": counts[0] or 0,
        "active_users": counts[1] or 0,
        "disabled_users": counts[2] or 0,
        "administrators": db.execute("SELECT COUNT(*) FROM admins WHERE disabled = 0")[
            0
        ][0],
        "pending_invites": db.execute("SELECT COUNT(*) FROM invites")[0][0],
    }


@router.get("/api/admin/playback/settings")
async def admin_playback_settings(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    _admin_request(request, Username, TOKEN)
    return PlaybackSettings().get()


@router.put("/api/admin/playback/settings")
async def update_admin_playback_settings(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    _admin_request(request, Username, TOKEN)
    data = await request.json()
    current = PlaybackSettings().get()
    width = data.get("trickplayFrameWidth", current["trickplayFrameWidth"])
    height = data.get("trickplayFrameHeight")
    if height is None:
        try:
            height = int(width) * 9 // 16
        except (TypeError, ValueError):
            height = current["trickplayFrameHeight"]
    try:
        values = PlaybackSettings().set(
            data.get("maxTranscodes", current["maxTranscodes"]),
            data.get("maxTranscodesPerUser", current["maxTranscodesPerUser"]),
            width,
            height,
            data.get("trickplayIntervalSeconds", current["trickplayIntervalSeconds"]),
            data.get("trickplayWorkers", current["trickplayWorkers"]),
        )
        return values
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/admin/intro-outro/settings")
async def admin_intro_outro_settings(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    _admin_request(request, Username, TOKEN)
    store = IntroOutroStore()
    definition = scheduler.store.by_key("intro_outro_detect")
    return {**store.settings(), "task": definition}


@router.put("/api/admin/intro-outro/settings")
async def update_admin_intro_outro_settings(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    _admin_request(request, Username, TOKEN)
    settings = IntroOutroStore().update_settings(await request.json())
    scheduler.enqueue_intro_outro_detection()
    return settings


@router.post("/api/admin/intro-outro/clear")
async def clear_admin_intro_outro_segments(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    _admin_request(request, Username, TOKEN)
    return {"removedSegments": IntroOutroStore().clear_segments()}


@router.get("/api/admin/accounts")
async def admin_accounts(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    username, _ = _admin_request(request, Username, TOKEN)
    return Admin(username).list_accounts()


@router.post("/api/admin/accounts", status_code=201)
async def admin_create_account(
    request: Request,
    Target_Username: str | None = Header(None),
    New_Password: str | None = Header(None),
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    username, _ = _admin_request(request, Username, TOKEN)
    if (
        not Target_Username
        or not New_Password
        or len(New_Password) < 8
        or not Admin(username).create(Target_Username, New_Password)
    ):
        raise HTTPException(400, "Invalid or duplicate administrator account.")


@router.patch("/api/admin/accounts/{target_username}")
async def admin_set_account_disabled(
    target_username: str,
    disabled: bool,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    username, _ = _root_admin_request(request, Username, TOKEN)
    if not Admin(username).set_disabled(target_username.strip(), disabled):
        raise HTTPException(404, "Administrator account not found.")
    profile = Admin(target_username.strip()).profile()
    if not profile:
        raise HTTPException(404, "Administrator account not found.")
    return profile


@router.post("/api/admin/invites", status_code=201)
async def admin_create_invite(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    _root_admin_request(request, Username, TOKEN)
    return {"inviteid": Invite().generate()}


@router.get("/api/user/check_invite")
async def check_invite(url: str | None = Header(None)):
    if not isinstance(url, str) or not Invite().validate(url.strip()):
        raise HTTPException(403, "Invalid invite.")
    return JSONResponse({}, status_code=202)


@router.get("/api/version")
async def version():
    return {"version": __version__, "main": _main_version()}


@router.get("/api/config")
async def mobile_config():
    ffmpeg = ffmpeg_path()
    ffprobe = ffprobe_path()
    return {
        "apiVersion": 2,
        "catalog": True,
        "playback": bool(ffmpeg and ffprobe),
        "version": __version__,
        "main": _main_version(),
    }


@router.post("/api/user/register", status_code=201)
async def register_client(request: Request):
    _enforce_rate_limit(request, "register", 5)
    data = await _bounded_json_object(request)
    invite_id = str(data.get("invite") or request.headers.get("url") or "").strip()
    username = str(
        data.get("username") or request.headers.get("username") or ""
    ).strip()
    password = str(data.get("password") or request.headers.get("password") or "")
    if not Invite().validate(invite_id):
        raise HTTPException(403, "Invalid invite.")
    try:
        account = Account().create(username, password)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    Invite().delete(invite_id)
    return {"user": account}


def _sync_identity(request: Request):
    account, _ = require_account(request)
    participant = request.headers.get("x-zenstream-participant")
    if not participant:
        raise HTTPException(401, "Authentication required.")
    return account["id"], participant


@router.get("/api/syncplay/groups")
async def syncplay_groups(
    request: Request,
):
    user_id, participant = _sync_identity(request)
    rows = SyncplayGroup("_").db.execute(
        "SELECT id FROM syncplay_groups WHERE ended=0 ORDER BY updated DESC", ()
    )
    return {
        "groups": [
            _redact_syncplay_state(SyncplayGroup(row[0]).state(), user_id, participant)
            for row in rows
        ]
    }


@router.post("/api/syncplay/groups")
async def syncplay_create(request: Request):
    user, participant = _sync_identity(request)
    try:
        account = Account()._row(user_id=user, read_only=True)
        state = SyncplayGroup.create(
            user, participant, account[1] if account else "ZenStream"
        ).state()
    except SyncplayMembershipConflict:
        raise HTTPException(409, "You already belong to an active Syncplay group.")
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    return JSONResponse(state, status_code=201)


@router.get("/api/syncplay/groups/{group_id}")
async def syncplay_group(group_id: str, request: Request):
    user, participant = _sync_identity(request)
    group = SyncplayGroup(group_id)
    state = group.state()
    if not state:
        raise HTTPException(404, "Group not found.")
    if not group.member(user, participant):
        raise HTTPException(403, "Join this group first.")
    return state


async def _sync_group_context(group_id: str, request: Request):
    user, participant = _sync_identity(request)
    group = SyncplayGroup(group_id)
    if not group.state():
        raise HTTPException(404, "Group not found.")
    if not group.member(user, participant):
        raise HTTPException(403, "Join this group first.")
    return (
        user,
        participant,
        group,
        await request.json()
        if request.headers.get("content-length", "0") != "0"
        else {},
    )


@router.post("/api/syncplay/groups/{group_id}/join")
async def syncplay_join(group_id: str, request: Request):
    user, participant = _sync_identity(request)
    group = SyncplayGroup(group_id)
    if not group.state():
        raise HTTPException(404, "Group not found.")
    data = (
        await request.json()
        if request.headers.get("content-length", "0") != "0"
        else {}
    )
    replaced = []
    try:

        def apply(cursor, state):
            cursor.execute(
                "SELECT 1 FROM syncplay_members m JOIN syncplay_groups g ON g.id=m.group_id WHERE m.user_id=? AND g.ended=0 AND m.group_id<>? LIMIT 1",
                (user, group_id),
            )
            if cursor.fetchone():
                raise SyncplayMembershipConflict
            cursor.execute(
                "SELECT participant_id FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id<>?",
                (group_id, user, participant),
            )
            replaced.extend(row[0] for row in cursor.fetchall())
            cursor.execute(
                "DELETE FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id<>?",
                (group_id, user, participant),
            )
            cursor.execute(
                "INSERT OR IGNORE INTO syncplay_members (group_id,user_id,participant_id,username) VALUES (?,?,?,?)",
                (
                    group_id,
                    user,
                    participant,
                    request.headers.get("x-zenstream-username", "ZenStream"),
                ),
            )
            group.transition(cursor, state)

        state = group.mutate(
            user, data.get("expectedRevision"), data.get("operationId"), apply
        )
    except SyncplayMembershipConflict:
        raise HTTPException(409, "You must leave your current Syncplay group first.")
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    for old_participant in replaced:
        for socket in await hub.sockets_for(user, old_participant):
            try:
                await socket.send_json(
                    {
                        "version": 1,
                        "type": "participant-replaced",
                        "id": group_id,
                        "revision": state["revision"],
                    }
                )
            except Exception:
                pass
    return state


@router.delete("/api/syncplay/groups/{group_id}")
async def syncplay_leave(group_id: str, request: Request):
    user, participant, group, data = await _sync_group_context(group_id, request)

    def apply(cursor, state):
        if state["hostUserId"] == user:
            group.transition(
                cursor,
                state,
                timeline=True,
                ended=1,
                playing=0,
                resume=0,
                playback_state="paused",
                effective_at=0,
                host_disconnected_at=None,
            )
        else:
            cursor.execute(
                "DELETE FROM syncplay_members WHERE group_id=? AND user_id=? AND participant_id=?",
                (group_id, user, participant),
            )
            group.transition(cursor, state)

    state = group.mutate(
        user, data.get("expectedRevision"), data.get("operationId"), apply
    )
    await hub.broadcast(
        {
            "version": 1,
            "type": "group-ended" if state["ended"] else "group",
            "id": group_id,
            "revision": state["revision"],
            "group": state,
        }
    )
    return Response(status_code=204)


@router.patch("/api/syncplay/groups/{group_id}")
async def syncplay_settings(group_id: str, request: Request):
    user, participant, group, data = await _sync_group_context(group_id, request)
    value = data.get("allowViewerControls")
    if not isinstance(value, bool):
        raise HTTPException(400, "allowViewerControls must be boolean.")

    def apply(cursor, state):
        if state["hostUserId"] != user:
            raise PermissionError
        group.transition(cursor, state, allow_controls=int(value))

    try:
        state = group.mutate(
            user, data.get("expectedRevision"), data.get("operationId"), apply
        )
    except PermissionError:
        raise HTTPException(403, "Only the host can change settings.")
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    return state


@router.delete("/api/syncplay/groups/{group_id}/members/{member_id}")
async def syncplay_remove_member(group_id: str, member_id: str, request: Request):
    user, participant, group, data = await _sync_group_context(group_id, request)

    def apply(cursor, state):
        if state["hostUserId"] != user:
            raise PermissionError
        if member_id == user:
            raise ValueError
        cursor.execute(
            "DELETE FROM syncplay_members WHERE group_id=? AND user_id=?",
            (group_id, member_id),
        )
        group.reconcile_readiness(cursor, state)

    try:
        state = group.mutate(
            user, data.get("expectedRevision"), data.get("operationId"), apply
        )
    except PermissionError:
        raise HTTPException(403, "Only the host can remove members.")
    except ValueError:
        raise HTTPException(400, "The host cannot remove themselves.")
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    return state


@router.post("/api/syncplay/groups/{group_id}/command")
async def syncplay_command(group_id: str, request: Request):
    user, participant, group, data = await _sync_group_context(group_id, request)
    position, action = data.get("position"), data.get("action")
    if (
        not isinstance(position, (int, float))
        or not math.isfinite(position)
        or position < 0
    ):
        raise HTTPException(400, "Invalid playback position.")
    if action not in {"media", "play", "pause", "seek"}:
        raise HTTPException(400, "Invalid playback command.")

    def apply(cursor, state):
        if user != state["hostUserId"] and not state["allowViewerControls"]:
            raise PermissionError
        item = data.get("itemId", state["itemId"])
        if action == "media":
            if not isinstance(item, str):
                raise ValueError
            generation = state["mediaGeneration"] + 1
            cursor.execute(
                "UPDATE syncplay_members SET watching_together=1 WHERE group_id=? AND participant_id=?",
                (group_id, participant),
            )
            cursor.execute(
                "UPDATE syncplay_members SET loading=CASE WHEN watching_together=1 AND viewing=1 THEN 1 ELSE 0 END,ready_generation=-1,presence_sequence=0 WHERE group_id=?",
                (group_id,),
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                item_id=item,
                position=float(position),
                playing=0,
                resume=1,
                media_generation=generation,
                anchor_position=float(position),
                anchor_time=time.time(),
                effective_at=0,
                playback_state="paused",
                pause_reason="readiness",
            )
            return
        if action == "seek":
            cursor.execute(
                "UPDATE syncplay_members SET loading=CASE WHEN watching_together=1 AND viewing=1 THEN 1 ELSE 0 END,ready_generation=-1 WHERE group_id=?",
                (group_id,),
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                position=float(position),
                playing=0,
                resume=1
                if bool(
                    data.get(
                        "playing",
                        state["playing"]
                        or state["resumeWhenReady"]
                        or state["playbackState"] == "playing",
                    )
                )
                else 0,
                anchor_position=float(position),
                anchor_time=time.time(),
                effective_at=0,
                playback_state="paused",
                pause_reason="seek",
            )
            return
        requested = bool(data.get("playing", state["playing"]))
        if action == "play":
            requested = True
        elif action == "pause":
            requested = False
        waiting = group.waiting_for_members(cursor, state["mediaGeneration"])
        if requested and not waiting:
            schedule(group, cursor, state, float(position))
        elif requested:
            group.transition(
                cursor,
                state,
                timeline=True,
                position=float(position),
                playing=0,
                resume=1,
                anchor_position=float(position),
                anchor_time=time.time(),
                effective_at=0,
                playback_state="paused",
                pause_reason="readiness",
            )
        else:
            pause(group, cursor, state, "command")

    try:
        state = group.mutate(
            user, data.get("expectedRevision"), data.get("operationId"), apply
        )
    except PermissionError:
        raise HTTPException(403, "Only the host can control playback.")
    except ValueError:
        raise HTTPException(400, "A media item is required.")
    except StaleSyncplayState as error:
        raise HTTPException(
            409,
            detail={"message": "Playback state is out of date.", "group": error.state},
        )
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    return state


@router.post("/api/syncplay/groups/{group_id}/presence")
async def syncplay_presence(group_id: str, request: Request):
    user, participant, group, data = await _sync_group_context(group_id, request)
    generation = data.get("mediaGeneration")
    timeline_revision = data.get("timelineRevision")
    sequence = data.get("presenceSequence")
    if (
        not isinstance(generation, int)
        or not isinstance(timeline_revision, int)
        or not isinstance(sequence, int)
    ):
        raise HTTPException(
            400,
            "mediaGeneration, timelineRevision, and presenceSequence are required.",
        )

    def apply(cursor, state):
        # A presence request can spend longer in the network than the seek or
        # playback transition that superseded it. Media generation protects
        # item changes; timeline revision protects seeks and play/pause changes
        # within the same item.
        group.apply_presence(
            cursor,
            state,
            user,
            participant,
            generation,
            timeline_revision,
            sequence,
            bool(data.get("viewing")),
            bool(data.get("loading")),
        )

    state = group.mutate(user, None, data.get("operationId"), apply)
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    return state


@router.post("/api/syncplay/groups/{group_id}/participation")
async def syncplay_participation(group_id: str, request: Request):
    user, participant, group, data = await _sync_group_context(group_id, request)
    watching = data.get("watchingTogether")
    if not isinstance(watching, bool):
        raise HTTPException(400, "watchingTogether must be boolean.")
    try:
        state = group.set_participation(
            user, participant, watching, data.get("operationId")
        )
    except StaleSyncplayState as error:
        raise HTTPException(
            409,
            detail={"message": "Playback state is out of date.", "group": error.state},
        )
    await hub.broadcast({"version": 1, "type": "group", "group": state})
    return state


@router.websocket("/api/ws/syncplay")
async def syncplay_socket(websocket: WebSocket):
    participant = websocket.headers.get(
        "x-zenstream-participant"
    ) or websocket.query_params.get("participantId")
    account = websocket_account(websocket)
    user_id = account["id"] if account else None
    if not user_id or not participant:
        await websocket.close(code=1008)
        return
    await hub.connect(websocket, user_id, participant)
    try:
        for group in SyncplayGroup.active_groups_for_user(user_id, participant):
            state = group.clear_host_disconnected()
            if state:
                await _broadcast_group(state)
        rows = SyncplayGroup("_").db.execute(
            "SELECT id FROM syncplay_groups WHERE ended=0 ORDER BY updated DESC", ()
        )
        await websocket.send_json(
            {
                "version": 1,
                "type": "groups",
                "groups": [
                    _redact_syncplay_state(
                        SyncplayGroup(row[0]).state(), user_id, participant
                    )
                    for row in rows
                ],
            }
        )
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "clock":
                received = time.time()
                await websocket.send_json(
                    {
                        "version": 1,
                        "type": "clock",
                        "clientSentAt": message.get("clientSentAt"),
                        "serverReceivedAt": received,
                        "serverSentAt": time.time(),
                    }
                )
    except WebSocketDisconnect:
        pass
    finally:
        identity, epoch = await hub.remove(websocket)
        if identity and not await hub.sockets_for(*identity):
            asyncio.create_task(_disconnect_cleanup(*identity, epoch))
