from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from app.config import Config
from app.logging_config import get_logger
from app.metadata_domain import fallback_tiers, language_family, locale_variants
from fastapi import HTTPException

try:  # Web Push is optional; in-app notifications must work without it.
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover - exercised in minimal installations
    WebPushException = Exception  # type: ignore[assignment,misc]
    webpush = None  # type: ignore[assignment]


logger = get_logger("notifications")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return str(uuid.uuid4())


def _table_exists(db, name: str) -> bool:
    rows = db.read_execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return isinstance(rows, (list, tuple)) and bool(rows)


class FollowService:
    """Resolve catalog and calendar items to one durable Follow identity."""

    def __init__(self, database=None):
        self.db = database or Config().database

    def _require_tables(self) -> None:
        if not _table_exists(self.db, "user_follow_targets"):
            raise HTTPException(503, "Follow support is not available yet.")

    def _entity_row(self, entity_id: str):
        rows = self.db.execute(
            "SELECT id,library_id,parent_id,entity_type FROM library_entities WHERE id=?",
            (entity_id,),
        )
        return rows[0] if rows else None

    def _series_for_entity(self, entity_id: str):
        row = self._entity_row(entity_id)
        if not row:
            return None
        seen: set[str] = set()
        while row and row[0] not in seen:
            seen.add(row[0])
            if row[3] == "series":
                return row
            if row[2] is None:
                return None
            row = self._entity_row(row[2])
        return None

    def _identity_for_entity(self, user_id: str, entity_id: str) -> dict:
        row = self._entity_row(entity_id)
        if not row:
            raise HTTPException(404, "Item not found.")
        access = self.db.execute(
            "SELECT 1 FROM user_library_access WHERE user_id=? AND library_id=?",
            (user_id, row[1]),
        )
        if not access:
            raise HTTPException(404, "Item not found.")

        target = row
        if row[3] == "episode":
            target = self._series_for_entity(entity_id)
            if not target:
                raise HTTPException(404, "Series not found.")
        if target[3] not in {"movie", "series"}:
            raise HTTPException(
                400, "Only movies, series, and episodes can be followed."
            )

        target_type = target[3]
        preferred = "tmdb" if target_type == "movie" else "tvdb"
        preferred_type = "movie" if target_type == "movie" else "series"
        rows = self.db.execute(
            "SELECT provider,provider_id FROM entity_provider_ids "
            "WHERE entity_id=? AND provider=? AND identifier_type=? "
            "ORDER BY is_primary DESC,provider_id LIMIT 1",
            (target[0], preferred, preferred_type),
        )
        if rows:
            provider, provider_id = rows[0]
        else:
            rows = self.db.execute(
                "SELECT provider,provider_id FROM entity_provider_ids "
                "WHERE entity_id=? AND provider IN ('tmdb','tvdb') AND identifier_type=? "
                "ORDER BY is_primary DESC,provider,provider_id LIMIT 1",
                (target[0], preferred_type),
            )
            if rows:
                provider, provider_id = rows[0]
            else:
                provider, provider_id = "entity", target[0]
        return {
            "library_id": target[1],
            "target_type": target_type,
            "provider": provider,
            "provider_id": str(provider_id),
            "entity_id": target[0],
        }

    def _set_identity(
        self,
        user_id: str,
        identity: dict,
        following: bool,
    ) -> bool:
        self._require_tables()
        library_id = identity["library_id"]
        if not self.db.execute(
            "SELECT 1 FROM user_library_access WHERE user_id=? AND library_id=?",
            (user_id, library_id),
        ):
            raise HTTPException(404, "Item not found.")
        now = _now()
        with self.db.transaction() as cursor:
            if following:
                entity_id = identity.get("entity_id")
                existing = (
                    cursor.execute(
                        "SELECT id FROM user_follow_targets WHERE user_id=? AND library_id=? "
                        "AND target_type=? AND entity_id=? LIMIT 1",
                        (user_id, library_id, identity["target_type"], entity_id),
                    ).fetchone()
                    if entity_id
                    else None
                )
                if existing:
                    cursor.execute(
                        "UPDATE user_follow_targets SET provider=?,provider_id=?,updated_at=? WHERE id=?",
                        (
                            identity["provider"],
                            identity["provider_id"],
                            now,
                            existing[0],
                        ),
                    )
                else:
                    cursor.execute(
                        "INSERT INTO user_follow_targets "
                        "(id,user_id,library_id,target_type,provider,provider_id,entity_id,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(user_id,library_id,target_type,provider,provider_id) DO UPDATE SET "
                        "entity_id=excluded.entity_id,updated_at=excluded.updated_at",
                        (
                            _id(),
                            user_id,
                            library_id,
                            identity["target_type"],
                            identity["provider"],
                            identity["provider_id"],
                            entity_id,
                            now,
                            now,
                        ),
                    )
            else:
                cursor.execute(
                    "DELETE FROM user_follow_targets WHERE user_id=? AND library_id=? "
                    "AND target_type=? AND ((provider=? AND provider_id=?) OR entity_id=?)",
                    (
                        user_id,
                        library_id,
                        identity["target_type"],
                        identity["provider"],
                        identity["provider_id"],
                        identity.get("entity_id"),
                    ),
                )
        return following

    def set_for_entity(self, user_id: str, entity_id: str, following: bool) -> bool:
        return self._set_identity(
            user_id,
            self._identity_for_entity(user_id, entity_id),
            following,
        )

    def following_for_entity(self, user_id: str, entity_id: str) -> bool:
        if not _table_exists(self.db, "user_follow_targets"):
            return False
        identity = self._identity_for_entity(user_id, entity_id)
        return bool(
            self.db.execute(
                "SELECT 1 FROM user_follow_targets WHERE user_id=? AND library_id=? "
                "AND target_type=? AND ((provider=? AND provider_id=?) OR entity_id=?) LIMIT 1",
                (
                    user_id,
                    identity["library_id"],
                    identity["target_type"],
                    identity["provider"],
                    identity["provider_id"],
                    identity["entity_id"],
                ),
            )
        )

    def following_for_identity(
        self,
        user_id: str,
        library_id: str,
        target_type: str,
        provider: str | None,
        provider_id: str | None,
        entity_id: str | None = None,
    ) -> bool:
        if not _table_exists(self.db, "user_follow_targets"):
            return False
        clauses = []
        params: list[str] = [user_id, library_id, target_type]
        if provider and provider_id:
            clauses.append("(provider=? AND provider_id=?)")
            params.extend([provider, str(provider_id)])
        if entity_id:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if not clauses:
            return False
        return bool(
            self.db.execute(
                "SELECT 1 FROM user_follow_targets WHERE user_id=? AND library_id=? "
                "AND target_type=? AND (" + " OR ".join(clauses) + ") LIMIT 1",
                params,
            )
        )

    def _calendar_linked_entity(self, event_id: str, kind: str):
        expected_type = "movie" if kind == "movie" else "episode"
        rows = self.db.execute(
            "SELECT e.id,e.library_id,e.parent_id,e.entity_type FROM calendar_event_entities x "
            "JOIN library_entities e ON e.id=x.entity_id "
            "WHERE x.event_id=? AND e.entity_type=? LIMIT 1",
            (event_id, expected_type),
        )
        return rows[0] if rows else None

    def set_for_calendar_event(
        self, user_id: str, event_id: str, following: bool
    ) -> bool:
        self._require_tables()
        rows = self.db.execute(
            "SELECT id,library_id,kind,tvdb_id,tmdb_id,series_tvdb_id FROM calendar_events WHERE id=?",
            (event_id,),
        )
        if not rows:
            raise HTTPException(404, "Calendar event not found.")
        event_id_value, library_id, kind, tvdb_id, tmdb_id, series_tvdb_id = rows[0]
        if not self.db.execute(
            "SELECT 1 FROM user_library_access WHERE user_id=? AND library_id=?",
            (user_id, library_id),
        ):
            raise HTTPException(404, "Calendar event not found.")

        target_type = "movie" if kind == "movie" else "series"
        provider = "tmdb" if kind == "movie" else "tvdb"
        provider_id = tmdb_id if kind == "movie" else series_tvdb_id
        linked = self._calendar_linked_entity(event_id_value, kind)
        linked_target_id = linked[0] if linked else None
        if kind == "episode" and linked:
            series = self._series_for_entity(linked[0])
            linked_target_id = series[0] if series else None
        if not provider_id and linked_target_id:
            identity = self._identity_for_entity(user_id, linked_target_id)
            provider = identity["provider"]
            provider_id = identity["provider_id"]
        if not provider_id:
            raise HTTPException(
                400, "This calendar event has no followable provider identity."
            )
        return self._set_identity(
            user_id,
            {
                "library_id": library_id,
                "target_type": target_type,
                "provider": provider,
                "provider_id": str(provider_id),
                "entity_id": linked_target_id,
            },
            following,
        )


class NotificationService:
    def __init__(self, database=None):
        self.db = database or Config().database

    @staticmethod
    def push_config() -> dict:
        public_key = os.getenv("WEB_PUSH_VAPID_PUBLIC_KEY", "").strip()
        private_key = os.getenv("WEB_PUSH_VAPID_PRIVATE_KEY", "").strip()
        subject = os.getenv("WEB_PUSH_VAPID_SUBJECT", "").strip()
        return {
            "configured": bool(public_key and private_key and subject),
            "publicKey": public_key or None,
        }

    @staticmethod
    def _fetch(executor, query: str, params=()):
        result = executor.execute(query, params)
        return result.fetchall() if hasattr(result, "fetchall") else result

    @classmethod
    def _has_table(cls, executor, name: str) -> bool:
        return bool(
            cls._fetch(
                executor,
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            )
        )

    @classmethod
    def _configured_metadata_locales(cls, executor) -> list[str]:
        if not cls._has_table(executor, "metadata_settings"):
            return ["en"]
        try:
            rows = cls._fetch(
                executor,
                "SELECT value FROM metadata_settings WHERE key='locales'",
            )
            values = json.loads(rows[0][0]) if rows else ["en"]
        except (TypeError, ValueError, json.JSONDecodeError):
            return ["en"]
        if not isinstance(values, list):
            return ["en"]
        locales: list[str] = []
        for value in values:
            locale = str(value or "").strip()
            if locale and locale.casefold() not in {
                item.casefold() for item in locales
            }:
                locales.append(locale)
        return locales or ["en"]

    @classmethod
    def _user_locales(
        cls, executor, user_id: str
    ) -> tuple[str, str, list[str]]:
        configured = cls._configured_metadata_locales(executor)
        metadata_language = None
        interface_locale = "en"
        if cls._has_table(executor, "account_preferences"):
            try:
                rows = cls._fetch(
                    executor,
                    "SELECT metadata_language,locale FROM account_preferences WHERE user_id=?",
                    (user_id,),
                )
            except Exception:
                rows = []
            if rows:
                metadata_language, interface_locale = rows[0]
                interface_locale = str(interface_locale or "en")

        explicit = next(
            (
                value
                for value in configured
                if metadata_language is not None
                and value.casefold() == str(metadata_language).casefold()
            ),
            None,
        )
        if explicit:
            return explicit, interface_locale, configured

        metadata_language = next(
            (
                value
                for value in configured
                if value.casefold() == interface_locale.casefold()
            ),
            None,
        )
        if metadata_language is None:
            interface_family = language_family(interface_locale)
            metadata_language = next(
                (
                    value
                    for value in configured
                    if language_family(value) == interface_family
                ),
                None,
            )
        return metadata_language or configured[0], interface_locale, configured

    @staticmethod
    def _notification_label(kind: str, interface_locale: str) -> str:
        if language_family(interface_locale) == "ja":
            return "新しいエピソード" if kind == "new_episode" else "新しい映画が追加されました"
        return "New episode" if kind == "new_episode" else "New movie added"

    @classmethod
    def _projection_title(
        cls,
        executor,
        entity_id: str | None,
        fallback: str,
        locale: str = "en",
        configured: list[str] | None = None,
    ) -> str:
        if not entity_id:
            return fallback
        if not cls._has_table(executor, "catalog_item_projection"):
            return fallback
        rows = cls._fetch(
            executor,
            "SELECT locale,payload FROM catalog_item_projection WHERE entity_id=?",
            (entity_id,),
        )
        payloads: dict[str, dict] = {}
        for row_locale, raw_payload in rows:
            try:
                payload = json.loads(raw_payload or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                payloads[str(row_locale)] = payload
        if not payloads:
            return fallback

        original = next(
            (
                str(payload["originalLanguage"])
                for payload in payloads.values()
                if payload.get("originalLanguage")
            ),
            None,
        )
        configured = configured or cls._configured_metadata_locales(executor)
        tiers = fallback_tiers(
            locale,
            original,
            media=False,
            include_english=any(language_family(value) == "en" for value in configured),
        )
        for tier in tiers:
            for candidate_locale in locale_variants(tier, payloads):
                title = payloads[candidate_locale].get("title")
                if title:
                    return str(title)

        for candidate_locale in sorted(payloads, key=str.casefold):
            title = payloads[candidate_locale].get("title")
            if title:
                return str(title)
        return fallback

    @classmethod
    def _notification_copy(
        cls,
        executor,
        kind: str,
        entity_id: str | None,
        series_id: str | None,
        stored_title: str | None,
        stored_subtitle: str | None,
        season: int | None,
        episode: int | None,
        metadata_locale: str,
        interface_locale: str,
        configured: list[str],
    ) -> tuple[str, str]:
        if kind == "new_episode":
            series_fallback = str(stored_title or "")
            for prefix in ("New episode: ", "新しいエピソード: "):
                if series_fallback.startswith(prefix):
                    series_fallback = series_fallback[len(prefix) :]
                    break
            episode_fallback = str(stored_subtitle or "")
            if " — " in episode_fallback:
                episode_fallback = episode_fallback.split(" — ", 1)[1]
            series_title = cls._projection_title(
                executor,
                series_id,
                series_fallback or "Series",
                metadata_locale,
                configured,
            )
            episode_title = cls._projection_title(
                executor,
                entity_id,
                episode_fallback or "Episode",
                metadata_locale,
                configured,
            )
            try:
                position = (
                    f"S{int(season):02d}E{int(episode):02d}"
                    if season is not None and episode is not None
                    else "New episode"
                )
            except (TypeError, ValueError):
                position = "New episode"
            return (
                f"{cls._notification_label(kind, interface_locale)}: {series_title}",
                f"{position} — {episode_title}",
            )

        title = cls._projection_title(
            executor,
            entity_id,
            str(stored_title or "New movie"),
            metadata_locale,
            configured,
        )
        return title, cls._notification_label(kind, interface_locale)

    def list(self, user_id: str, limit: int = 50, cursor: str | None = None) -> dict:
        limit = max(1, min(100, int(limit)))
        try:
            offset = max(0, int(cursor or 0))
        except (TypeError, ValueError) as error:
            raise HTTPException(400, "Invalid notification cursor.") from error
        if not _table_exists(self.db, "notifications"):
            return {"items": [], "unreadCount": 0, "nextCursor": None}
        rows = self.db.execute(
            "SELECT id,kind,title,subtitle,entity_id,series_id,season_number,episode_number,"
            "created_at,read_at,navigation_path FROM notifications "
            "WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
            (user_id, limit + 1, offset),
        )
        metadata_locale, interface_locale, configured = self._user_locales(
            self.db, user_id
        )
        items = []
        for row in rows[:limit]:
            title, subtitle = self._notification_copy(
                self.db,
                row[1],
                row[4],
                row[5],
                row[2],
                row[3],
                row[6],
                row[7],
                metadata_locale,
                interface_locale,
                configured,
            )
            items.append(
                {
                    "id": row[0],
                    "kind": row[1],
                    "title": title,
                    "subtitle": subtitle,
                    "itemId": row[4],
                    "seriesId": row[5],
                    "seasonNumber": row[6],
                    "episodeNumber": row[7],
                    "createdAt": row[8],
                    "readAt": row[9],
                    "navigationTarget": row[10],
                }
            )
        unread = self.db.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
            (user_id,),
        )
        next_cursor = str(offset + limit) if len(rows) > limit else None
        return {
            "items": items,
            "unreadCount": int(unread[0][0] or 0) if unread else 0,
            "nextCursor": next_cursor,
        }

    def summary(self, user_id: str) -> dict:
        if not _table_exists(self.db, "notifications"):
            return {"unreadCount": 0}
        rows = self.db.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
            (user_id,),
        )
        return {"unreadCount": int(rows[0][0] or 0) if rows else 0}

    def mark_read(self, user_id: str, notification_id: str, read: bool) -> dict:
        if not _table_exists(self.db, "notifications"):
            raise HTTPException(404, "Notification not found.")
        value = _now() if read else None
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE notifications SET read_at=? WHERE id=? AND user_id=?",
                (value, notification_id, user_id),
            )
            if cursor.rowcount != 1:
                raise HTTPException(404, "Notification not found.")
        return {"id": notification_id, "readAt": value}

    def mark_all_read(self, user_id: str) -> dict:
        if _table_exists(self.db, "notifications"):
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
                    (_now(), user_id),
                )
        return self.summary(user_id)

    def put_subscription(self, user_id: str, value: dict) -> dict:
        if not _table_exists(self.db, "notification_push_subscriptions"):
            raise HTTPException(503, "Push subscriptions are not available yet.")
        endpoint = str(value.get("endpoint") or "").strip()
        keys = value.get("keys")
        if not endpoint or not isinstance(keys, dict):
            raise HTTPException(400, "A Web Push subscription is required.")
        p256dh = str(keys.get("p256dh") or "").strip()
        auth = str(keys.get("auth") or "").strip()
        if not p256dh or not auth or len(endpoint) > 2048:
            raise HTTPException(400, "The Web Push subscription is invalid.")
        expiration = value.get("expirationTime")
        expiration_value = str(expiration) if expiration is not None else None
        now = _now()
        subscription_id = _id()
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO notification_push_subscriptions "
                "(id,user_id,endpoint,p256dh,auth,expiration_time,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET "
                "user_id=excluded.user_id,p256dh=excluded.p256dh,auth=excluded.auth,"
                "expiration_time=excluded.expiration_time,updated_at=excluded.updated_at",
                (
                    subscription_id,
                    user_id,
                    endpoint,
                    p256dh,
                    auth,
                    expiration_value,
                    now,
                    now,
                ),
            )
        return {"registered": True}

    def delete_subscription(self, user_id: str, endpoint: str | None = None) -> dict:
        if _table_exists(self.db, "notification_push_subscriptions"):
            with self.db.transaction() as cursor:
                if endpoint:
                    cursor.execute(
                        "DELETE FROM notification_push_subscriptions WHERE user_id=? AND endpoint=?",
                        (user_id, endpoint),
                    )
                else:
                    cursor.execute(
                        "DELETE FROM notification_push_subscriptions WHERE user_id=?",
                        (user_id,),
                    )
        return {"removed": True}

    def cleanup(self, retention_days: int) -> int:
        if not _table_exists(self.db, "notifications"):
            return 0
        cutoff = datetime.fromtimestamp(
            time.time() - max(1, retention_days) * 86400, tz=timezone.utc
        ).isoformat()
        has_outbox = _table_exists(self.db, "notification_push_outbox")
        removed = 0
        with self.db.transaction() as cursor:
            if has_outbox:
                cursor.execute(
                    "DELETE FROM notification_push_outbox WHERE state IN ('delivered','failed') "
                    "AND created_at<?",
                    (cutoff,),
                )
            cursor.execute(
                "DELETE FROM notifications WHERE read_at IS NOT NULL AND created_at<?",
                (cutoff,),
            )
            removed = cursor.rowcount
        return int(removed or 0)

    @staticmethod
    def _entity_row(cursor, entity_id: str | None):
        if not entity_id:
            return None
        row = cursor.execute(
            "SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number "
            "FROM library_entities WHERE id=?",
            (entity_id,),
        ).fetchone()
        return row

    @classmethod
    def _series_row(cls, cursor, entity_id: str):
        row = cls._entity_row(cursor, entity_id)
        seen: set[str] = set()
        while row and row[0] not in seen:
            seen.add(row[0])
            if row[3] == "series":
                return row
            row = cls._entity_row(cursor, row[2])
        return None

    @staticmethod
    def _provider(cursor, entity_id: str, provider: str, identifier_type: str):
        rows = cursor.execute(
            "SELECT provider_id FROM entity_provider_ids WHERE entity_id=? AND provider=? "
            "AND identifier_type=? ORDER BY is_primary DESC,provider_id LIMIT 1",
            (entity_id, provider, identifier_type),
        ).fetchall()
        return str(rows[0][0]) if rows else None

    def record_admissions(
        self, entity_ids: set[str] | list[str] | tuple[str, ...]
    ) -> int:
        """Create one notification per newly admitted playable item.

        This is called only after a scan has completed and the read model has
        been refreshed. The admission ledger makes retries and rescans safe.
        """
        if not entity_ids or not _table_exists(self.db, "catalog_admissions"):
            return 0
        if not _table_exists(self.db, "notifications") or not _table_exists(
            self.db, "user_follow_targets"
        ):
            return 0
        has_outbox = _table_exists(self.db, "notification_push_outbox")
        now = _now()
        created = 0
        with self.db.transaction() as cursor:
            for entity_id in dict.fromkeys(entity_ids):
                row = cursor.execute(
                    "SELECT e.id,e.library_id,e.entity_type,e.parent_id,e.relative_path,"
                    "e.season_number,e.episode_number FROM library_entities e "
                    "WHERE e.id=? AND e.entity_type IN ('movie','episode') AND EXISTS ("
                    "SELECT 1 FROM media_files m WHERE m.entity_id=e.id AND m.role='media')",
                    (entity_id,),
                ).fetchone()
                if not row:
                    continue
                cursor.execute(
                    "INSERT OR IGNORE INTO catalog_admissions(entity_id,library_id,entity_type,admitted_at) "
                    "VALUES(?,?,?,?)",
                    (row[0], row[1], row[2], now),
                )
                if cursor.rowcount != 1:
                    continue

                series_row = (
                    self._series_row(cursor, row[0]) if row[2] == "episode" else None
                )
                target_entity_id = series_row[0] if series_row else row[0]
                target_type = "series" if series_row else "movie"
                provider = "tvdb" if target_type == "series" else "tmdb"
                identifier_type = "series" if target_type == "series" else "movie"
                provider_id = self._provider(
                    cursor, target_entity_id, provider, identifier_type
                )
                if not provider_id:
                    provider = "entity"
                    provider_id = target_entity_id
                elif target_entity_id:
                    cursor.execute(
                        "UPDATE user_follow_targets SET entity_id=?,updated_at=? "
                        "WHERE library_id=? AND target_type=? AND provider=? "
                        "AND provider_id=? AND entity_id IS NULL",
                        (
                            target_entity_id,
                            now,
                            row[1],
                            target_type,
                            provider,
                            provider_id,
                        ),
                    )
                matches = cursor.execute(
                    "SELECT DISTINCT f.user_id FROM user_follow_targets f "
                    "JOIN user_library_access access ON access.user_id=f.user_id "
                    "AND access.library_id=f.library_id WHERE f.library_id=? "
                    "AND f.target_type=? AND ((f.provider=? AND f.provider_id=?) OR f.entity_id=?)",
                    (
                        row[1],
                        target_type,
                        provider,
                        provider_id,
                        target_entity_id,
                    ),
                ).fetchall()
                if not matches:
                    continue

                fallback = (row[4] or "").replace("\\", "/").rsplit("/", 1)[-1]
                if target_type == "series":
                    season = row[5]
                    episode = row[6]
                    stored_title = "New episode: Series"
                    stored_subtitle = f"New episode — {fallback or 'Episode'}"
                    navigation = f"/show/{target_entity_id}/episode/{row[0]}"
                    kind = "new_episode"
                else:
                    stored_title = fallback or "New movie"
                    stored_subtitle = "New movie added"
                    navigation = f"/show/{row[0]}"
                    season = None
                    episode = None
                    kind = "new_movie"

                for (user_id,) in matches:
                    metadata_locale, interface_locale, configured = self._user_locales(
                        cursor, user_id
                    )
                    title, subtitle = self._notification_copy(
                        cursor,
                        kind,
                        row[0],
                        target_entity_id if target_type == "series" else None,
                        stored_title,
                        stored_subtitle,
                        season,
                        episode,
                        metadata_locale,
                        interface_locale,
                        configured,
                    )
                    notification_id = _id()
                    cursor.execute(
                        "INSERT OR IGNORE INTO notifications "
                        "(id,user_id,kind,entity_id,series_id,title,subtitle,season_number,"
                        "episode_number,navigation_path,dedupe_key,created_at,read_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                        (
                            notification_id,
                            user_id,
                            kind,
                            row[0],
                            target_entity_id if target_type == "series" else None,
                            title,
                            subtitle,
                            season,
                            episode,
                            navigation,
                            f"admission:{row[0]}",
                            now,
                        ),
                    )
                    if cursor.rowcount != 1:
                        continue
                    created += 1
                    if has_outbox:
                        subscriptions = cursor.execute(
                            "SELECT id FROM notification_push_subscriptions WHERE user_id=?",
                            (user_id,),
                        ).fetchall()
                        notification_row = cursor.execute(
                            "SELECT id FROM notifications WHERE user_id=? AND dedupe_key=?",
                            (user_id, f"admission:{row[0]}"),
                        ).fetchone()
                        if notification_row:
                            for (subscription_id,) in subscriptions:
                                cursor.execute(
                                    "INSERT OR IGNORE INTO notification_push_outbox "
                                    "(id,notification_id,subscription_id,state,attempts,next_attempt_at,last_error,created_at,delivered_at) "
                                    "VALUES(?,?,?,?,?,?,?,?,?)",
                                    (
                                        _id(),
                                        notification_row[0],
                                        subscription_id,
                                        "queued",
                                        0,
                                        now,
                                        None,
                                        now,
                                        None,
                                    ),
                                )
        return created


class PushDispatcher:
    """Lifecycle-owned Web Push delivery for the durable notification outbox."""

    MAX_ATTEMPTS = 8

    def __init__(self, database=None):
        self.db = database or Config().database
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="zenstream-push-dispatcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join(timeout=max(0.0, timeout))
            if thread.is_alive():
                logger.warning("push dispatcher did not stop before shutdown deadline")
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(2.0):
            try:
                self.dispatch_pending()
            except Exception:
                logger.warning("push outbox dispatch failed", exc_info=True)

    def dispatch_pending(self) -> int:
        config = NotificationService.push_config()
        private_key = os.getenv("WEB_PUSH_VAPID_PRIVATE_KEY", "").strip()
        subject = os.getenv("WEB_PUSH_VAPID_SUBJECT", "").strip()
        if not config["configured"] or webpush is None:
            return 0
        if not _table_exists(self.db, "notification_push_outbox"):
            return 0
        now = _now()
        rows = self.db.execute(
            "SELECT o.id,o.notification_id,o.subscription_id,o.attempts,n.title,n.subtitle,"
            "n.navigation_path,s.endpoint,s.p256dh,s.auth FROM notification_push_outbox o "
            "JOIN notifications n ON n.id=o.notification_id "
            "JOIN notification_push_subscriptions s ON s.id=o.subscription_id "
            "WHERE o.state IN ('queued','retry') AND o.next_attempt_at<=? "
            "ORDER BY o.created_at,o.id LIMIT 20",
            (now,),
        )
        delivered = 0
        for row in rows:
            outbox_id, notification_id, subscription_id, attempts = row[:4]
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE notification_push_outbox SET state='retry',attempts=attempts+1 "
                    "WHERE id=? AND state IN ('queued','retry')",
                    (outbox_id,),
                )
                if cursor.rowcount != 1:
                    continue
            try:
                webpush(
                    subscription_info={
                        "endpoint": row[7],
                        "keys": {"p256dh": row[8], "auth": row[9]},
                    },
                    data=json.dumps(
                        {
                            "notificationId": notification_id,
                            "title": row[4],
                            "body": row[5],
                            "url": row[6],
                        }
                    ),
                    vapid_private_key=private_key,
                    vapid_claims={"sub": subject},
                )
            except Exception as error:  # WebPushException differs by backend.
                response = getattr(error, "response", None)
                status = getattr(response, "status_code", None)
                if status in {404, 410}:
                    with self.db.transaction() as cursor:
                        cursor.execute(
                            "DELETE FROM notification_push_subscriptions WHERE id=?",
                            (subscription_id,),
                        )
                    continue
                next_attempts = int(attempts or 0) + 1
                state = "failed" if next_attempts >= self.MAX_ATTEMPTS else "retry"
                delay = min(3600, 2 ** min(next_attempts, 10))
                retry_at = datetime.fromtimestamp(
                    time.time() + delay, tz=timezone.utc
                ).isoformat()
                with self.db.transaction() as cursor:
                    cursor.execute(
                        "UPDATE notification_push_outbox SET state=?,next_attempt_at=?,last_error=? WHERE id=?",
                        (state, retry_at, str(error)[:500], outbox_id),
                    )
                continue
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE notification_push_outbox SET state='delivered',delivered_at=?,last_error=NULL WHERE id=?",
                    (_now(), outbox_id),
                )
            delivered += 1
        return delivered
