from __future__ import annotations

import threading
import time
import re
import ssl
import unicodedata
from typing import Any
from pathlib import Path
from urllib.parse import quote

import httpx
import pycountry

from app.models.metadata import MetadataCache, MetadataCredentials
from app.metadata_domain import ARTWORK_CATEGORIES, choose_artwork
from app.logging_config import get_logger
from version import __version__


class ProviderError(RuntimeError):
    pass


logger = get_logger("providers")


PRIMARY = "Primary"
BACKDROP = "Backdrop"
LOGO = "Logo"
BANNER = "Banner"
IMAGE_TYPES = set(ARTWORK_CATEGORIES)

PRIMARY_PROVIDER_BY_ENTITY = {
    "series": "tvdb",
    "season": "tvdb",
    "episode": "tvdb",
    "movie": "tmdb",
    "artist": "musicbrainz",
    "release": "musicbrainz",
    "release_group": "musicbrainz",
    "track": "musicbrainz",
    "recording": "musicbrainz",
    "collection": "tvdb",
}


def _image(
    image_type: str,
    url: str,
    *,
    language: str | None = None,
    provider: str,
    source_type: str | None = None,
    score: float = 0,
    width: int = 0,
    height: int = 0,
    provider_language: str | None = None,
) -> dict:
    value = {
        "type": image_type,
        "language": language,
        "url": url,
        "score": score or 0,
        "width": width or 0,
        "height": height or 0,
        "provider": provider,
    }
    if provider_language and provider_language.lower() != (language or "").lower():
        value["providerLanguage"] = provider_language
    if source_type is not None:
        value["sourceType"] = source_type
    return value


def _trailer_url(
    site: str | None, key: str | None, value: dict | None = None
) -> str | None:
    value = value or {}
    direct = (
        value.get("url")
        or value.get("link")
        or value.get("videoUrl")
        or value.get("youtubeUrl")
    )
    if isinstance(direct, str) and direct.startswith(("http://", "https://")):
        return direct
    if not key:
        return None
    site = (site or "").strip().lower()
    if site == "youtube":
        return f"https://www.youtube.com/watch?v={key}"
    if site == "vimeo":
        return f"https://vimeo.com/{key}"
    return None


def _normalize_trailers(values: object, provider: str) -> list[dict]:
    """Normalize TMDB/TVDB trailer records to one URL-bearing shape."""
    if isinstance(values, dict):
        values = values.get("results") or values.get("data") or []
    result = []
    for trailer in values if isinstance(values, list) else []:
        if isinstance(trailer, str):
            trailer = {"url": trailer}
        if not isinstance(trailer, dict):
            continue
        site = trailer.get("site") or trailer.get("provider") or trailer.get("source")
        key = trailer.get("key") or trailer.get("videoId")
        url = _trailer_url(site, str(key) if key is not None else None, trailer)
        if not url:
            continue
        result.append(
            {
                "url": url,
                "site": site,
                "key": str(key) if key is not None else None,
                "name": trailer.get("name") or trailer.get("title"),
                "type": trailer.get("type") or "Trailer",
                "official": trailer.get("official"),
                "language": trailer.get("language")
                or trailer.get("iso_639_1")
                or trailer.get("languageCode"),
                "provider": provider,
            }
        )
    return result


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name") or value.get("title") or value.get("value")
    return str(value) if value else None


def _names(values: Any) -> list[str]:
    return [value for value in (_name(item) for item in values or []) if value]


def _normalize_language_tag(value: str | None) -> str:
    parts = str(value or "").strip().replace("_", "-").split("-", 1)
    if not parts[0]:
        return ""
    return (
        parts[0].lower()
        if len(parts) == 1
        else f"{parts[0].lower()}-{parts[1].upper()}"
    )


def _catalog_language_tag(
    provider_code: str, name: str | None = None, short_code: str | None = None
) -> str:
    """Resolve a provider language record through ISO catalogs without language-pair tables."""
    raw = str(provider_code or "").strip().lower()
    canonical = _normalize_language_tag(short_code)
    language_name, _, qualifier = str(name or "").partition(" - ")
    if not canonical:
        language = None
        for lookup in (
            {"alpha_2": raw} if len(raw) == 2 else {},
            {"alpha_3": raw} if len(raw) == 3 else {},
            {"bibliographic": raw} if len(raw) == 3 else {},
        ):
            if not lookup:
                continue
            language = pycountry.languages.get(**lookup)
            if language:
                break
        if not language and language_name:
            try:
                language = pycountry.languages.lookup(language_name)
            except LookupError:
                language = None
        canonical = str(getattr(language, "alpha_2", "") or raw).lower()
    if qualifier and canonical:
        try:
            country = pycountry.countries.lookup(qualifier)
        except LookupError:
            country = None
        if country:
            canonical = f"{_language_family(canonical)}-{country.alpha_2.upper()}"
    return canonical or raw


class ProviderLanguageCatalog:
    """Bidirectional provider-code and canonical-locale resolver."""

    def __init__(self):
        self.provider_to_canonical: dict[str, str] = {}
        self.provider_spelling: dict[str, str] = {}
        self.canonical_to_provider: dict[str, str] = {}

    def register(
        self, provider_code: str, canonical: str, *, prefer: bool = False
    ) -> None:
        provider = str(provider_code or "").strip()
        canonical_tag = _normalize_language_tag(canonical)
        if not provider or not canonical_tag:
            return
        provider_key = provider.lower()
        self.provider_to_canonical[provider_key] = canonical_tag
        self.provider_spelling[provider_key] = provider
        if prefer or canonical_tag not in self.canonical_to_provider:
            self.canonical_to_provider[canonical_tag] = provider
        base = _language_family(canonical_tag)
        if "-" not in canonical_tag and (
            prefer or base not in self.canonical_to_provider
        ):
            self.canonical_to_provider[base] = provider

    def canonical(self, provider_code: str | None) -> str | None:
        if not provider_code:
            return None
        raw = str(provider_code).strip()
        return self.provider_to_canonical.get(raw.lower(), _normalize_language_tag(raw))

    def provider(self, locale: str | None) -> str:
        raw = str(locale or "").strip()
        normalized = _normalize_language_tag(raw)
        provider_key = raw.lower()
        provider_canonical = self.provider_to_canonical.get(provider_key)
        # A provider code that means a more specific canonical locale (for
        # example a regional provider code) must remain directly addressable.
        if provider_canonical and provider_canonical != normalized:
            return self.provider_spelling[provider_key]
        return self.canonical_to_provider.get(
            normalized,
            self.canonical_to_provider.get(_language_family(normalized), raw),
        )


class ProviderClient:
    def __init__(self, timeout: float = 20):
        self.timeout = timeout

    def _get(self, url: str, **kwargs) -> dict:
        try:
            request_params = dict(kwargs.get("params") or {})
            request_params.pop("api_key", None)
            logger.debug("provider request url=%s params=%s", url, request_params)
            response = httpx.get(url, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            payload = response.json()
            logger.debug(
                "provider response url=%s status=%s payload=%s",
                url,
                response.status_code,
                payload,
            )
            return payload
        except (httpx.HTTPError, ValueError) as error:
            logger.debug(
                "provider request failed url=%s error=%s", url, error, exc_info=True
            )
            raise ProviderError(
                f"provider request failed: {type(error).__name__}: {error}"
            ) from error


class TMDBClient(ProviderClient):
    base_url = "https://api.themoviedb.org/3"
    _language_catalog = ProviderLanguageCatalog()
    _language_codes_loaded = False
    _language_codes_lock = threading.Lock()

    def __init__(
        self, credentials: dict, credential_type: str = "api_key", timeout: float = 20
    ):
        super().__init__(timeout)
        self.credentials = credentials
        self.credential_type = credential_type

    def _request(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        headers = {"Accept": "application/json"}
        value = self.credentials.get("value") or self.credentials.get("apiKey")
        if not value:
            raise ProviderError("TMDB credential is empty")
        if self.credential_type in {"read_access_token", "bearer", "v4"}:
            headers["Authorization"] = f"Bearer {value}"
        else:
            params["api_key"] = value
        return self._get(f"{self.base_url}{path}", params=params, headers=headers)

    def _language_code(self, locale: str) -> str:
        with self._language_codes_lock:
            if not self._language_codes_loaded:
                try:
                    values = self._request("/configuration/languages")
                    for value in values if isinstance(values, list) else []:
                        if not isinstance(value, dict):
                            continue
                        code = str(value.get("iso_639_1") or "").lower()
                        if code:
                            self.__class__._language_catalog.register(code, code)
                    primary_translations = self._request(
                        "/configuration/primary_translations"
                    )
                    for value in (
                        primary_translations
                        if isinstance(primary_translations, list)
                        else []
                    ):
                        code = str(value or "").strip()
                        if "-" in code:
                            canonical = _normalize_language_tag(code)
                            self.__class__._language_catalog.register(
                                code, canonical, prefer=True
                            )
                            self.__class__._language_catalog.canonical_to_provider[
                                _language_family(canonical)
                            ] = code
                except ProviderError:
                    logger.warning(
                        "TMDB language catalog unavailable; using locale fallback"
                    )
                self.__class__._language_codes_loaded = True
        return self._language_catalog.provider(locale)

    def _canonical_language(self, value: str | None) -> tuple[str | None, str | None]:
        if not value:
            return None, None
        raw = value.strip()
        return self._language_catalog.canonical(raw), raw

    @staticmethod
    def _include_image_language(language: str) -> str:
        """Keep localized and language-neutral TMDB artwork in appended images.

        TMDB applies the ``language`` filter to the appended ``images``
        response too.  Without this parameter, an English details request can
        return no posters even when the title has artwork; TMDB documents
        ``include_image_language`` as the fallback for this case.
        """
        values = [language, _language_family(language), "null"]
        return ",".join(dict.fromkeys(value for value in values if value))

    def test(self) -> None:
        self._request("/configuration")

    def search(
        self, entity_type: str, query: str, year: str | None = None
    ) -> list[dict]:
        kind = "tv" if entity_type in {"series", "episode"} else "movie"
        params = {"query": query, "language": "en"}
        if year and year.isdigit():
            params["year" if kind == "movie" else "first_air_date_year"] = year
        payload = self._request(f"/search/{kind}", params=params)
        return [
            {
                "provider": "tmdb",
                "providerId": str(value.get("id")),
                "title": value.get("title") or value.get("name"),
                "year": (
                    value.get("release_date") or value.get("first_air_date") or ""
                )[:4]
                or None,
                "overview": value.get("overview"),
            }
            for value in payload.get("results", [])
            if value.get("id")
        ]

    def details(self, entity_type: str, provider_id: str, locale: str) -> dict:
        language = self._language_code(locale)
        image_language = self._include_image_language(language)
        if entity_type == "season":
            series_id, season = provider_id.split(":", 1)
            return self._request(
                f"/tv/{quote(series_id)}/season/{quote(season)}",
                params={
                    "language": language,
                    "include_image_language": image_language,
                    "append_to_response": "images,external_ids,videos",
                },
            )
        if entity_type == "episode":
            series_id, season, episode = provider_id.split(":", 2)
            return self._request(
                f"/tv/{quote(series_id)}/season/{quote(season)}/episode/{quote(episode)}",
                params={
                    "language": language,
                    "include_image_language": image_language,
                    "append_to_response": "images,external_ids,videos",
                },
            )
        kind = "tv" if entity_type == "series" else "movie"
        return self._request(
            f"/{kind}/{quote(provider_id)}",
            params={
                "language": language,
                "include_image_language": image_language,
                "append_to_response": "images,external_ids,credits,videos",
            },
        )

    def series_hierarchy(self, provider_id: str, locale: str) -> dict:
        """Fetch season details, whose responses include their episode lists."""
        series = self.details("series", provider_id, locale)
        seasons = []
        for summary in series.get("seasons", []) or []:
            season_number = summary.get("season_number")
            if season_number is None:
                continue
            season_id = f"{provider_id}:{season_number}"
            seasons.append(
                {
                    "summary": summary,
                    "details": self.details("season", season_id, locale),
                }
            )
        return {"series": series, "seasons": seasons}

    def normalize(self, entity_type: str, provider_id: str, payload: dict) -> dict:
        title = (
            payload.get("title")
            or payload.get("name")
            or payload.get("original_title")
            or payload.get("original_name")
        )
        external = payload.get("external_ids") or {}
        ids = []
        if external.get("tvdb_id"):
            ids.append(
                {
                    "provider": "tvdb",
                    "identifierType": entity_type,
                    "id": str(external["tvdb_id"]),
                }
            )
        if external.get("imdb_id"):
            ids.append(
                {
                    "provider": "imdb",
                    "identifierType": "imdb",
                    "id": str(external["imdb_id"]),
                }
            )
        dates = (
            payload.get("release_date")
            or payload.get("first_air_date")
            or payload.get("air_date")
        )
        genres = payload.get("genres") or payload.get("tags") or []
        credits = payload.get("credits") or {}
        normalized_credits = {"cast": [], "crew": []}
        people = []
        for credit_type, source in (("cast", credits.get("cast") or []), ("crew", credits.get("crew") or [])):
            for order, person in enumerate(source):
                if not isinstance(person, dict):
                    continue
                profile = person.get("profile_path")
                entry = {
                    "id": str(person.get("id")) if person.get("id") is not None else None,
                    "name": person.get("name"),
                    "role": person.get("character")
                    or person.get("job")
                    or person.get("known_for_department"),
                    "department": person.get("known_for_department")
                    or person.get("department"),
                    "creditType": credit_type,
                    "order": person.get("order", order),
                }
                if profile:
                    entry["imageUrl"] = f"https://image.tmdb.org/t/p/w500{profile}"
                if entry["name"]:
                    normalized_credits[credit_type].append(entry)
                    people.append(entry)
        videos = _normalize_trailers(
            (payload.get("videos") or {}).get("results", []), "tmdb"
        )
        return {
            "title": title,
            "overview": payload.get("overview"),
            "description": payload.get("overview"),
            "date": dates or None,
            "firstAired": payload.get("first_air_date"),
            "lastAired": payload.get("last_air_date"),
            "status": payload.get("status"),
            "runtimeMinutes": payload.get("runtime")
            or ((payload.get("episode_run_time") or [None])[0]),
            "seasonNumber": payload.get("season_number"),
            "episodeNumber": payload.get("episode_number"),
            "studios": _names(payload.get("production_companies")),
            "networks": _names(payload.get("networks")),
            "productionCompanies": _names(payload.get("production_companies")),
            "originalCountry": (payload.get("origin_country") or [None])[0],
            "year": (dates or "")[:4] or None,
            "tags": _names(genres),
            "originalLanguage": payload.get("original_language"),
            "trailers": videos,
            "people": people,
            "credits": normalized_credits,
            "provider": "tmdb",
            "providerId": provider_id,
            "ids": ids,
            "children": [
                {
                    "type": "season",
                    "season": value.get("season_number"),
                    "id": str(value.get("id")),
                }
                for value in payload.get("seasons", []) or []
                if value.get("id") is not None
                and value.get("season_number") is not None
            ],
            "images": self._images(entity_type, payload),
        }

    def _images(self, entity_type: str, payload: dict) -> list[dict]:
        images = payload.get("images") or {}
        values = []
        for image_type, key in (
            (PRIMARY, "posters"),
            (BACKDROP, "backdrops"),
            (LOGO, "logos"),
        ):
            for image in images.get(key, []) or []:
                path = image.get("file_path")
                if path:
                    language, provider_language = self._canonical_language(
                        image.get("iso_639_1")
                    )
                    values.append(
                        _image(
                            image_type,
                            f"https://image.tmdb.org/t/p/w1280{path}",
                            language=language,
                            provider_language=provider_language,
                            provider="tmdb",
                            source_type=key,
                            score=image.get("vote_average", 0),
                            width=image.get("width", 0),
                            height=image.get("height", 0),
                        )
                    )
        # TMDB calls episode artwork "stills". Stills are primary only for
        # episodes; season artwork must remain poster-only.
        if entity_type == "episode":
            still_path = payload.get("still_path")
            if still_path:
                values.append(
                    _image(
                        PRIMARY,
                        f"https://image.tmdb.org/t/p/w1280{still_path}",
                        provider="tmdb",
                        source_type="still_path",
                        width=payload.get("width", 0),
                        height=payload.get("height", 0),
                    )
                )
            for image in images.get("stills", []) or []:
                path = image.get("file_path")
                if path:
                    language, provider_language = self._canonical_language(
                        image.get("iso_639_1")
                    )
                    values.append(
                        _image(
                            PRIMARY,
                            f"https://image.tmdb.org/t/p/w1280{path}",
                            language=language,
                            provider_language=provider_language,
                            provider="tmdb",
                            source_type="stills",
                            score=image.get("vote_average", 0),
                            width=image.get("width", 0),
                            height=image.get("height", 0),
                        )
                    )
        return values


class TVDBClient(ProviderClient):
    base_url = "https://api4.thetvdb.com/v4"
    _tls_context = ssl.create_default_context()
    _tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    _tls_context.maximum_version = ssl.TLSVersion.TLSv1_2
    _tokens: dict[str, tuple[str, float]] = {}
    _lock = threading.Lock()
    _language_catalog_lock = threading.Lock()
    _language_catalog = ProviderLanguageCatalog()
    _language_codes_loaded = False
    _artwork_catalog_lock = threading.Lock()
    _artwork_types: dict[str, tuple[str | None, str]] = {}
    _artwork_types_loaded = False

    def __init__(self, credentials: dict, timeout: float = 20):
        super().__init__(timeout)
        self.credentials = credentials

    def _token(self) -> str:
        key = self.credentials.get("apiKey") or self.credentials.get("value")
        if not key:
            raise ProviderError("TheTVDB API key is empty")
        cache_key = f"{key}:{self.credentials.get('pin', '')}"
        with self._lock:
            cached = self._tokens.get(cache_key)
            if cached and cached[1] > time.time() + 30:
                return cached[0]
        body = {"apikey": key}
        if self.credentials.get("pin"):
            body["pin"] = self.credentials["pin"]
        try:
            response = httpx.post(
                f"{self.base_url}/login",
                json=body,
                timeout=self.timeout,
                verify=self._tls_context,
            )
            response.raise_for_status()
            token = response.json()["data"]["token"]
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {401, 403}:
                raise ProviderError(
                    "TheTVDB rejected the API key or subscriber PIN. Use a v4 project API key; user-supported keys also require the matching subscriber PIN."
                ) from error
            raise ProviderError(str(error)) from error
        except (httpx.HTTPError, KeyError, ValueError) as error:
            raise ProviderError(str(error)) from error
        with self._lock:
            self._tokens[cache_key] = (token, time.time() + 30 * 24 * 60 * 60)
        return token

    def _get(self, url: str, **kwargs) -> dict:
        return super()._get(url, verify=self._tls_context, **kwargs)

    def _request(self, path: str, params: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/json",
        }
        return self._get(f"{self.base_url}{path}", params=params or {}, headers=headers)

    def _language_code(self, locale: str) -> str:
        with self._language_catalog_lock:
            if not self.__class__._language_codes_loaded:
                try:
                    payload = self._request("/languages")
                    values = (
                        payload.get("data", [])
                        if isinstance(payload, dict)
                        else payload
                    )
                    if isinstance(values, dict):
                        values = [values]
                    for value in values if isinstance(values, list) else []:
                        if not isinstance(value, dict):
                            continue
                        provider_code = (
                            str(value.get("id") or value.get("shortCode") or "")
                            .strip()
                            .lower()
                        )
                        short_code = str(value.get("shortCode") or "").strip().lower()
                        canonical = _catalog_language_tag(
                            provider_code, value.get("name"), short_code
                        )
                        if provider_code and canonical:
                            self.__class__._language_catalog.register(
                                provider_code, canonical
                            )
                            if short_code:
                                self.__class__._language_catalog.register(
                                    short_code, canonical
                                )
                except ProviderError:
                    logger.warning(
                        "TVDB language catalog unavailable; using locale fallback"
                    )
                self.__class__._language_codes_loaded = True
        # Keep unknown locale values intact when the provider catalog cannot
        # resolve them. This lets newly supported locales pass through
        # without another provider-specific hard-coded table.
        return self._language_catalog.provider(locale)

    def _language_code_for_artwork(self, value: str | None) -> str | None:
        if not value:
            return None
        self._language_code(value)
        return self._language_catalog.canonical(value)

    def _load_artwork_types(self) -> None:
        with self._artwork_catalog_lock:
            if self.__class__._artwork_types_loaded:
                return
            try:
                payload = self._request("/artwork/types")
                values = (
                    payload.get("data", []) if isinstance(payload, dict) else payload
                )
                for value in values if isinstance(values, list) else []:
                    if not isinstance(value, dict) or value.get("id") is None:
                        continue
                    name = str(value.get("name") or value.get("slug") or "").lower()
                    record_type = str(value.get("recordType") or "").lower()
                    image_type = None
                    if "poster" in name:
                        image_type = PRIMARY
                    elif "background" in name:
                        image_type = BACKDROP
                    elif "clearlogo" in name or "clear logo" in name:
                        image_type = LOGO
                    elif "banner" in name:
                        image_type = BANNER
                    elif record_type == "episode" and "screencap" in name:
                        image_type = PRIMARY
                    self.__class__._artwork_types[str(value["id"])] = (
                        image_type,
                        record_type,
                    )
            except ProviderError:
                logger.warning(
                    "TVDB artwork type catalog unavailable; using payload fallback"
                )
            self.__class__._artwork_types_loaded = True

    def _artwork_type(self, entity_type: str, artwork: dict) -> str | None:
        self._load_artwork_types()
        key = str(artwork.get("type", ""))
        if key in self._artwork_types:
            image_type, record_type = self._artwork_types[key]
            expected_record_type = (
                "list" if entity_type == "collection" else entity_type
            )
            return (
                image_type
                if not record_type or record_type == expected_record_type
                else None
            )
        return _fallback_tvdb_image_type(entity_type, artwork)

    def test(self) -> None:
        # Use a catalog endpoint rather than a hard-coded media ID. The v4
        # catalog can legitimately omit legacy IDs such as series/1.
        self._request("/languages")

    def search(self, entity_type: str, query: str) -> list[dict]:
        payload = self._request(
            "/search",
            params={
                "query": query,
                "type": "movie" if entity_type == "movie" else "series",
            },
        )
        values = []
        for value in payload.get("data", []) or []:
            identifier = (
                value.get("tvdb_id") or value.get("objectID") or value.get("id")
            )
            if identifier:
                values.append(
                    {
                        "provider": "tvdb",
                        "providerId": str(identifier),
                        "title": value.get("name") or value.get("title"),
                        "year": value.get("year"),
                        "overview": value.get("overview")
                        or value.get("overview_translated"),
                    }
                )
        return values

    def lists(self, page: int = 0) -> list[dict]:
        payload = self._request("/lists", params={"page": page} if page else {})
        return payload.get("data") or []

    def list_details(self, list_id: str) -> dict:
        return self._request(f"/lists/{quote(list_id)}/extended")

    def details(self, entity_type: str, provider_id: str, locale: str) -> dict:
        endpoint = {
            "series": "series",
            "episode": "episodes",
            "season": "seasons",
            "movie": "movies",
            "collection": "lists",
        }.get(entity_type, "series")
        payload = self._request(f"/{endpoint}/{quote(provider_id)}/extended")
        # TheTVDB extended endpoint is not reliably English by default. Ask
        # for the requested translation explicitly, including English, so a
        # response cached under `en` cannot contain the provider's default
        # language.
        if locale:
            try:
                translated = self._request(
                    f"/{endpoint}/{quote(provider_id)}/translations/{quote(self._language_code(locale))}"
                )
                payload["translation"] = translated.get("data")
            except ProviderError as error:
                # TVDB does not expose translations for every entity (notably
                # some seasons). Extended metadata is still usable, so an
                # optional translation miss must not abort the whole scan.
                logger.debug(
                    "TVDB translation unavailable provider_id=%s entity_type=%s locale=%s error=%s",
                    provider_id,
                    entity_type,
                    locale,
                    error,
                )
        return payload

    def series_hierarchy(self, provider_id: str, season_type: str = "default") -> dict:
        """Fetch a series and all episode identities from TVDB's hierarchy API."""
        extended = self._request(f"/series/{quote(provider_id)}/extended")
        episodes = []
        page = 0
        while True:
            payload = self._request(
                f"/series/{quote(provider_id)}/episodes/{quote(season_type)}",
                params={"page": page},
            )
            data = payload.get("data") or {}
            episodes.extend(data.get("episodes") or [])
            links = payload.get("links") or {}
            if not links.get("next"):
                break
            page += 1
        return {"extended": extended, "episodes": episodes}

    def normalize(self, entity_type: str, provider_id: str, payload: dict) -> dict:
        data = payload.get("data", payload)
        translation = payload.get("translation") or {}
        ids = []
        for remote in data.get("remoteIds", []) or data.get("remote_ids", []) or []:
            source = str(
                remote.get("sourceName")
                or remote.get("sourceInfo")
                or remote.get("type")
                or ""
            ).lower()
            value = remote.get("id") or remote.get("value")
            if not value:
                continue
            if "tmdb" in source:
                ids.append(
                    {
                        "provider": "tmdb",
                        "identifierType": entity_type,
                        "id": str(value),
                    }
                )
            elif "imdb" in source:
                ids.append(
                    {"provider": "imdb", "identifierType": "imdb", "id": str(value)}
                )
        genres = data.get("genres") or data.get("tags") or []
        dates = (
            data.get("firstAired") or data.get("lastAired") or data.get("releaseDate")
        )
        trailers = _normalize_trailers(
            data.get("trailers") or data.get("videos") or [], "tvdb"
        )
        for trailer in trailers:
            if trailer.get("language"):
                trailer["language"] = (
                    self._language_code_for_artwork(str(trailer["language"]))
                    or trailer["language"]
                )
        normalized_credits = {"cast": [], "crew": []}
        people = []
        for order, person in enumerate(data.get("characters", []) or data.get("people", []) or []):
            if not isinstance(person, dict):
                continue
            image = person.get("image") or person.get("imageUrl")
            role = person.get("role") or person.get("character")
            department = person.get("department") or person.get("peopleType") or person.get("type")
            kind = str(person.get("creditType") or person.get("type") or person.get("peopleType") or "").lower()
            credit_type = "crew" if person.get("job") or any(value in kind for value in ("crew", "director", "writer", "producer")) else "cast"
            entry = {
                "id": str(person.get("id")) if person.get("id") is not None else None,
                "name": person.get("name") or person.get("personName"),
                "role": role or person.get("job"),
                "department": department,
                "creditType": credit_type,
                "order": person.get("sort") or person.get("order") or order,
            }
            if image:
                entry["imageUrl"] = image
            if entry["name"]:
                normalized_credits[credit_type].append(entry)
                people.append(entry)
        images, extra_images = _tvdb_images(
            entity_type, data, self._language_code_for_artwork, self._artwork_type
        )
        overview_translations = data.get("overviewTranslations") or []
        return {
            "title": translation.get("name") or data.get("name") or data.get("title"),
            "overview": translation.get("overview")
            or data.get("overview")
            or (overview_translations[0] if overview_translations else None),
            "description": translation.get("overview") or data.get("overview"),
            "date": dates or None,
            "firstAired": data.get("firstAired"),
            "lastAired": data.get("lastAired"),
            "airTime": data.get("airs", {}).get("time")
            if isinstance(data.get("airs"), dict)
            else data.get("airTime"),
            "status": _name(data.get("status")),
            "studios": _names(data.get("studios")),
            "networks": _names(data.get("networks") or data.get("network")),
            "productionCompanies": _names(
                data.get("productionCompanies") or data.get("companies")
            ),
            "runtimeMinutes": data.get("averageRuntime") or data.get("runtime"),
            "seasonNumber": data.get("seasonNumber")
            if data.get("seasonNumber") is not None
            else data.get("number")
            if entity_type == "season"
            else data.get("season"),
            "episodeNumber": data.get("number")
            if entity_type == "episode"
            else data.get("episodeNumber"),
            "originalCountry": _name(
                data.get("originalCountry") or data.get("originalCountryName")
            ),
            "year": str(data.get("year") or dates or "")[:4] or None,
            "tags": _names(genres),
            "originalLanguage": _catalog_language_tag(
                str(data.get("originalLanguage") or "")
            )
            or data.get("originalLanguage"),
            "trailers": trailers,
            "people": people,
            "credits": normalized_credits,
            "provider": "tvdb",
            "providerId": provider_id,
            "ids": ids,
            "children": _tvdb_children(data),
            "images": images,
            "extraImages": extra_images,
        }


class MusicBrainzClient(ProviderClient):
    base_url = "https://musicbrainz.org/ws/2"
    _lock = threading.Lock()
    _last_request = 0.0

    def _request(self, path: str, params: dict | None = None) -> dict:
        with self._lock:
            wait = 1.0 - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self.__class__._last_request = time.monotonic()
        params = dict(params or {})
        params["fmt"] = "json"
        return self._get(
            f"{self.base_url}{path}",
            params=params,
            headers={
                "User-Agent": f"ZenStream/{__version__} (https://zenstream.amai.space)",
                "Accept": "application/json",
            },
        )

    def details(self, entity_type: str, provider_id: str, locale: str) -> dict:
        endpoint = {
            "artist": "artist",
            "release": "release",
            "release_group": "release-group",
            "track": "recording",
            "recording": "recording",
            "work": "work",
        }.get(entity_type, "release")
        payload = self._request(
            f"/{endpoint}/{quote(provider_id)}",
            {
                "inc": "artist-credits+aliases+releases+release-groups+recordings+relationships+tags+media"
            },
        )
        if endpoint in {"release", "release-group"}:
            try:
                payload["_coverArt"] = self._get(
                    f"https://coverartarchive.org/{endpoint}/{quote(provider_id)}",
                    headers={"Accept": "application/json"},
                )
            except ProviderError:
                payload["_coverArt"] = {}
        return payload

    def search(self, entity_type: str, query: str) -> list[dict]:
        endpoint = {
            "artist": "artist",
            "release": "release",
            "track": "recording",
            "recording": "recording",
        }.get(entity_type, "release")
        field = {
            "artist": "artist",
            "release": "release",
            "track": "recording",
            "recording": "recording",
        }.get(entity_type, "release")
        payload = self._request(
            f"/{endpoint}", {"query": f'{field}:"{query}"', "limit": 10}
        )
        values = payload.get(f"{endpoint}s", []) or []
        return [
            {
                "provider": "musicbrainz",
                "providerId": str(value.get("id")),
                "title": value.get("name") or value.get("title"),
                "year": str(value.get("first-release-date") or value.get("date") or "")[
                    :4
                ]
                or None,
            }
            for value in values
            if value.get("id")
        ]

    def normalize(self, entity_type: str, provider_id: str, payload: dict) -> dict:
        images = []
        extra_images = []
        if entity_type in {"release", "release_group"}:
            archive_key = payload.get("cover-art-archive") or {}
            endpoint = "release-group" if entity_type == "release_group" else "release"
            cover_art = payload.get("_coverArt") or {}
            for artwork in cover_art.get("images", []) or []:
                image_type = PRIMARY if artwork.get("front") else None
                image_url = artwork.get("image") or artwork.get("thumbnails", {}).get(
                    "500"
                )
                if not image_url:
                    continue
                value = (
                    _image(
                        image_type,
                        image_url,
                        provider="musicbrainz",
                        source_type=", ".join(artwork.get("types", []) or [])
                        or "cover-art",
                        width=artwork.get("width", 0),
                        height=artwork.get("height", 0),
                    )
                    if image_type
                    else {
                        "sourceType": ", ".join(artwork.get("types", []) or [])
                        or "cover-art",
                        "url": image_url,
                        "provider": "musicbrainz",
                    }
                )
                (images if image_type else extra_images).append(value)
            if not images and (archive_key.get("front") or entity_type == "release"):
                images.append(
                    _image(
                        PRIMARY,
                        f"https://coverartarchive.org/{endpoint}/{quote(provider_id)}/front-500",
                        provider="musicbrainz",
                        source_type="front",
                        width=500,
                    )
                )
        external_ids = []
        for relationship in (
            payload.get("relations", []) or payload.get("relationships", []) or []
        ):
            resource = (
                ((relationship.get("url") or {}).get("resource") or "")
                if isinstance(relationship, dict)
                else ""
            )
            match = re.search(
                r"(?:themoviedb\.org/(?:movie|tv)|thetvdb\.com/(?:series|movies)|imdb\.com/title)/(?:.*?/)?(\d+|tt\d+)",
                resource,
                re.I,
            )
            if not match:
                continue
            provider = (
                "imdb"
                if "imdb.com" in resource.lower()
                else "tmdb"
                if "themoviedb.org" in resource.lower()
                else "tvdb"
            )
            external_ids.append(
                {
                    "provider": provider,
                    "identifierType": entity_type,
                    "id": match.group(1),
                }
            )
        date = payload.get("first-release-date") or payload.get("date")
        tags = _names(payload.get("tags"))
        credits = []
        for value in payload.get("artist-credit", []) or []:
            artist = value.get("artist") or {}
            if artist.get("id") or artist.get("name"):
                credits.append(
                    {
                        "id": artist.get("id"),
                        "name": artist.get("name"),
                        "joinPhrase": value.get("joinphrase"),
                    }
                )
        tracks = []
        for medium in payload.get("media", []) or []:
            for position, track in enumerate(medium.get("tracks", []) or [], start=1):
                recording = track.get("recording") or {}
                tracks.append(
                    {
                        "id": recording.get("id") or track.get("id"),
                        "title": track.get("title") or recording.get("title"),
                        "position": track.get("position") or position,
                        "disc": medium.get("position"),
                        "length": track.get("length") or recording.get("length"),
                    }
                )
        if entity_type == "track" and not tracks:
            tracks = [
                {
                    "id": provider_id,
                    "title": payload.get("title") or payload.get("name"),
                    "position": payload.get("position"),
                }
            ]
        return {
            "title": payload.get("name") or payload.get("title"),
            "overview": None,
            "description": None,
            "date": date,
            "releaseDate": date,
            "year": str(date or "")[:4] or None,
            "tags": tags,
            "originalLanguage": None,
            "albumArtist": credits[0]["name"] if credits else None,
            "artists": credits,
            "tracks": tracks,
            "provider": "musicbrainz",
            "providerId": provider_id,
            "ids": external_ids,
            "images": images,
            "extraImages": extra_images,
        }


def _fallback_tvdb_image_type(entity_type: str, artwork: dict) -> str | None:
    raw_type = artwork.get("type", "")
    kind = str(raw_type).lower()
    if any(value in kind for value in ("background", "backdrop")):
        return BACKDROP
    if any(value in kind for value in ("clearlogo", "clear logo", "logo")):
        return LOGO
    if any(value in kind for value in ("banner", "thumbnail", "thumb")):
        return BANNER
    if entity_type == "episode" and any(
        value in kind for value in ("still", "episode", "screencap")
    ):
        return PRIMARY
    if any(
        value in kind
        for value in ("poster", "series", "movie", "season", "box", "cover")
    ):
        return PRIMARY
    width = artwork.get("width") or 0
    height = artwork.get("height") or 0
    return BANNER if width and height and float(width) / float(height) > 1.45 else None


def _tvdb_images(
    entity_type: str, data: dict, normalize_language=None, normalize_type=None
) -> tuple[list[dict], list[dict]]:
    values = []
    extras = []
    for artwork in data.get("artworks", []) or data.get("artwork", []) or []:
        url = (
            artwork.get("image") or artwork.get("thumbnail") or artwork.get("imageUrl")
        )
        if not url:
            continue
        raw_type = artwork.get("type", "")
        image_type = (
            normalize_type(entity_type, artwork)
            if normalize_type
            else _fallback_tvdb_image_type(entity_type, artwork)
        )
        provider_language = artwork.get("language") or artwork.get("lang")
        language = provider_language
        if normalize_language:
            language = normalize_language(language)
        if image_type:
            value = _image(
                image_type,
                url,
                language=language,
                provider_language=provider_language,
                provider="tvdb",
                source_type=str(raw_type),
                score=artwork.get("score", 0),
                width=artwork.get("width", 0),
                height=artwork.get("height", 0),
            )
        else:
            value = {
                "sourceType": str(raw_type),
                "url": url,
                "provider": "tvdb",
                "language": language,
                "score": artwork.get("score", 0) or 0,
                "width": artwork.get("width", 0) or 0,
                "height": artwork.get("height", 0) or 0,
            }
            if (
                provider_language
                and str(provider_language).lower() != str(language or "").lower()
            ):
                value["providerLanguage"] = provider_language
        (values if image_type else extras).append(value)
    if data.get("image") and entity_type in {"series", "season", "episode", "movie"}:
        values.append(
            _image(PRIMARY, data["image"], provider="tvdb", source_type="image")
        )
    return values, extras


def _tvdb_children(data: dict) -> list[dict]:
    values = []
    for season in data.get("seasons", []) or []:
        season_number = season.get("seasonNumber")
        if season_number is None:
            season_number = season.get("number")
        if season.get("id") is not None and season_number is not None:
            values.append(
                {"type": "season", "season": season_number, "id": str(season["id"])}
            )
    for episode in data.get("episodes", []) or []:
        season_number = episode.get("seasonNumber")
        if season_number is None:
            season_number = episode.get("season")
        episode_number = episode.get("number")
        if episode_number is None:
            episode_number = episode.get("episodeNumber")
        if (
            episode.get("id") is not None
            and season_number is not None
            and episode_number is not None
        ):
            values.append(
                {
                    "type": "episode",
                    "season": season_number,
                    "episode": episode_number,
                    "id": str(episode["id"]),
                }
            )
    return values


class MetadataService:
    _fetch_locks_guard = threading.Lock()
    _fetch_locks: dict[tuple[str, str, str, str], threading.Lock] = {}

    def __init__(self):
        self.credentials = MetadataCredentials()
        self.cache = MetadataCache()

    def language_options(self) -> list[dict[str, str]]:
        """Return canonical metadata locales known by configured providers."""
        values = {
            "en",
            "ja",
            "zh-CN",
            "zh-TW",
            "ko",
            "fr",
            "de",
            "es",
            "it",
            "pt",
            "ru",
        }
        for provider, client_type in (("tmdb", TMDBClient), ("tvdb", TVDBClient)):
            try:
                client = self.client(provider)
                client._language_code("en")
                values.update(
                    client_type._language_catalog.provider_to_canonical.values()
                )
            except Exception:
                logger.debug(
                    "metadata language catalog unavailable provider=%s",
                    provider,
                    exc_info=True,
                )
        return [
            {"value": value, "label": value}
            for value in sorted(values, key=lambda item: (item != "en", item.lower()))
        ]

    @classmethod
    def _lock_for(
        cls, provider: str, entity_type: str, provider_id: str, locale: str
    ) -> threading.Lock:
        key = (provider, entity_type, provider_id, locale or "en")
        with cls._fetch_locks_guard:
            lock = cls._fetch_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._fetch_locks[key] = lock
            return lock

    def client(self, provider: str):
        if provider == "musicbrainz":
            return MusicBrainzClient()
        credential = self.credentials.get(provider)
        if not credential:
            raise ProviderError(f"{provider.upper()} is not configured")
        if provider == "tmdb":
            return TMDBClient(
                credential,
                self.credentials.configured()[provider].get(
                    "credentialType", "api_key"
                ),
            )
        if provider == "tvdb":
            return TVDBClient(credential)
        raise ProviderError(f"Unsupported metadata provider '{provider}'")

    def test(
        self,
        provider: str,
        credential: dict | None = None,
        credential_type: str = "api_key",
    ) -> None:
        if provider == "tmdb":
            TMDBClient(
                credential or self.credentials.get(provider) or {}, credential_type
            ).test()
        elif provider == "tvdb":
            TVDBClient(credential or self.credentials.get(provider) or {}).test()
        elif provider == "musicbrainz":
            MusicBrainzClient()._request("/artist/00000000-0000-0000-0000-000000000000")
        else:
            raise ProviderError("Unsupported provider")

    def fetch(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        force: bool = False,
    ) -> dict:
        lock = self._lock_for(provider, entity_type, provider_id, locale)
        with lock:
            # Another request may have populated the cache while this request
            # was waiting. Re-check before making a provider call.
            cached = self.cache.get(provider, entity_type, provider_id, locale)
            if cached and not force and not cached.pop("_stale", False):
                return cached
            logger.debug(
                "metadata fetch provider=%s entity_type=%s provider_id=%s locale=%s force=%s",
                provider,
                entity_type,
                provider_id,
                locale,
                force,
            )
            client = self.client(provider)
            try:
                payload = client.details(entity_type, provider_id, locale)
                normalized = client.normalize(entity_type, provider_id, payload)
            except Exception as error:
                logger.exception(
                    "metadata fetch failed provider=%s entity_type=%s provider_id=%s locale=%s",
                    provider,
                    entity_type,
                    provider_id,
                    locale,
                )
                if isinstance(error, ProviderError):
                    raise
                raise ProviderError(
                    f"{provider} {entity_type} {provider_id} details normalization failed: {type(error).__name__}: {error}"
                ) from error
            self.cache.put(provider, entity_type, provider_id, locale, normalized)
            from app.metadata_services import MetadataSearchProjection
            MetadataSearchProjection(self.cache.db).project(provider, entity_type, provider_id, locale, normalized)
            logger.info(
                "metadata cached provider=%s entity_type=%s provider_id=%s locale=%s images=%d",
                provider,
                entity_type,
                provider_id,
                locale,
                len(normalized.get("images", [])),
            )
            return normalized

    def _cache_normalized(
        self,
        provider: str,
        entity_type: str,
        provider_id: str,
        locale: str,
        client,
        payload: dict,
    ) -> dict:
        normalized = client.normalize(entity_type, provider_id, payload)
        self.cache.put(provider, entity_type, provider_id, locale, normalized)
        from app.metadata_services import MetadataSearchProjection
        MetadataSearchProjection(self.cache.db).project(provider, entity_type, provider_id, locale, normalized)
        logger.info(
            "metadata cached provider=%s entity_type=%s provider_id=%s locale=%s images=%d",
            provider,
            entity_type,
            provider_id,
            locale,
            len(normalized.get("images", [])),
        )
        return normalized

    def fetch_for_identity(self, provider: str, entity_type: str, provider_id: str) -> dict:
        """Fetch an unlocalized identity/hierarchy response without caching metadata."""
        client = self.client(provider)
        return client.normalize(entity_type, provider_id, client.details(entity_type, provider_id, "en"))

    def aggregate_series(
        self, provider: str, provider_id: str, locale: str = "en", force: bool = False
    ) -> dict:
        """Cache a resolved series and its season/episode hierarchy in batches."""
        client = self.client(provider)
        records = {"series": None, "seasons": [], "episodes": []}
        if provider == "tvdb":
            hierarchy = client.series_hierarchy(provider_id)
            # TVDB's hierarchy endpoint has no locale parameter and its
            # extended series record is commonly returned in the catalogue's
            # default language.  Caching that record under every configured
            # locale makes a Japanese title appear in the English cache until
            # a manual metadata refresh runs.  Fetch the series through the
            # locale-aware translation endpoint instead.  Legacy hierarchy
            # rows without the provenance marker are deliberately refreshed
            # once so existing libraries repair themselves on their next
            # series scan.
            cached = self.cache.get(provider, "series", provider_id, locale)
            cache_matches_locale = cached and cached.get("_metadataLocale") == locale
            records["series"] = self.fetch(
                provider,
                "series",
                provider_id,
                locale,
                force=force or not cache_matches_locale,
            )

            seasons = (hierarchy["extended"].get("data") or {}).get("seasons") or []
            for season in seasons:
                season_type = season.get("type")
                if isinstance(season_type, dict):
                    season_type = season_type.get("type")
                if season_type and str(season_type).lower() not in {
                    "official",
                    "aired",
                    "default",
                }:
                    continue
                number = season.get("seasonNumber")
                if number is None:
                    number = season.get("number")
                if season.get("id") is None or number is None:
                    continue
                season_id = str(season["id"])
                # This aggregate is an identity-mapping pass.  Child
                # metadata is fetched once by LibraryScanner._seed_all_children;
                # fetching every season here would duplicate provider work for
                # every configured locale and block the shared database.
                normalized = client.normalize("season", season_id, {"data": season})
                records["seasons"].append(normalized)
            for episode in hierarchy["episodes"]:
                episode_id = episode.get("id")
                season_number = episode.get("seasonNumber")
                if season_number is None:
                    season_number = episode.get("season")
                episode_number = episode.get("number")
                if episode_number is None:
                    episode_number = episode.get("episodeNumber")
                if (
                    episode_id is None
                    or season_number is None
                    or episode_number is None
                ):
                    continue
                normalized = client.normalize(
                    "episode", str(episode_id), {"data": episode}
                )
                records["episodes"].append(normalized)
            return records
        if provider == "tmdb":
            hierarchy = client.series_hierarchy(provider_id, locale)
            records["series"] = self._cache_normalized(
                provider, "series", provider_id, locale, client, hierarchy["series"]
            )
            for value in hierarchy["seasons"]:
                details = value["details"]
                season_number = details.get("season_number")
                if season_number is None:
                    season_number = value["summary"].get("season_number")
                if season_number is None:
                    continue
                season_id = f"{provider_id}:{season_number}"
                records["seasons"].append(
                    self._cache_normalized(
                        provider, "season", season_id, locale, client, details
                    )
                )
                for episode in details.get("episodes", []) or []:
                    episode_number = episode.get("episode_number")
                    if episode_number is None:
                        continue
                    episode_id = f"{provider_id}:{season_number}:{episode_number}"
                    normalized = self._cache_normalized(
                        provider, "episode", episode_id, locale, client, episode
                    )
                    if not normalized.get("title") or not any(
                        image.get("type") == PRIMARY
                        for image in normalized.get("images", [])
                    ):
                        try:
                            normalized = self.fetch(
                                provider, "episode", episode_id, locale, force=True
                            )
                        except ProviderError:
                            if not normalized.get("title"):
                                raise
                    records["episodes"].append(normalized)
            return records
        raise ProviderError(f"Series aggregation is unsupported for {provider}")

    def resolve_inventory_entity(
        self,
        entity_type: str,
        query: str,
        year: str | None = None,
        explicit_ids: list[dict] | None = None,
    ) -> dict:
        """Resolve an inventory entity from its authoritative provider.

        Series and movies must be matched by TVDB and TMDB respectively. The
        selected primary record is then the only source of secondary IDs via
        its external/remote database links; searching secondary providers by
        title can match a different entity with the same name.
        """
        priorities = {
            "series": ["tvdb", "tmdb"],
            "movie": ["tmdb", "tvdb"],
            "artist": ["musicbrainz"],
            "release": ["musicbrainz"],
            "track": ["musicbrainz"],
            "recording": ["musicbrainz"],
        }.get(entity_type, [])
        if not priorities:
            raise ProviderError(
                f"No provider resolution strategy exists for {entity_type}"
            )
        primary_provider = priorities[0]
        explicit_by_provider = {
            value["provider"]: value["id"]
            for value in (explicit_ids or [])
            if value.get("provider") == primary_provider and value.get("id")
        }
        errors: list[str] = []
        normalized: dict | None = None
        try:
            client = self.client(primary_provider)
            provider_id = explicit_by_provider.get(primary_provider)
            if not provider_id:
                candidates = (
                    client.search(entity_type, query, year)
                    if primary_provider == "tmdb"
                    else client.search(entity_type, query)
                )
                provider_id = _select_match(candidates, query, year)
            normalized = self.fetch(
                primary_provider, entity_type, str(provider_id), "en", force=True
            )
            explicit_by_provider[primary_provider] = str(provider_id)
            for value in normalized.get("ids", []) or []:
                provider = value.get("provider")
                if provider in {"tmdb", "tvdb", "imdb"} and value.get("id"):
                    explicit_by_provider.setdefault(provider, str(value["id"]))
        except (ProviderError, ValueError, KeyError) as error:
            errors.append(f"{primary_provider}: {error}")
        if not normalized or primary_provider not in explicit_by_provider:
            detail = "; ".join(errors) or "no matching provider result"
            raise ProviderError(f"Could not resolve {entity_type} '{query}': {detail}")
        return {
            "metadata": normalized,
            "providerIds": [
                {"provider": provider, "id": provider_id}
                for provider, provider_id in explicit_by_provider.items()
            ],
        }


def _normalized_match_text(value: str) -> str:
    value = (
        unicodedata.normalize("NFKD", value or "")
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _select_match(candidates: list[dict], query: str, year: str | None = None) -> str:
    wanted = _normalized_match_text(query)
    scored = []
    for candidate in candidates:
        title = _normalized_match_text(str(candidate.get("title") or ""))
        if not title:
            continue
        score = (
            100 if title == wanted else 75 if wanted in title or title in wanted else 0
        )
        if (
            year
            and candidate.get("year")
            and str(candidate["year"])[:4] == str(year)[:4]
        ):
            score += 20
        if score:
            scored.append((score, str(candidate["providerId"])))
    scored.sort(reverse=True)
    if (
        not scored
        or scored[0][0] < 95
        or (len(scored) > 1 and scored[0][0] == scored[1][0])
    ):
        raise ProviderError(f"No unique high-confidence match for '{query}'")
    return scored[0][1]


def choose_image(images: list[dict], requested: str, image_type: str) -> dict | None:
    return choose_artwork(images, requested, image_type, None, [])


def _language_family(value: str) -> str:
    """Normalize a language tag to its base code without provider assumptions."""
    return (value or "").lower().split("-", 1)[0].split("_", 1)[0]
