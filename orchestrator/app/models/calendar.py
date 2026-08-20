from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.config import Config
from app.models.metadata import IMAGE_LANGUAGE_SCHEMA, MetadataCache, _fernet
from cryptography.fernet import InvalidToken


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _now().isoformat()


def encrypt_calendar_api_key(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_calendar_api_key(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as error:
        raise ValueError("Stored calendar API key cannot be decrypted; enter it again.") from error


class FutureMetadataCache:
    """The calendar's isolated metadata-only cache boundary."""

    def __init__(self):
        self.db = Config().database

    def get_locales(
        self, provider: str, entity_type: str, provider_id: str
    ) -> dict[str, dict]:
        """Return every usable cached locale for one future identity.

        Future documents remain readable after expiry so the calendar can show
        the last known title while the daily refetch job obtains a replacement.
        """
        rows = self.db.read_execute(
            "SELECT locale,payload,expires_at FROM future_metadata_cache "
            "WHERE provider=? AND entity_type=? AND provider_id=?",
            (provider, entity_type, provider_id),
        )
        values: dict[str, dict] = {}
        now = _iso_now()
        for locale, encoded, expires_at in rows:
            try:
                payload = json.loads(encoded)
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                not isinstance(payload, dict)
                or payload.get("_imageLanguageSchema") != IMAGE_LANGUAGE_SCHEMA
            ):
                continue
            payload.pop("images", None)
            payload["_stale"] = bool(expires_at and expires_at <= now)
            values[str(locale)] = payload
        return values

    def get(
        self, provider: str, entity_type: str, provider_id: str, locale: str
    ) -> dict | None:
        rows = self.db.read_execute(
            "SELECT payload,expires_at FROM future_metadata_cache "
            "WHERE provider=? AND entity_type=? AND provider_id=? AND locale=?",
            (provider, entity_type, provider_id, locale),
        )
        if not rows:
            return None
        try:
            payload = json.loads(rows[0][0])
        except (TypeError, json.JSONDecodeError):
            return None
        if payload.get("_imageLanguageSchema") != IMAGE_LANGUAGE_SCHEMA:
            return None
        payload.pop("images", None)
        payload["_stale"] = rows[0][1] <= _iso_now()
        return payload

    def put(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        payload: dict,
        days: int = 7,
    ) -> None:
        value = dict(payload)
        value.pop("images", None)
        value["_imageLanguageSchema"] = IMAGE_LANGUAGE_SCHEMA
        value["_metadataLocale"] = locale
        current = _now()
        self.db.execute(
            "INSERT INTO future_metadata_cache(provider,entity_type,provider_id,locale,payload,fetched_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(provider,entity_type,provider_id,locale) DO UPDATE SET "
            "payload=excluded.payload,fetched_at=excluded.fetched_at,expires_at=excluded.expires_at",
            (
                provider,
                entity_type,
                provider_id,
                locale,
                json.dumps(value, ensure_ascii=False),
                current.isoformat(),
                (current + timedelta(days=days)).isoformat(),
            ),
        )

    def delete_identity(self, provider: str, entity_type: str, provider_id: str) -> None:
        paths = self.db.execute(
            "SELECT local_path FROM future_metadata_images WHERE provider=? AND entity_type=? AND provider_id=?",
            (provider, entity_type, provider_id),
        )
        with self.db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM future_metadata_images WHERE provider=? AND entity_type=? AND provider_id=?",
                (provider, entity_type, provider_id),
            )
            cursor.execute(
                "DELETE FROM future_metadata_cache WHERE provider=? AND entity_type=? AND provider_id=?",
                (provider, entity_type, provider_id),
            )
        root = getattr(self.db, "db_file", None)
        if root and root != ":memory:":
            from pathlib import Path

            cache_root = (Path(root).parent / "future-metadata-cache").resolve()
            for (raw_path,) in paths:
                if not raw_path:
                    continue
                path = Path(str(raw_path))
                try:
                    path.resolve().relative_to(cache_root)
                except (OSError, ValueError):
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def promote_identity(self, provider: str, entity_type: str, provider_id: str) -> int:
        rows = self.db.execute(
            "SELECT locale,payload FROM future_metadata_cache WHERE provider=? AND entity_type=? AND provider_id=?",
            (provider, entity_type, provider_id),
        )
        if not rows:
            self.delete_identity(provider, entity_type, provider_id)
            return 0
        normal = MetadataCache()
        for locale, raw_payload in rows:
            try:
                payload = json.loads(raw_payload)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                payload.pop("images", None)
            normal.put(provider, entity_type, provider_id, locale, payload)
        self.delete_identity(provider, entity_type, provider_id)
        return len(rows)

    def prune_expired(self, before: str | None = None) -> int:
        cutoff = before or _iso_now()
        paths = self.db.execute(
            "SELECT local_path FROM future_metadata_images WHERE local_path IS NOT NULL"
        )
        with self.db.transaction() as cursor:
            cursor.execute("DELETE FROM future_metadata_cache WHERE expires_at<?", (cutoff,))
            removed = max(0, cursor.rowcount)
            cursor.execute("DELETE FROM future_metadata_images")
            removed += max(0, cursor.rowcount)
        root = getattr(self.db, "db_file", None)
        if root and root != ":memory:":
            from pathlib import Path

            cache_root = (Path(root).parent / "future-metadata-cache").resolve()
            for (raw_path,) in paths:
                if not raw_path:
                    continue
                path = Path(str(raw_path))
                try:
                    path.resolve().relative_to(cache_root)
                except (OSError, ValueError):
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        return removed
