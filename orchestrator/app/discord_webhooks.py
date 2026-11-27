from __future__ import annotations

import asyncio
import json
import logging
import math
import mimetypes
import re
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from app.config import Config
from app.models.metadata import _fernet
from cryptography.fernet import InvalidToken
from sqlalchemy.exc import SQLAlchemyError

EVENTS = {
    "user.login": "User signed in",
    "user.logout": "User signed out",
    "playback.started": "Video playback started",
    "playback.paused": "Video paused",
    "playback.resumed": "Video resumed",
    "playback.progress": "Video progress milestone",
    "playback.completed": "Video reached 90%",
    "playback.stopped": "Video playback ended",
    "playback.disconnected": "Video viewer disconnected",
    "music.started": "Track playback started",
    "library.episode_added": "Episode added",
    "library.movie_added": "Movie added",
    "library.album_added": "Album added",
}
_LOG = logging.getLogger(__name__)
_URL = re.compile(
    r"https://(?:discord\.com|discordapp\.com)/api/(?:v\d+/)?webhooks/"
    r"([0-9]{1,24})/([A-Za-z0-9_-]{20,256})"
)
_QUEUE_LIMIT = 1000
_MAX_AGE = 86400


def settings(db):
    rows = db.read_execute(
        "SELECT w.id,w.name,w.events,w.last_result,EXISTS ("
        "SELECT 1 FROM discord_webhook_deliveries d WHERE d.webhook_id=w.id AND d.kind='test') "
        "FROM discord_webhooks w ORDER BY w.name COLLATE NOCASE,w.id"
    )
    return {
        "events": EVENTS,
        "webhooks": [
            {
                "id": hook,
                "name": name,
                "events": json.loads(events),
                "lastResult": result,
                "testPending": bool(test_pending),
            }
            for hook, name, events, result, test_pending in rows
        ],
    }


def save_webhook(db, hook_id, body):
    name = body.name.strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("Enter a name between 1 and 80 characters.")
    if any(event not in EVENTS for event in body.events):
        raise ValueError("Select events from the available list.")
    url = discord_id = None
    if body.url is not None:
        match = _URL.fullmatch(body.url.strip())
        if not match:
            raise ValueError("Enter a Discord incoming webhook URL.")
        discord_id = match[1]
        url = f"https://discord.com/api/webhooks/{discord_id}/{match[2]}"
    ciphertext = _fernet().encrypt(url.encode()).decode("ascii") if url else None
    events = json.dumps(list(dict.fromkeys(body.events)))
    with db.transaction() as cursor:
        if hook_id is None:
            if ciphertext is None:
                raise ValueError("Enter a Discord incoming webhook URL.")
            hook_id = str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO discord_webhooks(id,name,url_ciphertext,discord_id,events) VALUES(?,?,?,?,?)",
                (hook_id, name, ciphertext, discord_id, events),
            )
        else:
            if not cursor.execute(
                "UPDATE discord_webhooks SET name=?,events=? WHERE id=?",
                (name, events, hook_id),
            ).rowcount:
                raise LookupError
            if ciphertext:
                cursor.execute(
                    "UPDATE discord_webhooks SET url_ciphertext=?,discord_id=?,"
                    "last_result=NULL,blocked=0,send_after=0 WHERE id=?",
                    (ciphertext, discord_id, hook_id),
                )
                cursor.execute(
                    "DELETE FROM discord_webhook_deliveries WHERE webhook_id=?",
                    (hook_id,),
                )
            else:
                cursor.execute(
                    "DELETE FROM discord_webhook_deliveries WHERE webhook_id=? "
                    "AND kind<>'test' AND kind NOT IN (SELECT value FROM json_each(?))",
                    (hook_id, events),
                )
    return settings(db)


def remove_webhook(db, hook_id):
    with db.transaction() as cursor:
        if not cursor.execute(
            "DELETE FROM discord_webhooks WHERE id=?", (hook_id,)
        ).rowcount:
            raise LookupError
        cursor.execute(
            "DELETE FROM discord_webhook_deliveries WHERE webhook_id=?", (hook_id,)
        )


def _enqueue(cursor, kind, payload, hook_id=None):
    now = time.time()
    target = (
        "w.id=?"
        if hook_id
        else "w.blocked=0 AND ? IN (SELECT value FROM json_each(w.events))"
    )
    cursor.execute(
        "INSERT INTO discord_webhook_deliveries(webhook_id,kind,payload,created,due) "
        "SELECT w.id,?,?,?,? FROM discord_webhooks w WHERE "
        + target
        + " AND (SELECT COUNT(*) FROM discord_webhook_deliveries d WHERE d.webhook_id=w.id)<?",
        (
            kind,
            json.dumps(payload, allow_nan=False),
            now,
            now,
            hook_id or kind,
            _QUEUE_LIMIT,
        ),
    )
    return cursor.rowcount


def queue_test(db, hook_id):
    with db.transaction() as cursor:
        if not cursor.execute(
            "SELECT id FROM discord_webhooks WHERE id=?", (hook_id,)
        ).fetchone():
            raise LookupError
        if not _enqueue(cursor, "test", {}, hook_id):
            raise ValueError("This webhook's delivery queue is full.")
        cursor.execute(
            "UPDATE discord_webhooks SET last_result='Test message queued',blocked=0 WHERE id=?",
            (hook_id,),
        )


def publish(kind, *, db=None, cursor=None, **payload):
    """Record an event without letting optional delivery affect the source operation."""
    try:
        if cursor is not None:
            _enqueue(cursor, kind, payload)
        else:
            with (db or Config().database).transaction() as transaction:
                _enqueue(transaction, kind, payload)
    except sqlite3.OperationalError as error:
        if "no such table: discord_" not in str(error):
            _LOG.warning("Could not queue Discord event %s", kind)
    except (sqlite3.Error, RuntimeError, TypeError, ValueError):
        _LOG.warning("Could not queue Discord event %s", kind)


def viewer_notifications_enabled(cursor):
    try:
        return (
            cursor.execute(
                "SELECT 1 FROM discord_webhooks w,json_each(w.events) e "
                "WHERE w.blocked=0 AND e.value LIKE 'playback.%' LIMIT 1"
            ).fetchone()
            is not None
        )
    except sqlite3.OperationalError as error:
        if "no such table: discord_webhooks" not in str(error):
            _LOG.warning("Could not read Discord webhook subscriptions")
        return False


def record_viewer_sample(
    cursor, viewer_id, user_id, entity_id, position, duration, paused, mode
):
    preference = cursor.execute(
        "SELECT watch_history_enabled FROM account_preferences WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if preference and not preference[0]:
        cursor.execute(
            "DELETE FROM discord_viewer_activity WHERE viewer_id=?", (viewer_id,)
        )
        return
    if not duration or not math.isfinite(duration) or not math.isfinite(position):
        position = position if math.isfinite(position) else 0
        duration = None
    old = cursor.execute(
        "SELECT paused,milestone FROM discord_viewer_activity WHERE viewer_id=?",
        (viewer_id,),
    ).fetchone()
    if old is None and paused:
        return
    payload = {
        "user": user_id,
        "entity": entity_id,
        "position": position,
        "duration": duration,
        "mode": mode,
    }
    percent = 100 * position / duration if duration else 0
    reached = max((mark for mark in (25, 50, 75, 90) if percent >= mark), default=0)
    if old is None:
        cursor.execute(
            "INSERT INTO discord_viewer_activity(viewer_id,paused,milestone) VALUES(?,?,?)",
            (viewer_id, paused, reached),
        )
        publish("playback.started", cursor=cursor, **payload)
        return
    if bool(old[0]) != paused:
        publish(
            "playback.paused" if paused else "playback.resumed",
            cursor=cursor,
            **payload,
        )
    if not paused and reached > old[1]:
        if reached == 90:
            publish("playback.completed", cursor=cursor, **payload)
        else:
            publish("playback.progress", cursor=cursor, milestone=reached, **payload)
    cursor.execute(
        "UPDATE discord_viewer_activity SET paused=?,milestone=? WHERE viewer_id=?",
        (paused, old[1] if paused else max(old[1], reached), viewer_id),
    )


def finish_viewer(cursor, viewer_id, kind, **payload):
    try:
        tracked = cursor.execute(
            "DELETE FROM discord_viewer_activity WHERE viewer_id=?", (viewer_id,)
        ).rowcount
    except sqlite3.OperationalError as error:
        if "no such table: discord_viewer_activity" not in str(error):
            _LOG.warning("Could not close Discord viewer activity")
        return
    if tracked:
        if not math.isfinite(payload.get("position") or 0):
            payload["position"] = 0
        if not math.isfinite(payload.get("duration") or 0):
            payload["duration"] = None
        publish(kind, cursor=cursor, **payload)


def _metadata_projection_table(db):
    rows = db.read_execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('catalog_item_projection','catalog_metadata_projection') "
        "ORDER BY CASE name WHEN 'catalog_item_projection' THEN 0 ELSE 1 END LIMIT 1"
    )
    return rows[0][0] if rows else None


def _entity(db, entity_id, projection_table):
    rows = db.read_execute(
        "SELECT entity_type,parent_id,relative_path,season_number,episode_number "
        "FROM library_entities WHERE id=?",
        (entity_id,),
    )
    if not rows:
        return None
    kind, parent, path, season, number = rows[0]
    projected = (
        db.read_execute(
            f"SELECT payload FROM {projection_table} WHERE entity_id=? "
            "ORDER BY (locale='en') DESC,locale LIMIT 1",
            (entity_id,),
        )
        if projection_table
        else []
    )
    try:
        payload = json.loads(projected[0][0] or "{}") if projected else {}
    except ValueError:
        payload = {}
    title = (
        (payload.get("title") if isinstance(payload, dict) else None)
        or Path(path or "").stem
        or kind.title()
    )
    return kind, parent, title, season, number


def _artwork(db, candidates):
    for entity_id, image_types in candidates:
        for image_type in image_types:
            rows = db.read_execute(
                "SELECT local_path FROM catalog_artwork_selection "
                "WHERE entity_id=? AND image_type=? COLLATE NOCASE AND local_path IS NOT NULL "
                "ORDER BY (locale='en') DESC,locale LIMIT 1",
                (entity_id, image_type),
            )
            if not rows:
                continue
            path = Path(rows[0][0])
            media_type = mimetypes.guess_type(path.name)[0]
            if media_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                continue
            try:
                with path.open("rb") as image:
                    data = image.read(2_000_001)
            except OSError:
                continue
            if 0 < len(data) <= 2_000_000:
                return ("artwork" + path.suffix.lower(), data, media_type)
    return None


def _media(db, entity_id):
    projection_table = _metadata_projection_table(db)
    entity = _entity(db, entity_id, projection_table)
    if entity is None:
        return "Media item", [], None
    kind, parent, title, season, number = entity
    fields = []
    candidates = [(entity_id, ("primary", "backdrop"))]
    if kind == "episode":
        candidates = [(entity_id, ("thumb", "primary", "backdrop"))]
        parent_item = _entity(db, parent, projection_table)
        if parent_item and parent_item[0] == "season":
            parent, parent_item = (
                parent_item[1],
                _entity(db, parent_item[1], projection_table),
            )
        if parent_item and parent_item[0] == "series":
            fields.append(("Series", parent_item[2]))
            candidates.append((parent, ("primary", "backdrop")))
        if season is not None:
            fields.append(("Season", str(season)))
        if number is not None:
            fields.append(("Episode", str(number)))
    elif kind == "track":
        album = _entity(db, parent, projection_table)
        if album and album[0] == "release":
            fields.append(("Album", album[2]))
            candidates.insert(0, (parent, ("primary",)))
            artist = _entity(db, album[1], projection_table)
            if artist and artist[0] == "artist":
                fields.append(("Artist", artist[2]))
    elif kind == "release":
        artist = _entity(db, parent, projection_table)
        if artist and artist[0] == "artist":
            fields.append(("Artist", artist[2]))
    return title, fields, _artwork(db, candidates)


def _clock(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


_EVENT_COLORS = {
    "playback.progress": 0xD1D5DB,
    "user.logout": 0xED4245,
    "playback.stopped": 0xED4245,
    "playback.disconnected": 0xED4245,
    "playback.paused": 0xF59E42,
    "playback.started": 0x57F287,
    "playback.resumed": 0x57F287,
    "playback.completed": 0x57F287,
    "music.started": 0x57F287,
}

_MEDIA_COPY = {
    "playback.started": "Started watching",
    "playback.paused": "Paused",
    "playback.resumed": "Resumed watching",
    "playback.progress": "Watch progress for",
    "playback.completed": "Reached the watch threshold for",
    "playback.stopped": "Ended the viewing session for",
    "playback.disconnected": "Lost contact with the player for",
    "music.started": "Started listening to",
    "library.episode_added": "New episode available",
    "library.movie_added": "New movie available",
    "library.album_added": "New album available",
}


def build_message(db, kind, payload, created):
    fields = []
    artwork = None
    title = EVENTS.get(kind, "Webhook test")
    description = "ZenStream can deliver messages to this channel."
    color = _EVENT_COLORS.get(kind, 0x44C7EA)
    if user_id := payload.get("user"):
        rows = db.read_execute("SELECT username FROM users WHERE id=?", (user_id,))
        fields.append(("User", rows[0][0] if rows else "Former user"))
    if entity_id := payload.get("entity"):
        try:
            media_title, media_fields, artwork = _media(db, entity_id)
        except (sqlite3.Error, SQLAlchemyError, ValueError, TypeError) as error:
            _LOG.warning(
                "Could not load media details for Discord event error_type=%s",
                type(error).__name__,
            )
            media_title, media_fields, artwork = "Media item", [], None
        description = f"{_MEDIA_COPY[kind]}\n{media_title}"
        fields += media_fields
    if kind == "user.login":
        description = "A new ZenStream session was created."
        fields.append(("Client", payload.get("client", "ZenStream")))
    elif kind == "user.logout":
        description = "The user ended their ZenStream session."
    elif kind == "playback.started":
        mode = payload.get("mode")
        if mode:
            fields.append(("Playback mode", mode.replace("-", " ").title()))
    elif kind == "playback.progress":
        fields.append(("Milestone", f"{payload['milestone']}%"))
    elif kind == "playback.completed":
        fields.append(("Completion", "Reached the 90% playback threshold"))
    elif kind == "playback.disconnected":
        fields.append(("Reason", "The player stopped reporting activity"))
    if (
        kind.startswith("playback.")
        and payload.get("duration")
        and math.isfinite(payload["duration"])
        and math.isfinite(payload["position"])
    ):
        fields.append(
            (
                "Position",
                f"{_clock(payload['position'])} / {_clock(payload['duration'])}",
            )
        )
    embed = {
        "title": title,
        "description": str(description)[:512],
        "color": color,
        "timestamp": datetime.fromtimestamp(created, UTC).isoformat(),
        "footer": {"text": "ZenStream"},
    }
    if fields:
        embed["fields"] = [
            {"name": key, "value": str(value)[:256], "inline": True}
            for key, value in fields
            if value
        ]
    message = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if artwork:
        embed["thumbnail"] = {"url": f"attachment://{artwork[0]}"}
        message["attachments"] = [{"id": 0, "filename": artwork[0]}]
    return message, artwork


def _claim(db):
    now = time.time()
    ready = db.read_execute(
        "SELECT 1 FROM discord_webhook_deliveries d JOIN discord_webhooks w ON w.id=d.webhook_id "
        "WHERE d.due<=? AND w.send_after<=? AND w.blocked=0 LIMIT 1",
        (now, now),
    )
    if not ready:
        return None
    with db.transaction() as cursor:
        row = cursor.execute(
            "SELECT d.id,d.webhook_id,d.kind,d.payload,d.created,d.attempts,w.url_ciphertext "
            "FROM discord_webhook_deliveries d JOIN discord_webhooks w ON w.id=d.webhook_id "
            "WHERE d.due<=? AND w.send_after<=? AND w.blocked=0 ORDER BY d.created,d.id LIMIT 1",
            (now, now),
        ).fetchone()
        if row:
            cursor.execute(
                "UPDATE discord_webhook_deliveries SET due=? WHERE id=?",
                (now + 120, row[0]),
            )
            cursor.execute(
                "UPDATE discord_webhooks SET send_after=? WHERE id=?",
                (now + 120, row[1]),
            )
        return row


def _settle(db, row, result, delay, retry, disable=False):
    delivery, hook, _, _, created, attempts, ciphertext = row
    now = time.time()
    with db.transaction() as cursor:
        if not cursor.execute(
            "UPDATE discord_webhooks SET send_after=?,last_result=?,blocked=? WHERE id=? AND url_ciphertext=?",
            (now + max(1, delay), result, int(disable), hook, ciphertext),
        ).rowcount:
            return
        if retry and attempts < 7 and now + delay - created < _MAX_AGE:
            cursor.execute(
                "UPDATE discord_webhook_deliveries SET due=?,attempts=attempts+1 WHERE id=?",
                (now + delay, delivery),
            )
        else:
            cursor.execute(
                "DELETE FROM discord_webhook_deliveries WHERE id=?", (delivery,)
            )
        if disable:
            cursor.execute(
                "DELETE FROM discord_webhook_deliveries WHERE webhook_id=?", (hook,)
            )


def _seconds(value, default):
    try:
        number = float(value)
        return max(1, number) if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


async def _deliver(db, client, row):
    _, _, kind, payload, created, attempts, ciphertext = row
    if time.time() - created >= _MAX_AGE:
        await asyncio.to_thread(_settle, db, row, "Queued message expired", 1, False)
        return 0
    try:
        url = _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        await asyncio.to_thread(
            _settle,
            db,
            row,
            "Saved URL could not be read. Enter the URL again.",
            1,
            False,
            True,
        )
        return 0
    message, artwork = await asyncio.to_thread(
        build_message, db, kind, json.loads(payload), created
    )
    try:
        if artwork:
            response = await client.post(
                url,
                params={"wait": "true"},
                data={"payload_json": json.dumps(message)},
                files={"files[0]": artwork},
            )
        else:
            response = await client.post(url, params={"wait": "true"}, json=message)
    except httpx.HTTPError:
        await asyncio.to_thread(
            _settle, db, row, "Discord connection failed", 2 ** (attempts + 1), True
        )
        return 0
    status = response.status_code
    label = "Test message delivered" if kind == "test" else "Message delivered"
    delay, retry, global_wait = 1, False, 0
    if status == 429:
        try:
            details = response.json()
        except ValueError:
            details = {}
        if not isinstance(details, dict):
            details = {}
        delay = _seconds(
            response.headers.get("Retry-After", details.get("retry_after")), 5
        )
        if details.get("global") or response.headers.get("X-RateLimit-Global"):
            global_wait = delay
        label, retry = "Discord asked ZenStream to wait", True
    elif status >= 500:
        label, delay, retry = (
            "Discord temporarily unavailable",
            2 ** (attempts + 1),
            True,
        )
    elif not response.is_success:
        label = f"Discord rejected the message (HTTP {status})"
    elif response.headers.get("X-RateLimit-Remaining") == "0":
        delay = _seconds(response.headers.get("X-RateLimit-Reset-After"), 1)
    await asyncio.to_thread(
        _settle, db, row, label, delay, retry, status in (401, 403, 404)
    )
    return global_wait


class _HideWebhookRequests(logging.Filter):
    def filter(self, record):
        return "https://discord.com/api/webhooks/" not in record.getMessage()


async def run_dispatcher():
    logging.getLogger("httpx").addFilter(_hide_webhook_requests)
    try:
        await _dispatch()
    finally:
        logging.getLogger("httpx").removeFilter(_hide_webhook_requests)


_hide_webhook_requests = _HideWebhookRequests()


async def _dispatch():
    # Redirects and environment proxies could send the webhook credential to a different host.
    async with httpx.AsyncClient(
        timeout=10, follow_redirects=False, trust_env=False
    ) as client:
        while True:
            try:
                db = Config().database
                row = await asyncio.to_thread(_claim, db)
                if row is not None:
                    wait = await _deliver(db, client, row)
                    await asyncio.sleep(max(wait, 0.25))
                    continue
            except Exception:  # noqa: BLE001 - keep optional delivery alive
                # HTTP exceptions can contain the token-bearing URL.
                _LOG.warning("Discord webhook delivery worker will retry")
            await asyncio.sleep(1)
