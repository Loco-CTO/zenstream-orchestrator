"""Small provider clients used by the administrator library preview."""

from __future__ import annotations

import threading
import time
import re
import unicodedata
from typing import Any
from pathlib import Path
from urllib.parse import quote

import httpx

from app.models.metadata import MetadataCache, MetadataCredentials
from app.logging_config import get_logger
from version import __version__


class ProviderError(RuntimeError):
    pass


logger = get_logger("providers")


PRIMARY = "Primary"
BACKDROP = "Backdrop"
LOGO = "Logo"
BANNER = "Banner"
IMAGE_TYPES = {PRIMARY, BACKDROP, LOGO, BANNER}

# The first provider is authoritative for identity and hierarchy resolution.
# Secondary providers may enrich the same entity but must never be flagged as
# the entity's primary ID.
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


def _image(image_type: str, url: str, *, language: str | None = None, provider: str,
           source_type: str | None = None, score: float = 0, width: int = 0,
           height: int = 0) -> dict:
    value = {
        "type": image_type,
        "language": language,
        "url": url,
        "score": score or 0,
        "width": width or 0,
        "height": height or 0,
        "provider": provider,
    }
    if source_type is not None:
        value["sourceType"] = source_type
    return value


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name") or value.get("title") or value.get("value")
    return str(value) if value else None


def _names(values: Any) -> list[str]:
    return [value for value in (_name(item) for item in values or []) if value]


class ProviderClient:
    def __init__(self, timeout: float = 20):
        self.timeout = timeout

    def _get(self, url: str, **kwargs) -> dict:
        try:
            response = httpx.get(url, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderError(f"provider request failed: {type(error).__name__}: {error}") from error


class TMDBClient(ProviderClient):
    base_url = "https://api.themoviedb.org/3"

    def __init__(self, credentials: dict, credential_type: str = "api_key", timeout: float = 20):
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

    def test(self) -> None:
        self._request("/configuration")

    def search(self, entity_type: str, query: str, year: str | None = None) -> list[dict]:
        kind = "tv" if entity_type in {"series", "episode"} else "movie"
        params = {"query": query, "language": "en"}
        if year and year.isdigit():
            params["year" if kind == "movie" else "first_air_date_year"] = year
        payload = self._request(f"/search/{kind}", params=params)
        return [{"provider": "tmdb", "providerId": str(value.get("id")), "title": value.get("title") or value.get("name"), "year": (value.get("release_date") or value.get("first_air_date") or "")[:4] or None, "overview": value.get("overview")} for value in payload.get("results", []) if value.get("id")]

    def details(self, entity_type: str, provider_id: str, locale: str) -> dict:
        if entity_type == "season":
            series_id, season = provider_id.split(":", 1)
            return self._request(f"/tv/{quote(series_id)}/season/{quote(season)}", params={"language": locale, "append_to_response": "images,external_ids,videos"})
        if entity_type == "episode":
            series_id, season, episode = provider_id.split(":", 2)
            return self._request(f"/tv/{quote(series_id)}/season/{quote(season)}/episode/{quote(episode)}", params={"language": locale, "append_to_response": "images,external_ids,videos"})
        kind = "tv" if entity_type == "series" else "movie"
        return self._request(f"/{kind}/{quote(provider_id)}", params={"language": locale, "append_to_response": "images,external_ids,credits,videos"})

    def series_hierarchy(self, provider_id: str, locale: str) -> dict:
        """Fetch season details, whose responses include their episode lists."""
        series = self.details("series", provider_id, locale)
        seasons = []
        for summary in series.get("seasons", []) or []:
            season_number = summary.get("season_number")
            if season_number is None:
                continue
            season_id = f"{provider_id}:{season_number}"
            seasons.append({"summary": summary, "details": self.details("season", season_id, locale)})
        return {"series": series, "seasons": seasons}

    def normalize(self, entity_type: str, provider_id: str, payload: dict) -> dict:
        title = payload.get("title") or payload.get("name") or payload.get("original_title") or payload.get("original_name")
        external = payload.get("external_ids") or {}
        ids = []
        if external.get("tvdb_id"):
            ids.append({"provider": "tvdb", "identifierType": entity_type, "id": str(external["tvdb_id"])})
        if external.get("imdb_id"):
            ids.append({"provider": "imdb", "identifierType": "imdb", "id": str(external["imdb_id"])})
        dates = payload.get("release_date") or payload.get("first_air_date") or payload.get("air_date")
        genres = payload.get("genres") or payload.get("tags") or []
        credits = payload.get("credits") or {}
        people = []
        for person in (credits.get("cast") or []) + (credits.get("crew") or []):
            profile = person.get("profile_path")
            entry = {
                "id": str(person.get("id")) if person.get("id") is not None else None,
                "name": person.get("name"),
                "role": person.get("character") or person.get("job") or person.get("known_for_department"),
                "department": person.get("known_for_department") or person.get("department"),
            }
            if profile:
                entry["image"] = _image(PRIMARY, f"https://image.tmdb.org/t/p/w500{profile}", provider="tmdb", source_type="profile", width=500)
            if entry["name"]:
                people.append(entry)
        videos = [
            {"name": video.get("name"), "key": video.get("key"), "language": video.get("iso_639_1"), "type": video.get("type"), "site": video.get("site")}
            for video in (payload.get("videos") or {}).get("results", []) or []
            if video.get("key") and video.get("site")
        ]
        return {
            "title": title,
            "overview": payload.get("overview"),
            "description": payload.get("overview"),
            "date": dates or None,
            "firstAired": payload.get("first_air_date"),
            "lastAired": payload.get("last_air_date"),
            "status": payload.get("status"),
            "runtimeMinutes": payload.get("runtime") or ((payload.get("episode_run_time") or [None])[0]),
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
            "provider": "tmdb",
            "providerId": provider_id,
            "ids": ids,
            "children": [{"type": "season", "season": value.get("season_number"), "id": str(value.get("id"))} for value in payload.get("seasons", []) or [] if value.get("id") is not None and value.get("season_number") is not None],
            "images": self._images(entity_type, payload),
        }

    @staticmethod
    def _images(entity_type: str, payload: dict) -> list[dict]:
        images = payload.get("images") or {}
        values = []
        for image_type, key in ((PRIMARY, "posters"), (BACKDROP, "backdrops"), (LOGO, "logos")):
            for image in images.get(key, []) or []:
                path = image.get("file_path")
                if path:
                    values.append(_image(image_type, f"https://image.tmdb.org/t/p/w1280{path}", language=image.get("iso_639_1"), provider="tmdb", source_type=key, score=image.get("vote_average", 0), width=image.get("width", 0), height=image.get("height", 0)))
        # TMDB calls episode artwork "stills". Stills are primary only for
        # episodes; season artwork must remain poster-only.
        if entity_type == "episode":
            still_path = payload.get("still_path")
            if still_path:
                values.append(_image(PRIMARY, f"https://image.tmdb.org/t/p/w1280{still_path}", provider="tmdb", source_type="still_path", width=payload.get("width", 0), height=payload.get("height", 0)))
            for image in images.get("stills", []) or []:
                path = image.get("file_path")
                if path:
                    values.append(_image(PRIMARY, f"https://image.tmdb.org/t/p/w1280{path}", language=image.get("iso_639_1"), provider="tmdb", source_type="stills", score=image.get("vote_average", 0), width=image.get("width", 0), height=image.get("height", 0)))
        return values


class TVDBClient(ProviderClient):
    base_url = "https://api4.thetvdb.com/v4"
    _tokens: dict[str, tuple[str, float]] = {}
    _lock = threading.Lock()

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
            response = httpx.post(f"{self.base_url}/login", json=body, timeout=self.timeout)
            response.raise_for_status()
            token = response.json()["data"]["token"]
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {401, 403}:
                raise ProviderError("TheTVDB rejected the API key or subscriber PIN. Use a v4 project API key; user-supported keys also require the matching subscriber PIN.") from error
            raise ProviderError(str(error)) from error
        except (httpx.HTTPError, KeyError, ValueError) as error:
            raise ProviderError(str(error)) from error
        with self._lock:
            self._tokens[cache_key] = (token, time.time() + 30 * 24 * 60 * 60)
        return token

    def _request(self, path: str, params: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        return self._get(f"{self.base_url}{path}", params=params or {}, headers=headers)

    def test(self) -> None:
        # Use a catalog endpoint rather than a hard-coded media ID. The v4
        # catalog can legitimately omit legacy IDs such as series/1.
        self._request("/languages")

    def search(self, entity_type: str, query: str) -> list[dict]:
        payload = self._request("/search", params={"query": query, "type": "movie" if entity_type == "movie" else "series"})
        values = []
        for value in payload.get("data", []) or []:
            identifier = value.get("tvdb_id") or value.get("objectID") or value.get("id")
            if identifier:
                values.append({"provider": "tvdb", "providerId": str(identifier), "title": value.get("name") or value.get("title"), "year": value.get("year"), "overview": value.get("overview") or value.get("overview_translated")})
        return values

    def lists(self, page: int = 0) -> list[dict]:
        payload = self._request("/lists", params={"page": page} if page else {})
        return payload.get("data") or []

    def list_details(self, list_id: str) -> dict:
        return self._request(f"/lists/{quote(list_id)}/extended")

    def details(self, entity_type: str, provider_id: str, locale: str) -> dict:
        endpoint = {"series": "series", "episode": "episodes", "season": "seasons", "movie": "movies", "collection": "lists"}.get(entity_type, "series")
        payload = self._request(f"/{endpoint}/{quote(provider_id)}/extended")
        if locale and locale not in {"en", "eng"}:
            translated = self._request(f"/{endpoint}/{quote(provider_id)}/translations/{quote(_tvdb_language(locale))}")
            payload["translation"] = translated.get("data")
        return payload

    def series_hierarchy(self, provider_id: str, season_type: str = "default") -> dict:
        """Fetch a series and all episode identities from TVDB's hierarchy API."""
        extended = self._request(f"/series/{quote(provider_id)}/extended")
        episodes = []
        page = 0
        while True:
            payload = self._request(f"/series/{quote(provider_id)}/episodes/{quote(season_type)}", params={"page": page})
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
            source = str(remote.get("sourceName") or remote.get("sourceInfo") or remote.get("type") or "").lower()
            value = remote.get("id") or remote.get("value")
            if not value:
                continue
            if "tmdb" in source:
                ids.append({"provider": "tmdb", "identifierType": entity_type, "id": str(value)})
            elif "imdb" in source:
                ids.append({"provider": "imdb", "identifierType": "imdb", "id": str(value)})
        genres = data.get("genres") or data.get("tags") or []
        dates = data.get("firstAired") or data.get("lastAired") or data.get("releaseDate")
        trailers = data.get("trailers") or data.get("videos") or []
        people = []
        for person in data.get("characters", []) or data.get("people", []) or []:
            image = person.get("image") or person.get("imageUrl")
            entry = {"id": str(person.get("id")) if person.get("id") is not None else None, "name": person.get("name") or person.get("personName"), "role": person.get("role") or person.get("character")}
            if image:
                entry["image"] = _image(PRIMARY, image, provider="tvdb", source_type="person")
            if entry["name"]:
                people.append(entry)
        images, extra_images = _tvdb_images(entity_type, data)
        overview_translations = data.get("overviewTranslations") or []
        return {
            "title": translation.get("name") or data.get("name") or data.get("title"),
            "overview": translation.get("overview") or data.get("overview") or (overview_translations[0] if overview_translations else None),
            "description": translation.get("overview") or data.get("overview"),
            "date": dates or None,
            "firstAired": data.get("firstAired"),
            "lastAired": data.get("lastAired"),
            "airTime": data.get("airs", {}).get("time") if isinstance(data.get("airs"), dict) else data.get("airTime"),
            "status": _name(data.get("status")),
            "studios": _names(data.get("studios")),
            "networks": _names(data.get("networks") or data.get("network")),
            "productionCompanies": _names(data.get("productionCompanies") or data.get("companies")),
            "runtimeMinutes": data.get("averageRuntime") or data.get("runtime"),
            "seasonNumber": data.get("seasonNumber") if data.get("seasonNumber") is not None else data.get("number") if entity_type == "season" else data.get("season"),
            "episodeNumber": data.get("number") if entity_type == "episode" else data.get("episodeNumber"),
            "originalCountry": _name(data.get("originalCountry") or data.get("originalCountryName")),
            "year": str(data.get("year") or dates or "")[:4] or None,
            "tags": _names(genres),
            "originalLanguage": data.get("originalLanguage"),
            "trailers": trailers,
            "people": people,
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
        return self._get(f"{self.base_url}{path}", params=params, headers={"User-Agent": f"ZenStream/{__version__} (https://zenstream.amai.space)", "Accept": "application/json"})

    def details(self, entity_type: str, provider_id: str, locale: str) -> dict:
        endpoint = {"artist": "artist", "release": "release", "release_group": "release-group", "track": "recording", "recording": "recording", "work": "work"}.get(entity_type, "release")
        payload = self._request(f"/{endpoint}/{quote(provider_id)}", {"inc": "artist-credits+aliases+releases+release-groups+recordings+relationships+tags+media"})
        if endpoint in {"release", "release-group"}:
            try:
                payload["_coverArt"] = self._get(f"https://coverartarchive.org/{endpoint}/{quote(provider_id)}", headers={"Accept": "application/json"})
            except ProviderError:
                payload["_coverArt"] = {}
        return payload

    def search(self, entity_type: str, query: str) -> list[dict]:
        endpoint = {"artist": "artist", "release": "release", "track": "recording", "recording": "recording"}.get(entity_type, "release")
        field = {"artist": "artist", "release": "release", "track": "recording", "recording": "recording"}.get(entity_type, "release")
        payload = self._request(f"/{endpoint}", {"query": f'{field}:"{query}"', "limit": 10})
        values = payload.get(f"{endpoint}s", []) or []
        return [{"provider": "musicbrainz", "providerId": str(value.get("id")), "title": value.get("name") or value.get("title"), "year": str(value.get("first-release-date") or value.get("date") or "")[:4] or None} for value in values if value.get("id")]

    def normalize(self, entity_type: str, provider_id: str, payload: dict) -> dict:
        images = []
        extra_images = []
        if entity_type in {"release", "release_group"}:
            archive_key = payload.get("cover-art-archive") or {}
            endpoint = "release-group" if entity_type == "release_group" else "release"
            cover_art = payload.get("_coverArt") or {}
            for artwork in cover_art.get("images", []) or []:
                image_type = PRIMARY if artwork.get("front") else None
                image_url = artwork.get("image") or artwork.get("thumbnails", {}).get("500")
                if not image_url:
                    continue
                value = _image(image_type, image_url, provider="musicbrainz", source_type=", ".join(artwork.get("types", []) or []) or "cover-art", width=artwork.get("width", 0), height=artwork.get("height", 0)) if image_type else {"sourceType": ", ".join(artwork.get("types", []) or []) or "cover-art", "url": image_url, "provider": "musicbrainz"}
                (images if image_type else extra_images).append(value)
            if not images and (archive_key.get("front") or entity_type == "release"):
                images.append(_image(PRIMARY, f"https://coverartarchive.org/{endpoint}/{quote(provider_id)}/front-500", provider="musicbrainz", source_type="front", width=500))
        external_ids = []
        for relationship in payload.get("relations", []) or payload.get("relationships", []) or []:
            resource = ((relationship.get("url") or {}).get("resource") or "") if isinstance(relationship, dict) else ""
            match = re.search(r"(?:themoviedb\.org/(?:movie|tv)|thetvdb\.com/(?:series|movies)|imdb\.com/title)/(?:.*?/)?(\d+|tt\d+)", resource, re.I)
            if not match:
                continue
            provider = "imdb" if "imdb.com" in resource.lower() else "tmdb" if "themoviedb.org" in resource.lower() else "tvdb"
            external_ids.append({"provider": provider, "identifierType": entity_type, "id": match.group(1)})
        date = payload.get("first-release-date") or payload.get("date")
        tags = _names(payload.get("tags"))
        credits = []
        for value in payload.get("artist-credit", []) or []:
            artist = value.get("artist") or {}
            if artist.get("id") or artist.get("name"):
                credits.append({"id": artist.get("id"), "name": artist.get("name"), "joinPhrase": value.get("joinphrase")})
        tracks = []
        for medium in payload.get("media", []) or []:
            for position, track in enumerate(medium.get("tracks", []) or [], start=1):
                recording = track.get("recording") or {}
                tracks.append({"id": recording.get("id") or track.get("id"), "title": track.get("title") or recording.get("title"), "position": track.get("position") or position, "disc": medium.get("position"), "length": track.get("length") or recording.get("length")})
        if entity_type == "track" and not tracks:
            tracks = [{"id": provider_id, "title": payload.get("title") or payload.get("name"), "position": payload.get("position")}]
        return {"title": payload.get("name") or payload.get("title"), "overview": None, "description": None, "date": date, "releaseDate": date, "year": str(date or "")[:4] or None, "tags": tags, "originalLanguage": None, "albumArtist": credits[0]["name"] if credits else None, "artists": credits, "tracks": tracks, "provider": "musicbrainz", "providerId": provider_id, "ids": external_ids, "images": images, "extraImages": extra_images}


def _tvdb_language(locale: str) -> str:
    return {"en": "eng", "en-us": "eng", "ja": "jpn", "ja-jp": "jpn", "zh": "zho", "ko": "kor"}.get(locale.lower(), locale.split("-")[0])


def _tvdb_images(entity_type: str, data: dict) -> tuple[list[dict], list[dict]]:
    values = []
    extras = []
    for artwork in data.get("artworks", []) or data.get("artwork", []) or []:
        url = artwork.get("image") or artwork.get("thumbnail") or artwork.get("imageUrl")
        if not url:
            continue
        raw_type = artwork.get("type", "")
        kind = str(raw_type).lower()
        if any(value in kind for value in ("background", "backdrop")):
            image_type = BACKDROP
        elif any(value in kind for value in ("clearlogo", "clear logo", "logo")):
            image_type = LOGO
        elif any(value in kind for value in ("banner", "thumbnail", "thumb")):
            image_type = BANNER
        elif entity_type == "episode" and any(value in kind for value in ("still", "episode", "screencap")):
            image_type = PRIMARY
        elif any(value in kind for value in ("poster", "series", "movie", "season", "box", "cover")):
            image_type = PRIMARY
        else:
            width = artwork.get("width") or 0
            height = artwork.get("height") or 0
            image_type = BANNER if width and height and float(width) / float(height) > 1.45 else None
        value = _image(image_type, url, language=artwork.get("language") or artwork.get("lang"), provider="tvdb", source_type=str(raw_type), score=artwork.get("score", 0), width=artwork.get("width", 0), height=artwork.get("height", 0)) if image_type else {"sourceType": str(raw_type), "url": url, "provider": "tvdb"}
        (values if image_type else extras).append(value)
    if data.get("image") and entity_type in {"series", "season", "episode", "movie"}:
        values.append(_image(PRIMARY, data["image"], provider="tvdb", source_type="image"))
    return values, extras


def _tvdb_children(data: dict) -> list[dict]:
    values = []
    for season in data.get("seasons", []) or []:
        season_number = season.get("seasonNumber")
        if season_number is None:
            season_number = season.get("number")
        if season.get("id") is not None and season_number is not None:
            values.append({"type": "season", "season": season_number, "id": str(season["id"])})
    for episode in data.get("episodes", []) or []:
        season_number = episode.get("seasonNumber")
        if season_number is None:
            season_number = episode.get("season")
        episode_number = episode.get("number")
        if episode_number is None:
            episode_number = episode.get("episodeNumber")
        if episode.get("id") is not None and season_number is not None and episode_number is not None:
            values.append({"type": "episode", "season": season_number, "episode": episode_number, "id": str(episode["id"])})
    return values


class MetadataService:
    _fetch_locks_guard = threading.Lock()
    _fetch_locks: dict[tuple[str, str, str, str], threading.Lock] = {}

    def __init__(self):
        self.credentials = MetadataCredentials()
        self.cache = MetadataCache()

    @classmethod
    def _lock_for(cls, provider: str, entity_type: str, provider_id: str, locale: str) -> threading.Lock:
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
            return TMDBClient(credential, self.credentials.configured()[provider].get("credentialType", "api_key"))
        if provider == "tvdb":
            return TVDBClient(credential)
        raise ProviderError(f"Unsupported metadata provider '{provider}'")

    def test(self, provider: str, credential: dict | None = None, credential_type: str = "api_key") -> None:
        if provider == "tmdb":
            TMDBClient(credential or self.credentials.get(provider) or {}, credential_type).test()
        elif provider == "tvdb":
            TVDBClient(credential or self.credentials.get(provider) or {}).test()
        elif provider == "musicbrainz":
            MusicBrainzClient()._request("/artist/00000000-0000-0000-0000-000000000000")
        else:
            raise ProviderError("Unsupported provider")

    def fetch(self, provider: str, entity_type: str, provider_id: str, locale: str, force: bool = False) -> dict:
        lock = self._lock_for(provider, entity_type, provider_id, locale)
        with lock:
            # Another request may have populated the cache while this request
            # was waiting. Re-check before making a provider call.
            cached = self.cache.get(provider, entity_type, provider_id, locale)
            if cached and not force and not cached.pop("_stale", False):
                return cached
            logger.debug("metadata fetch provider=%s entity_type=%s provider_id=%s locale=%s force=%s", provider, entity_type, provider_id, locale, force)
            client = self.client(provider)
            try:
                payload = client.details(entity_type, provider_id, locale)
                normalized = client.normalize(entity_type, provider_id, payload)
            except Exception as error:
                logger.exception("metadata fetch failed provider=%s entity_type=%s provider_id=%s locale=%s", provider, entity_type, provider_id, locale)
                if isinstance(error, ProviderError):
                    raise
                raise ProviderError(f"{provider} {entity_type} {provider_id} details normalization failed: {type(error).__name__}: {error}") from error
            self.cache.put(provider, entity_type, provider_id, locale, normalized)
            logger.info("metadata cached provider=%s entity_type=%s provider_id=%s locale=%s images=%d", provider, entity_type, provider_id, locale, len(normalized.get("images", [])))
            return normalized

    def _cache_normalized(self, provider: str, entity_type: str, provider_id: str, locale: str, client, payload: dict) -> dict:
        normalized = client.normalize(entity_type, provider_id, payload)
        self.cache.put(provider, entity_type, provider_id, locale, normalized)
        logger.info("metadata cached provider=%s entity_type=%s provider_id=%s locale=%s images=%d", provider, entity_type, provider_id, locale, len(normalized.get("images", [])))
        return normalized

    def aggregate_series(self, provider: str, provider_id: str, locale: str = "en", force: bool = False) -> dict:
        """Cache a resolved series and its season/episode hierarchy in batches."""
        client = self.client(provider)
        records = {"series": None, "seasons": [], "episodes": []}
        if provider == "tvdb":
            hierarchy = client.series_hierarchy(provider_id)
            records["series"] = self._cache_normalized(provider, "series", provider_id, locale, client, hierarchy["extended"])
            seasons = (hierarchy["extended"].get("data") or {}).get("seasons") or []
            for season in seasons:
                season_type = season.get("type")
                if isinstance(season_type, dict):
                    season_type = season_type.get("type")
                if season_type and str(season_type).lower() not in {"official", "aired", "default"}:
                    continue
                number = season.get("seasonNumber")
                if number is None:
                    number = season.get("number")
                if season.get("id") is None or number is None:
                    continue
                season_id = str(season["id"])
                normalized = self._cache_normalized(provider, "season", season_id, locale, client, {"data": season})
                if not any(image.get("type") == PRIMARY for image in normalized.get("images", [])):
                    try:
                        normalized = self.fetch(provider, "season", season_id, locale, force=True)
                    except ProviderError:
                        if not normalized.get("title"):
                            raise
                records["seasons"].append(normalized)
            for episode in hierarchy["episodes"]:
                episode_id = episode.get("id")
                season_number = episode.get("seasonNumber")
                if season_number is None:
                    season_number = episode.get("season")
                episode_number = episode.get("number")
                if episode_number is None:
                    episode_number = episode.get("episodeNumber")
                if episode_id is None or season_number is None or episode_number is None:
                    continue
                normalized = self._cache_normalized(provider, "episode", str(episode_id), locale, client, {"data": episode})
                if not normalized.get("title") or not any(image.get("type") == PRIMARY for image in normalized.get("images", [])):
                    try:
                        normalized = self.fetch(provider, "episode", str(episode_id), locale, force=True)
                    except ProviderError:
                        if not normalized.get("title"):
                            raise
                records["episodes"].append(normalized)
            return records
        if provider == "tmdb":
            hierarchy = client.series_hierarchy(provider_id, locale)
            records["series"] = self._cache_normalized(provider, "series", provider_id, locale, client, hierarchy["series"])
            for value in hierarchy["seasons"]:
                details = value["details"]
                season_number = details.get("season_number")
                if season_number is None:
                    season_number = value["summary"].get("season_number")
                if season_number is None:
                    continue
                season_id = f"{provider_id}:{season_number}"
                records["seasons"].append(self._cache_normalized(provider, "season", season_id, locale, client, details))
                for episode in details.get("episodes", []) or []:
                    episode_number = episode.get("episode_number")
                    if episode_number is None:
                        continue
                    episode_id = f"{provider_id}:{season_number}:{episode_number}"
                    normalized = self._cache_normalized(provider, "episode", episode_id, locale, client, episode)
                    if not normalized.get("title") or not any(image.get("type") == PRIMARY for image in normalized.get("images", [])):
                        try:
                            normalized = self.fetch(provider, "episode", episode_id, locale, force=True)
                        except ProviderError:
                            if not normalized.get("title"):
                                raise
                    records["episodes"].append(normalized)
            return records
        raise ProviderError(f"Series aggregation is unsupported for {provider}")

    def fetch_fallback(self, provider: str, entity_type: str, provider_id: str, locale: str, force: bool = False) -> dict | None:
        """Resolve requested locale, then English, then any cached translation."""
        wanted = (locale or "en").lower()
        candidates = [wanted] if wanted == "en" else [wanted, "en"]
        values: list[dict] = []
        for candidate in candidates:
            cached = self.cache.get(provider, entity_type, provider_id, candidate)
            if cached and not force and not cached.pop("_stale", False):
                values.append(cached)
                continue
            try:
                values.append(self.fetch(provider, entity_type, provider_id, candidate, force=force))
            except ProviderError:
                continue
        any_cached = self.cache.any(provider, entity_type, provider_id)
        if any_cached:
            any_cached.pop("_stale", None)
            values.append(any_cached)
        if not values:
            return None
        merged: dict = {}
        for value in values:
            for key, field in value.items():
                if key in {"images", "extraImages"}:
                    merged.setdefault(key, [])
                    merged[key].extend(field or [])
                elif not merged.get(key) and field:
                    merged[key] = field
        return merged

    def resolve_inventory_entity(self, entity_type: str, query: str, year: str | None = None, explicit_ids: list[dict] | None = None) -> dict:
        """Resolve an inventory entity from its authoritative provider.

        Series and movies must be matched by TVDB and TMDB respectively. The
        selected primary record is then the only source of secondary IDs via
        its external/remote database links; searching secondary providers by
        title can match a different entity with the same name.
        """
        priorities = {
            "series": ["tvdb", "tmdb"], "movie": ["tmdb", "tvdb"],
            "artist": ["musicbrainz"], "release": ["musicbrainz"],
            "track": ["musicbrainz"], "recording": ["musicbrainz"],
        }.get(entity_type, [])
        if not priorities:
            raise ProviderError(f"No provider resolution strategy exists for {entity_type}")
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
                candidates = client.search(entity_type, query, year) if primary_provider == "tmdb" else client.search(entity_type, query)
                provider_id = _select_match(candidates, query, year)
            normalized = self.fetch(primary_provider, entity_type, str(provider_id), "en", force=True)
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
        return {"metadata": normalized, "providerIds": [{"provider": provider, "id": provider_id} for provider, provider_id in explicit_by_provider.items()]}


def _normalized_match_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _select_match(candidates: list[dict], query: str, year: str | None = None) -> str:
    wanted = _normalized_match_text(query)
    scored = []
    for candidate in candidates:
        title = _normalized_match_text(str(candidate.get("title") or ""))
        if not title:
            continue
        score = 100 if title == wanted else 75 if wanted in title or title in wanted else 0
        if year and candidate.get("year") and str(candidate["year"])[:4] == str(year)[:4]:
            score += 20
        if score:
            scored.append((score, str(candidate["providerId"])))
    scored.sort(reverse=True)
    if not scored or scored[0][0] < 95 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        raise ProviderError(f"No unique high-confidence match for '{query}'")
    return scored[0][1]


def choose_image(images: list[dict], requested: str, image_type: str) -> dict | None:
    if image_type not in IMAGE_TYPES:
        raise ValueError(f"Unsupported image type '{image_type}'. Expected one of: {', '.join(sorted(IMAGE_TYPES))}")
    values = [image for image in images if image.get("type") == image_type]
    if not values:
        return None
    requested = (requested or "en").lower()
    requested_language = _language_family(requested)

    def bucket(image: dict) -> int:
        lang = (image.get("language") or "").lower()
        image_language = _language_family(lang)
        if image_language == requested_language:
            return 0
        if not lang:
            return 1
        if _language_family(lang) == "en":
            return 2
        return 3
    return sorted(values, key=lambda image: (bucket(image), -(image.get("score") or 0), -(image.get("width") or 0), image.get("url", "")))[0]


def _language_family(value: str) -> str:
    """Normalize locale and provider ISO-639-2 language codes for artwork selection."""
    code = (value or "").lower().split("-", 1)[0].split("_", 1)[0]
    return {
        "eng": "en",
        "jpn": "ja",
        "zho": "zh",
        "kor": "ko",
    }.get(code, code)
