"""Small provider clients used by the administrator library preview."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from app.models.metadata import MetadataCache, MetadataCredentials
from version import __version__


class ProviderError(RuntimeError):
    pass


class ProviderClient:
    def __init__(self, timeout: float = 20):
        self.timeout = timeout

    def _get(self, url: str, **kwargs) -> dict:
        try:
            response = httpx.get(url, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderError(str(error)) from error


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
        kind = "tv" if entity_type in {"series", "season", "episode"} else "movie"
        return self._request(f"/{kind}/{quote(provider_id)}", params={"language": locale, "append_to_response": "images,external_ids"})

    def normalize(self, entity_type: str, provider_id: str, payload: dict) -> dict:
        title = payload.get("title") or payload.get("name") or payload.get("original_title") or payload.get("original_name")
        return {
            "title": title,
            "overview": payload.get("overview"),
            "year": (payload.get("release_date") or payload.get("first_air_date") or "")[:4] or None,
            "originalLanguage": payload.get("original_language"),
            "provider": "tmdb",
            "providerId": provider_id,
            "images": self._images(payload),
        }

    @staticmethod
    def _images(payload: dict) -> list[dict]:
        images = payload.get("images") or {}
        values = []
        for image_type, key in (("poster", "posters"), ("backdrop", "backdrops"), ("logo", "logos")):
            for image in images.get(key, []) or []:
                path = image.get("file_path")
                if path:
                    values.append({"type": image_type, "language": image.get("iso_639_1"), "url": f"https://image.tmdb.org/t/p/w1280{path}", "score": image.get("vote_average", 0), "width": image.get("width", 0), "provider": "tmdb"})
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

    def normalize(self, entity_type: str, provider_id: str, payload: dict) -> dict:
        data = payload.get("data", payload)
        translation = payload.get("translation") or {}
        return {
            "title": translation.get("name") or data.get("name") or data.get("title"),
            "overview": translation.get("overview") or data.get("overview") or data.get("overviewTranslations", [None])[0],
            "year": (data.get("year") or data.get("firstAired") or "")[:4] or None,
            "originalLanguage": data.get("originalLanguage"),
            "provider": "tvdb",
            "providerId": provider_id,
            "images": _tvdb_images(data),
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
        endpoint = {"artist": "artist", "release": "release", "release_group": "release-group", "recording": "recording", "work": "work"}.get(entity_type, "release")
        return self._request(f"/{endpoint}/{quote(provider_id)}", {"inc": "artist-credits+aliases+releases+release-groups+recordings"})

    def normalize(self, entity_type: str, provider_id: str, payload: dict) -> dict:
        images = []
        if entity_type in {"release", "release_group"}:
            archive_key = payload.get("cover-art-archive") or {}
            if archive_key.get("front") or entity_type == "release":
                endpoint = "release-group" if entity_type == "release_group" else "release"
                images.append({"type": "poster", "language": None, "url": f"https://coverartarchive.org/{endpoint}/{quote(provider_id)}/front-500", "score": 0, "width": 500, "provider": "musicbrainz"})
        return {"title": payload.get("name") or payload.get("title"), "overview": None, "year": None, "originalLanguage": None, "provider": "musicbrainz", "providerId": provider_id, "images": images}


def _tvdb_language(locale: str) -> str:
    return {"en": "eng", "en-us": "eng", "ja": "jpn", "ja-jp": "jpn", "zh": "zho", "ko": "kor"}.get(locale.lower(), locale.split("-")[0])


def _tvdb_images(data: dict) -> list[dict]:
    values = []
    for artwork in data.get("artworks", []) or data.get("artwork", []) or []:
        url = artwork.get("image") or artwork.get("thumbnail") or artwork.get("imageUrl")
        if not url:
            continue
        kind = str(artwork.get("type", "")).lower()
        image_type = "backdrop" if "background" in kind or "backdrop" in kind else "poster"
        values.append({"type": image_type, "language": artwork.get("language") or artwork.get("lang"), "url": url, "score": artwork.get("score", 0), "width": artwork.get("width", 0), "provider": "tvdb"})
    if data.get("image"):
        values.append({"type": "poster", "language": None, "url": data["image"], "score": 0, "width": 0, "provider": "tvdb"})
    return values


class MetadataService:
    def __init__(self):
        self.credentials = MetadataCredentials()
        self.cache = MetadataCache()

    def client(self, provider: str):
        credential = self.credentials.get(provider)
        if not credential:
            raise ProviderError(f"{provider.upper()} is not configured")
        if provider == "tmdb":
            return TMDBClient(credential, self.credentials.configured()[provider].get("credentialType", "api_key"))
        if provider == "tvdb":
            return TVDBClient(credential)
        return MusicBrainzClient()

    def test(self, provider: str, credential: dict | None = None, credential_type: str = "api_key") -> None:
        if provider == "tmdb":
            TMDBClient(credential or self.credentials.get(provider) or {}, credential_type).test()
        elif provider == "tvdb":
            TVDBClient(credential or self.credentials.get(provider) or {}).test()
        elif provider == "musicbrainz":
            MusicBrainzClient()._request("/artist/00000000-0000-0000-0000-000000000000")
        else:
            raise ProviderError("Unsupported provider")

    def fetch(self, provider: str, entity_type: str, provider_id: str, locale: str) -> dict:
        cached = self.cache.get(provider, entity_type, provider_id, locale)
        if cached and not cached.pop("_stale", False):
            return cached
        client = self.client(provider)
        payload = client.details(entity_type, provider_id, locale)
        normalized = client.normalize(entity_type, provider_id, payload)
        self.cache.put(provider, entity_type, provider_id, locale, normalized)
        return normalized

    def fetch_fallback(self, provider: str, entity_type: str, provider_id: str, locale: str) -> dict | None:
        """Resolve requested locale, then English, then any cached translation."""
        wanted = (locale or "en").lower()
        candidates = [wanted] if wanted == "en" else [wanted, "en"]
        values: list[dict] = []
        for candidate in candidates:
            cached = self.cache.get(provider, entity_type, provider_id, candidate)
            if cached and not cached.pop("_stale", False):
                values.append(cached)
                continue
            try:
                values.append(self.fetch(provider, entity_type, provider_id, candidate))
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
                if key == "images":
                    merged.setdefault("images", [])
                    merged["images"].extend(field or [])
                elif not merged.get(key) and field:
                    merged[key] = field
        return merged


def choose_image(images: list[dict], requested: str, image_type: str) -> dict | None:
    values = [image for image in images if image.get("type") == image_type]
    if not values:
        return None
    requested = requested.lower()
    def bucket(image: dict) -> int:
        lang = (image.get("language") or "").lower()
        if lang == requested or lang.startswith(requested.split("-")[0]):
            return 0
        if not lang:
            return 1
        if lang in {"en", "eng", "en-us"}:
            return 2
        return 3
    return sorted(values, key=lambda image: (bucket(image), -(image.get("score") or 0), -(image.get("width") or 0), image.get("url", "")))[0]
