from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

from app.config import Config
from app.metadata_domain import ARTWORK_CATEGORY_SET
from cryptography.fernet import Fernet, InvalidToken

IMAGE_LANGUAGE_SCHEMA = 3

PROVIDERS = {"tmdb", "tvdb"}
DEFAULT_METADATA_LOCALES = ["en"]
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def normalize_metadata_locale(value: str) -> str:
    parts = str(value or "").strip().replace("_", "-").split("-")
    if not parts or not parts[0]:
        raise ValueError("Metadata languages must be valid language tags.")
    normalized = "-".join(
        [
            parts[0].lower(),
            *[
                part.upper() if len(part) == 2 or part.isdigit() else part
                for part in parts[1:]
            ],
        ]
    )
    if not _LOCALE_RE.fullmatch(normalized):
        raise ValueError(f"Invalid metadata language '{value}'.")
    return normalized


class MetadataLanguageSettings:
    def __init__(self):
        self.db = Config().database

    def get(self) -> list[str]:
        rows = self.db.read_execute(
            "SELECT value FROM metadata_settings WHERE key='locales'"
        )
        if not rows:
            return list(DEFAULT_METADATA_LOCALES)
        try:
            values = json.loads(rows[0][0])
            return self.normalize(values)
        except (TypeError, ValueError, json.JSONDecodeError):
            return list(DEFAULT_METADATA_LOCALES)

    @staticmethod
    def normalize(values) -> list[str]:
        if not isinstance(values, list):
            raise ValueError("Metadata languages must be a non-empty list.")
        result = []
        for value in values:
            locale = normalize_metadata_locale(str(value))
            if locale not in result:
                result.append(locale)
        if not result:
            raise ValueError("At least one metadata language is required.")
        return result

    def set(self, values) -> list[str]:
        locales = self.normalize(values)
        self.db.execute(
            "INSERT INTO metadata_settings(key,value,updated_at) VALUES('locales',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (json.dumps(locales, ensure_ascii=False), iso_now()),
        )
        # An explicit user preference may only point at a configured
        # language. Removed languages fall back to automatic selection.
        if self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='account_preferences'"
        ):
            placeholders = ",".join("?" for _ in locales)
            self.db.execute(
                f"UPDATE account_preferences SET metadata_language=NULL WHERE metadata_language IS NOT NULL AND metadata_language NOT IN ({placeholders})",
                locales,
            )
        return locales


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
        values = {}
        for provider, credential_type, validated_at, updated_at in rows:
            credential = self.get(provider) or {}
            values[provider] = {
                "configured": True,
                "credentialType": credential_type,
                "credential": credential.get("value") or credential.get("apiKey") or "",
                "validatedAt": validated_at,
                "updatedAt": updated_at,
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
            return json.loads(
                _fernet().decrypt(rows[0][0].encode("ascii")).decode("utf-8")
            )
        except (InvalidToken, ValueError, json.JSONDecodeError) as error:
            raise ValueError(
                "Stored provider credential cannot be decrypted; enter it again."
            ) from error

    def set(
        self, provider: str, credential: dict, credential_type: str = "api_key"
    ) -> None:
        if provider not in PROVIDERS:
            raise ValueError("Unsupported metadata provider")
        ciphertext = (
            _fernet()
            .encrypt(json.dumps(credential, separators=(",", ":")).encode("utf-8"))
            .decode("ascii")
        )
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
        self.db.execute(
            "DELETE FROM metadata_credentials WHERE provider = ?", (provider,)
        )

    def mark_validated(self, provider: str) -> None:
        self.db.execute(
            "UPDATE metadata_credentials SET validated_at = ?, updated_at = ? WHERE provider = ?",
            (iso_now(), iso_now(), provider),
        )


class MetadataCache:
    def __init__(self):
        self.db = Config().database

    def get(
        self, provider: str, entity_type: str, provider_id: str, locale: str
    ) -> dict | None:
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
        if payload.get("_imageLanguageSchema") != IMAGE_LANGUAGE_SCHEMA:
            return None
        payload["_stale"] = rows[0][1] <= iso_now()
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
        payload = dict(payload)
        payload["_imageLanguageSchema"] = IMAGE_LANGUAGE_SCHEMA
        # Keep the locale used for the provider request inside the cache
        # payload.  This lets bulk series aggregation distinguish a payload
        # fetched for the requested locale from legacy hierarchy data that was
        # copied into every locale bucket.
        payload["_metadataLocale"] = locale
        now = utc_now()
        encoded = json.dumps(payload, ensure_ascii=False)
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO metadata_cache(provider, entity_type, provider_id, locale, payload, fetched_at, expires_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(provider, entity_type, provider_id, locale) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
                (
                    provider,
                    entity_type,
                    provider_id,
                    locale,
                    encoded,
                    now.isoformat(),
                    (now + timedelta(days=days)).isoformat(),
                ),
            )

    def put_image(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str | None,
        image_type: str,
        image_url: str,
        blur_hash: str | None = None,
        local_path: str | None = None,
    ) -> None:
        if image_type not in ARTWORK_CATEGORY_SET:
            raise ValueError(f"Unsupported image type '{image_type}'.")
        now = utc_now()
        self.db.execute(
            "INSERT INTO metadata_images(provider, entity_type, provider_id, locale, image_type, image_url, blur_hash, local_path, fetched_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(provider, entity_type, provider_id, locale, image_type, image_url) DO UPDATE SET blur_hash=excluded.blur_hash, local_path=excluded.local_path, fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
            (
                provider,
                entity_type,
                provider_id,
                locale or "",
                image_type,
                image_url,
                blur_hash,
                local_path,
                now.isoformat(),
                (now + timedelta(days=7)).isoformat(),
            ),
        )

    def put_images(self, records) -> None:
        records = list(records)
        if not records:
            return
        now = utc_now()
        fetched_at = now.isoformat()
        expires_at = (now + timedelta(days=7)).isoformat()
        for record in records:
            if record[4] not in ARTWORK_CATEGORY_SET:
                raise ValueError(f"Unsupported image type '{record[4]}'.")
        normalized_records = [
            (*record[:3], record[3] or "", *record[4:]) for record in records
        ]
        with self.db.transaction() as cursor:
            cursor.executemany(
                "INSERT INTO metadata_images(provider, entity_type, provider_id, locale, image_type, image_url, blur_hash, local_path, fetched_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider, entity_type, provider_id, locale, image_type, image_url) DO UPDATE SET blur_hash=excluded.blur_hash, local_path=excluded.local_path, fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
                [(*record, fetched_at, expires_at) for record in normalized_records],
            )
