"""Provider credentials and locale-keyed metadata cache helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken

from app.config import Config


PROVIDERS = {"tmdb", "tvdb"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def _fernet() -> Fernet:
    secret = os.getenv("SECRET_KEY", "")
    if not secret:
        raise RuntimeError("SECRET_KEY is required to access metadata credentials")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class MetadataCredentials:
    def __init__(self):
        self.db = Config().database

    def configured(self) -> dict:
        rows = self.db.execute(
            "SELECT provider, credential_type, validated_at, updated_at FROM metadata_credentials ORDER BY provider"
        )
        values = {
            provider: {
                "configured": True,
                "credentialType": credential_type,
                "validatedAt": validated_at,
                "updatedAt": updated_at,
            }
            for provider, credential_type, validated_at, updated_at in rows
        }
        for provider in sorted(PROVIDERS):
            values.setdefault(provider, {"configured": False, "validatedAt": None})
        values["musicbrainz"] = {
            "configured": True,
            "credentialType": "built_in",
            "validatedAt": None,
        }
        return values

    def get(self, provider: str) -> dict | None:
        if provider == "musicbrainz":
            return {}
        if provider not in PROVIDERS:
            raise ValueError("Unsupported metadata provider")
        rows = self.db.execute(
            "SELECT ciphertext, credential_type FROM metadata_credentials WHERE provider = ?",
            (provider,),
        )
        if not rows:
            return None
        try:
            return json.loads(_fernet().decrypt(rows[0][0].encode("ascii")).decode("utf-8"))
        except (InvalidToken, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Stored provider credential cannot be decrypted; enter it again.") from error

    def set(self, provider: str, credential: dict, credential_type: str = "api_key") -> None:
        if provider not in PROVIDERS:
            raise ValueError("Unsupported metadata provider")
        ciphertext = _fernet().encrypt(json.dumps(credential, separators=(",", ":")).encode("utf-8")).decode("ascii")
        now = iso_now()
        self.db.execute(
            "INSERT INTO metadata_credentials(provider, ciphertext, credential_type, validated_at, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(provider) DO UPDATE SET ciphertext=excluded.ciphertext, credential_type=excluded.credential_type, "
            "validated_at=excluded.validated_at, updated_at=excluded.updated_at",
            (provider, ciphertext, credential_type, now, now),
        )

    def clear(self, provider: str) -> None:
        if provider not in PROVIDERS:
            raise ValueError("Unsupported metadata provider")
        self.db.execute("DELETE FROM metadata_credentials WHERE provider = ?", (provider,))

    def mark_validated(self, provider: str) -> None:
        self.db.execute(
            "UPDATE metadata_credentials SET validated_at = ?, updated_at = ? WHERE provider = ?",
            (iso_now(), iso_now(), provider),
        )


class MetadataCache:
    def __init__(self):
        self.db = Config().database

    def get(self, provider: str, entity_type: str, provider_id: str, locale: str) -> dict | None:
        rows = self.db.execute(
            "SELECT payload, expires_at FROM metadata_cache WHERE provider=? AND entity_type=? AND provider_id=? AND locale=?",
            (provider, entity_type, provider_id, locale),
        )
        if not rows:
            return None
        try:
            payload = json.loads(rows[0][0])
        except json.JSONDecodeError:
            return None
        payload["_stale"] = rows[0][1] <= iso_now()
        return payload

    def any(self, provider: str, entity_type: str, provider_id: str) -> dict | None:
        """Return the newest non-empty locale when a requested translation is absent."""
        rows = self.db.execute(
            "SELECT payload, expires_at FROM metadata_cache WHERE provider=? AND entity_type=? AND provider_id=? ORDER BY fetched_at DESC",
            (provider, entity_type, provider_id),
        )
        for payload_text, expires_at in rows:
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                continue
            if payload.get("title") or payload.get("overview") or payload.get("images"):
                payload["_stale"] = expires_at <= iso_now()
                return payload
        return None

    def put(self, provider: str, entity_type: str, provider_id: str, locale: str, payload: dict, days: int = 7) -> None:
        now = utc_now()
        self.db.execute(
            "INSERT INTO metadata_cache(provider, entity_type, provider_id, locale, payload, fetched_at, expires_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(provider, entity_type, provider_id, locale) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
            (provider, entity_type, provider_id, locale, json.dumps(payload, ensure_ascii=False), now.isoformat(), (now + timedelta(days=days)).isoformat()),
        )

    def put_image(self, provider: str, entity_type: str, provider_id: str, locale: str | None, image_type: str, image_url: str, local_path: str | None = None) -> None:
        now = utc_now()
        self.db.execute(
            "INSERT INTO metadata_images(provider, entity_type, provider_id, locale, image_type, image_url, local_path, fetched_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(provider, entity_type, provider_id, locale, image_type, image_url) DO UPDATE SET local_path=excluded.local_path, fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
            (provider, entity_type, provider_id, locale, image_type, image_url, local_path, now.isoformat(), (now + timedelta(days=7)).isoformat()),
        )
