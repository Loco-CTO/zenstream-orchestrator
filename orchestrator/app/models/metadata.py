from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

from app.config import Config
from app.language_registry import normalize_metadata_locale
from app.metadata_domain import ARTWORK_CATEGORY_SET
from cryptography.fernet import Fernet, InvalidToken

IMAGE_LANGUAGE_SCHEMA = 3

PROVIDERS = {"tmdb", "tvdb"}
DEFAULT_METADATA_LOCALES = ["en"]
PREFER_NO_LANGUAGE_FOR_BACKDROP_KEY = "prefer_no_language_for_backdrop"
DEFAULT_PREFER_NO_LANGUAGE_FOR_BACKDROP = False
METADATA_REFRESH_SETTINGS_KEY = "metadata_refresh_settings"
METADATA_REFRESH_ITEM_TYPES = ("movie", "series", "season", "episode")
METADATA_REFRESH_ARTWORK_TYPES = ("Primary", "Backdrop", "Logo", "Banner")
METADATA_REFRESH_CHECKS = (
    "missingTitle",
    "missingOverview",
    "missingName",
    "nameIsDate",
    "overviewContainsBadName",
)
_UNSET = object()


def _refresh_artwork_defaults(
    enabled_types: set[str], max_age_days: int = 7
) -> dict[str, dict[str, int | bool]]:
    return {
        image_type: {
            "enabled": image_type in enabled_types,
            "maxAgeDays": max_age_days,
        }
        for image_type in METADATA_REFRESH_ARTWORK_TYPES
    }


def _refresh_item_defaults(
    *,
    cooldown_minutes: int,
    cutoff_days: int,
    minimum_provider_ids: int,
    checks: dict[str, bool],
    status_after_days: int = -1,
    artwork: set[str] | None = None,
) -> dict:
    return {
        "enabled": True,
        "cooldownMinutes": cooldown_minutes,
        "cutoffDays": cutoff_days,
        "minimumProviderIds": minimum_provider_ids,
        "checks": {
            check: bool(checks.get(check, False))
            for check in METADATA_REFRESH_CHECKS
        },
        "statusAfterDays": status_after_days,
        "documentMaxAgeDays": 7,
        "artwork": _refresh_artwork_defaults(artwork or {"Primary"}),
        "replaceAllMetadata": False,
        "replaceAllImages": False,
    }


DEFAULT_METADATA_REFRESH_SETTINGS = {
    "seriesBlockList": "",
    "badNames": "",
    "pretend": False,
    "itemTypes": {
        "movie": _refresh_item_defaults(
            cooldown_minutes=43_200,
            cutoff_days=-1,
            minimum_provider_ids=0,
            checks={"missingTitle": True, "missingOverview": True},
            artwork={"Primary"},
        ),
        "series": _refresh_item_defaults(
            cooldown_minutes=43_200,
            cutoff_days=-1,
            minimum_provider_ids=0,
            checks={"missingTitle": True, "missingOverview": True},
            status_after_days=180,
            artwork={"Primary", "Backdrop"},
        ),
        "season": _refresh_item_defaults(
            cooldown_minutes=43_200,
            cutoff_days=-1,
            minimum_provider_ids=0,
            checks={"missingOverview": True},
            artwork={"Primary"},
        ),
        "episode": _refresh_item_defaults(
            cooldown_minutes=60,
            cutoff_days=14,
            minimum_provider_ids=0,
            checks={
                "missingTitle": True,
                "missingOverview": True,
                "missingName": True,
                "nameIsDate": True,
                "overviewContainsBadName": True,
            },
            artwork={"Primary"},
        ),
    },
}


class MetadataRefreshSettings:
    def __init__(self, db=None):
        self.db = db if db is not None else Config().database

    @staticmethod
    def _unknown(values: dict, allowed: set[str]) -> None:
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported metadata refresh setting: {sorted(unknown)[0]}")

    @staticmethod
    def _bool(values: dict, key: str, default: bool) -> bool:
        value = values.get(key, default)
        if type(value) is not bool:
            raise ValueError(f"{key} must be a boolean")
        return value

    @staticmethod
    def _days(values: dict, key: str, default: int) -> int:
        value = values.get(key, default)
        if type(value) is not int or value < -1:
            raise ValueError(f"{key} must be an integer of -1 or greater")
        return value

    @staticmethod
    def _cooldown(values: dict, default: int) -> int:
        value = values.get("cooldownMinutes", default)
        if type(value) is not int or value < -1:
            raise ValueError("cooldownMinutes must be an integer of -1 or greater")
        return value

    @classmethod
    def normalize(cls, values) -> dict:
        if not isinstance(values, dict):
            raise ValueError("Metadata refresh settings must be an object")
        cls._unknown(values, {"seriesBlockList", "badNames", "pretend", "itemTypes"})
        result = deepcopy(DEFAULT_METADATA_REFRESH_SETTINGS)
        for key in ("seriesBlockList", "badNames"):
            value = values.get(key, result[key])
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            result[key] = value.strip()
        result["pretend"] = cls._bool(values, "pretend", result["pretend"])
        item_values = values.get("itemTypes", {})
        if not isinstance(item_values, dict):
            raise ValueError("itemTypes must be an object")
        cls._unknown(item_values, set(METADATA_REFRESH_ITEM_TYPES))
        for entity_type in METADATA_REFRESH_ITEM_TYPES:
            source = item_values.get(entity_type, {})
            if not isinstance(source, dict):
                raise ValueError(f"itemTypes.{entity_type} must be an object")
            defaults = result["itemTypes"][entity_type]
            cls._unknown(
                source,
                {
                    "enabled",
                    "cooldownMinutes",
                    "cutoffDays",
                    "minimumProviderIds",
                    "checks",
                    "statusAfterDays",
                    "documentMaxAgeDays",
                    "artwork",
                    "replaceAllMetadata",
                    "replaceAllImages",
                },
            )
            normalized = {
                "enabled": cls._bool(source, "enabled", defaults["enabled"]),
                "cooldownMinutes": cls._cooldown(
                    source, defaults["cooldownMinutes"]
                ),
                "cutoffDays": cls._days(
                    source, "cutoffDays", defaults["cutoffDays"]
                ),
                "minimumProviderIds": source.get(
                    "minimumProviderIds", defaults["minimumProviderIds"]
                ),
                "statusAfterDays": cls._days(
                    source, "statusAfterDays", defaults["statusAfterDays"]
                ),
                "documentMaxAgeDays": cls._days(
                    source, "documentMaxAgeDays", defaults["documentMaxAgeDays"]
                ),
                "replaceAllMetadata": cls._bool(
                    source, "replaceAllMetadata", defaults["replaceAllMetadata"]
                ),
                "replaceAllImages": cls._bool(
                    source, "replaceAllImages", defaults["replaceAllImages"]
                ),
            }
            if (
                type(normalized["minimumProviderIds"]) is not int
                or normalized["minimumProviderIds"] < 0
            ):
                raise ValueError("minimumProviderIds must be a non-negative integer")
            checks = source.get("checks", {})
            if not isinstance(checks, dict):
                raise ValueError(f"itemTypes.{entity_type}.checks must be an object")
            cls._unknown(checks, set(METADATA_REFRESH_CHECKS))
            normalized["checks"] = {
                check: cls._bool(
                    checks, check, defaults["checks"].get(check, False)
                )
                for check in METADATA_REFRESH_CHECKS
            }
            artwork = source.get("artwork", {})
            if not isinstance(artwork, dict):
                raise ValueError(f"itemTypes.{entity_type}.artwork must be an object")
            cls._unknown(artwork, set(METADATA_REFRESH_ARTWORK_TYPES))
            normalized["artwork"] = {}
            for image_type in METADATA_REFRESH_ARTWORK_TYPES:
                image_values = artwork.get(image_type, {})
                if not isinstance(image_values, dict):
                    raise ValueError(
                        f"itemTypes.{entity_type}.artwork.{image_type} must be an object"
                    )
                image_defaults = defaults["artwork"][image_type]
                cls._unknown(image_values, {"enabled", "maxAgeDays"})
                normalized["artwork"][image_type] = {
                    "enabled": cls._bool(
                        image_values, "enabled", image_defaults["enabled"]
                    ),
                    "maxAgeDays": cls._days(
                        image_values, "maxAgeDays", image_defaults["maxAgeDays"]
                    ),
                }
            result["itemTypes"][entity_type] = normalized
        return result

    def get(self) -> dict:
        try:
            rows = self.db.read_execute(
                "SELECT value FROM metadata_settings WHERE key=?",
                (METADATA_REFRESH_SETTINGS_KEY,),
            )
        except Exception:
            return deepcopy(DEFAULT_METADATA_REFRESH_SETTINGS)
        if not rows:
            return deepcopy(DEFAULT_METADATA_REFRESH_SETTINGS)
        try:
            return self.normalize(json.loads(rows[0][0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return deepcopy(DEFAULT_METADATA_REFRESH_SETTINGS)

    def update(self, values) -> dict:
        normalized = self.normalize(values)
        timestamp = iso_now()
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO metadata_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (
                    METADATA_REFRESH_SETTINGS_KEY,
                    json.dumps(normalized, ensure_ascii=False),
                    timestamp,
                ),
            )
        return deepcopy(normalized)


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

    def prefer_no_language_for_backdrop(self) -> bool:
        try:
            rows = self.db.read_execute(
                "SELECT value FROM metadata_settings WHERE key=?",
                (PREFER_NO_LANGUAGE_FOR_BACKDROP_KEY,),
            )
        except Exception:
            return DEFAULT_PREFER_NO_LANGUAGE_FOR_BACKDROP
        if not rows:
            return DEFAULT_PREFER_NO_LANGUAGE_FOR_BACKDROP
        try:
            value = json.loads(rows[0][0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return DEFAULT_PREFER_NO_LANGUAGE_FOR_BACKDROP
        return value if type(value) is bool else DEFAULT_PREFER_NO_LANGUAGE_FOR_BACKDROP

    def get_settings(self) -> dict:
        return {
            "locales": self.get(),
            "preferNoLanguageForBackdrop": self.prefer_no_language_for_backdrop(),
        }

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
        return self.update(values)["locales"]

    def update(
        self,
        values,
        prefer_no_language_for_backdrop=_UNSET,
    ) -> dict:
        locales = self.normalize(values)
        if prefer_no_language_for_backdrop is _UNSET:
            prefer_no_language_for_backdrop = self.prefer_no_language_for_backdrop()
        elif type(prefer_no_language_for_backdrop) is not bool:
            raise ValueError("preferNoLanguageForBackdrop must be a boolean.")

        timestamp = iso_now()
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO metadata_settings(key,value,updated_at) VALUES('locales',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (json.dumps(locales, ensure_ascii=False), timestamp),
            )
            cursor.execute(
                "INSERT INTO metadata_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (
                    PREFER_NO_LANGUAGE_FOR_BACKDROP_KEY,
                    json.dumps(prefer_no_language_for_backdrop),
                    timestamp,
                ),
            )
            # An explicit user preference may only point at a configured
            # language. Removed languages fall back to automatic selection.
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='account_preferences'"
            )
            if cursor.fetchall():
                placeholders = ",".join("?" for _ in locales)
                cursor.execute(
                    f"UPDATE account_preferences SET metadata_language=NULL WHERE metadata_language IS NOT NULL AND metadata_language NOT IN ({placeholders})",
                    locales,
                )
        return {
            "locales": locales,
            "preferNoLanguageForBackdrop": prefer_no_language_for_backdrop,
        }


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

    def get_locales(
        self, provider: str, entity_type: str, provider_id: str
    ) -> dict[str, dict]:
        """Return every usable cached locale for one provider identity.

        Calendar reads need the same locale fallback behavior as catalog reads.
        Expired documents are still returned and marked stale; callers can use
        them as a display fallback while a background refresh repairs them.
        """
        rows = self.db.read_execute(
            "SELECT locale,payload,expires_at FROM metadata_cache "
            "WHERE provider=? AND entity_type=? AND provider_id=?",
            (provider, entity_type, provider_id),
        )
        values: dict[str, dict] = {}
        now = iso_now()
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
            payload["_stale"] = bool(expires_at and expires_at <= now)
            values[str(locale)] = payload
        return values

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
