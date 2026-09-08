"""Helpers for filesystem-provided metadata and artwork."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

NFO_MAX_BYTES = 4 * 1024 * 1024
NFO_EXTENSIONS = frozenset({".nfo"})

# These names are intentionally shared by the scanner, catalog projection,
# and image routes.  Numeric suffixes and separators are accepted by
# ``local_artwork_type`` so multi-artwork folders remain deterministic.
LOCAL_ARTWORK_NAMES = {
    "Primary": {
        "poster",
        "folder",
        "cover",
        "primary",
        "tvshow",
        "movie",
        "season",
        "album",
        "front",
        "frontcover",
        "thumb",
        "thumbnail",
    },
    "Backdrop": {"backdrop", "fanart", "background", "landscape"},
    "Logo": {"logo", "clearlogo", "clear-logo"},
    "Banner": {"banner"},
}

_LOCAL_ARTWORK_COMPACT = {
    image_type: {re.sub(r"[^a-z0-9]", "", name.casefold()) for name in names}
    for image_type, names in LOCAL_ARTWORK_NAMES.items()
}


def local_artwork_type(value: str | Path) -> str | None:
    """Return the canonical artwork category for a conventional file name."""
    stem = Path(str(value)).stem.casefold()
    compact = re.sub(r"[^a-z0-9]", "", stem)
    if not compact:
        return None
    # Kodi/Jellyfin folders commonly number repeated fanart/poster files.
    base = re.sub(r"\d+$", "", compact) or compact
    for image_type, names in _LOCAL_ARTWORK_COMPACT.items():
        if compact in names or base in names:
            return image_type
    return None


def _tag(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _text(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    value = " ".join(part.strip() for part in node.itertext() if part.strip())
    return re.sub(r"\s+", " ", value).strip() or None


def _nodes(root: ET.Element, *names: str) -> list[ET.Element]:
    wanted = {_tag(name) for name in names}
    return [
        node for node in root.iter() if node is not root and _tag(node.tag) in wanted
    ]


def _first(root: ET.Element, *names: str) -> str | None:
    for node in _nodes(root, *names):
        value = _text(node)
        if value:
            return value
    return None


def _values(root: ET.Element, *names: str) -> list[str]:
    result: list[str] = []
    for node in _nodes(root, *names):
        value = _text(node)
        if value and value.casefold() not in {item.casefold() for item in result}:
            result.append(value)
    return result


def _number(value: str | None, *, integer: bool = False) -> int | float | None:
    if not value:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value.replace(",", "."))
    if not match:
        return None
    try:
        parsed = float(match.group(0))
    except ValueError:
        return None
    if parsed < 0:
        return None
    return int(parsed) if integer else parsed


def _runtime_minutes(value: str | None) -> int | float | None:
    if not value:
        return None
    hours = re.search(r"(\d+(?:\.\d+)?)\s*h", value, re.IGNORECASE)
    minutes = re.search(r"(\d+(?:\.\d+)?)\s*m", value, re.IGNORECASE)
    seconds = re.search(r"(\d+(?:\.\d+)?)\s*s", value, re.IGNORECASE)
    if hours or minutes or seconds:
        total = float(hours.group(1)) * 60 if hours else 0
        total += float(minutes.group(1)) if minutes else 0
        total += float(seconds.group(1)) / 60 if seconds else 0
        return int(total) if total.is_integer() else total
    return _number(value, integer=True)


def _date(value: str | None) -> str | None:
    if not value:
        return None
    match = re.match(r"^(\d{4})(?:[-/]?(\d{2})(?:[-/]?(\d{2}))?)?", value)
    if match:
        year, month, day = match.groups()
        if month and day:
            return f"{year}-{month}-{day}"
        if month:
            return f"{year}-{month}"
        return year
    return value


def _entity_identifier_type(entity_type: str | None, provider: str) -> str:
    if provider == "tmdb":
        return (
            entity_type
            if entity_type in {"movie", "series", "season", "episode"}
            else "movie"
        )
    if provider == "tvdb":
        return (
            entity_type if entity_type in {"series", "season", "episode"} else "series"
        )
    if provider == "imdb":
        return "imdb"
    if entity_type == "artist":
        return "artist"
    if entity_type == "release":
        return "release"
    if entity_type == "track":
        return "recording"
    return "work"


def _provider_from_identifier(value: str) -> tuple[str, str] | None:
    key = _tag(value)
    aliases = {
        "tmdb": "tmdb",
        "themoviedb": "tmdb",
        "tmdbid": "tmdb",
        "tvdb": "tvdb",
        "thetvdb": "tvdb",
        "tvdbid": "tvdb",
        "imdb": "imdb",
        "imdbid": "imdb",
        "musicbrainz": "musicbrainz",
        "musicbrainzartist": "musicbrainz",
        "musicbrainzartistid": "musicbrainz",
        "musicbrainzalbum": "musicbrainz",
        "musicbrainzalbumid": "musicbrainz",
        "musicbrainzrelease": "musicbrainz",
        "musicbrainzreleaseid": "musicbrainz",
        "musicbrainzreleasegroup": "musicbrainz",
        "musicbrainzreleasegroupid": "musicbrainz",
        "musicbrainzrecording": "musicbrainz",
        "musicbrainzrecordingid": "musicbrainz",
        "musicbrainztrack": "musicbrainz",
        "musicbrainztrackid": "musicbrainz",
        "musicbrainzwork": "musicbrainz",
        "musicbrainzworkid": "musicbrainz",
    }
    provider = aliases.get(key)
    if not provider:
        return None
    if provider != "musicbrainz":
        return provider, ""
    identifier = {
        "musicbrainzartist": "artist",
        "musicbrainzartistid": "artist",
        "musicbrainzalbum": "release",
        "musicbrainzalbumid": "release",
        "musicbrainzrelease": "release",
        "musicbrainzreleaseid": "release",
        "musicbrainzreleasegroup": "release_group",
        "musicbrainzreleasegroupid": "release_group",
        "musicbrainzrecording": "recording",
        "musicbrainzrecordingid": "recording",
        "musicbrainztrack": "recording",
        "musicbrainztrackid": "recording",
        "musicbrainzwork": "work",
        "musicbrainzworkid": "work",
    }.get(key, "work")
    return provider, identifier


def _nfo_root(path: Path) -> ET.Element | None:
    if path.suffix.casefold() not in NFO_EXTENSIONS:
        return None
    try:
        if not path.is_file() or path.stat().st_size > NFO_MAX_BYTES:
            return None
        return ET.parse(path).getroot()
    except (OSError, ET.ParseError, ValueError):
        return None


def _nfo_ids_from_root(
    root: ET.Element, entity_type: str | None = None
) -> list[tuple[str, str, str]]:
    values: list[tuple[str, str, str]] = []

    def accepted(provider: str, identifier_type: str) -> bool:
        if provider != "musicbrainz" or entity_type is None:
            return True
        allowed = {
            "artist": {"artist"},
            "release": {"release", "release_group"},
            "track": {"recording", "release_track", "work"},
        }.get(entity_type)
        return allowed is None or identifier_type in allowed

    def add(provider: str, identifier_type: str, value: str | None) -> None:
        value = str(value or "").strip()
        if not value or not accepted(provider, identifier_type):
            return
        item = (provider, identifier_type, value)
        if item not in values:
            values.append(item)

    for node in _nodes(root, "uniqueid"):
        provider_info = _provider_from_identifier(node.attrib.get("type"))
        if not provider_info:
            continue
        provider, identifier_type = provider_info
        if not identifier_type:
            identifier_type = _entity_identifier_type(entity_type, provider)
        add(provider, identifier_type, _text(node))

    root_fields = {
        "tmdbid": ("tmdb", None),
        "tvdbid": ("tvdb", None),
        "imdbid": ("imdb", "imdb"),
        "musicbrainzartistid": ("musicbrainz", "artist"),
        "musicbrainzalbumid": ("musicbrainz", "release"),
        "musicbrainzreleaseid": ("musicbrainz", "release"),
        "musicbrainzreleasegroupid": ("musicbrainz", "release_group"),
        "musicbrainzrecordingid": ("musicbrainz", "recording"),
        "musicbrainztrackid": ("musicbrainz", "recording"),
        "musicbrainzworkid": ("musicbrainz", "work"),
    }
    for node in root.iter():
        field = _tag(node.tag)
        mapping = root_fields.get(field)
        if not mapping:
            continue
        provider, identifier_type = mapping
        add(
            provider,
            identifier_type or _entity_identifier_type(entity_type, provider),
            _text(node),
        )
    return values


def parse_nfo_ids(
    path: Path, entity_type: str | None = None
) -> list[tuple[str, str, str]]:
    """Read supported provider identities from a Kodi/Jellyfin NFO file."""
    root = _nfo_root(Path(path))
    return _nfo_ids_from_root(root, entity_type) if root is not None else []


def _music_people(root: ET.Element) -> list[dict]:
    result: list[dict] = []
    for node in _nodes(root, "artist"):
        name = _text(node)
        if list(node):
            name = _first(node, "name") or name
        if name:
            result.append({"name": name})
    return result


def _video_people(root: ET.Element) -> list[dict]:
    result: list[dict] = []
    for node in root.iter():
        kind = _tag(node.tag)
        if kind not in {"actor", "director", "writer", "producer", "creator"}:
            continue
        name = _first(node, "name") or (_text(node) if not list(node) else None)
        if not name:
            continue
        person = {"name": name}
        role = _first(node, "role", "character")
        if role:
            person["role"] = role
        if kind != "actor":
            person["role"] = person.get("role") or kind.title()
            person["department"] = (
                "Directing"
                if kind == "director"
                else "Writing"
                if kind == "writer"
                else "Production"
            )
        order = _number(_first(node, "order"), integer=True)
        if order is not None:
            person["order"] = order
        if not any(
            existing.get("name", "").casefold() == name.casefold()
            and existing.get("role") == person.get("role")
            for existing in result
        ):
            result.append(person)
    return result


def _nfo_images(root: ET.Element) -> list[dict]:
    result: list[dict] = []
    for node in root.iter():
        kind = _tag(node.tag)
        if kind not in {
            "thumb",
            "thumbnail",
            "poster",
            "fanart",
            "backdrop",
            "logo",
            "clearlogo",
            "banner",
        }:
            continue
        if kind == "fanart" and list(node):
            continue
        value = _text(node)
        parsed = urlparse(value or "")
        if not value or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            # Relative NFO artwork is resolved from admitted image files by
            # the scanner; remote refs are safe to expose as metadata only.
            continue
        aspect = _tag(node.attrib.get("aspect"))
        image_type = {
            "fanart": "Backdrop",
            "backdrop": "Backdrop",
            "logo": "Logo",
            "clearlogo": "Logo",
            "banner": "Banner",
        }.get(aspect or kind, "Primary")
        candidate = {
            "type": image_type,
            "url": value,
            "language": node.attrib.get("language") or None,
            "provider": "local",
        }
        if candidate not in result:
            result.append(candidate)
    return result


def parse_nfo_metadata(path: Path, entity_type: str | None = None) -> dict:
    """Return a normalized, locale-neutral metadata document from an NFO."""
    root = _nfo_root(Path(path))
    if root is None:
        return {}
    entity_type = str(entity_type or "").strip().casefold() or None
    values: dict = {}

    title = _first(root, "title", "name", "album")
    if title:
        values["title"] = title
    original_title = _first(root, "originaltitle", "originalname")
    if original_title:
        values["originalTitle"] = original_title
    overview = _first(root, "plot", "description", "summary", "outline")
    if overview:
        values["overview"] = overview
        values["description"] = overview

    genres = _values(root, "genre", "tag", "style", "mood")
    if genres:
        values["tags"] = genres
        values["genres"] = genres
    studios = _values(root, "studio", "productioncompany", "company")
    networks = _values(root, "network")
    if studios:
        values["studios"] = studios
        values["productionCompanies"] = studios
    if networks:
        values["networks"] = networks

    release_date = _date(
        _first(root, "releasedate", "released", "premiered", "airdate", "date")
    )
    first_aired = _date(_first(root, "firstaired", "premiered", "airdate"))
    last_aired = _date(_first(root, "lastaired"))
    if release_date:
        values["date"] = release_date
        values["releaseDate"] = release_date
    if first_aired:
        values["firstAired"] = first_aired
    if last_aired:
        values["lastAired"] = last_aired
    year = _first(root, "year") or (release_date[:4] if release_date else None)
    if year:
        values["year"] = str(year)[:4]

    runtime = _runtime_minutes(_first(root, "runtime", "runtimeminutes"))
    if runtime is not None:
        values["runtimeMinutes"] = runtime
    duration = _number(_first(root, "durationseconds", "duration"))
    if duration is not None:
        values["durationSeconds"] = duration

    for field, names in {
        "seasonNumber": ("seasonnumber", "season"),
        "episodeNumber": ("episodenumber", "episode"),
        "discNumber": ("discnumber", "disc"),
        "trackNumber": ("tracknumber", "position"),
    }.items():
        number = _number(_first(root, *names), integer=True)
        if number is not None:
            values[field] = number

    for field, names in {
        "status": ("status",),
        "airTime": ("airtime",),
        "originalCountry": ("originalcountry", "country"),
        "originalLanguage": ("originallanguage", "language"),
    }.items():
        value = _first(root, *names)
        if value:
            values[field] = value

    community_rating = _first(root, "communityrating", "rating", "userrating")
    if community_rating is None:
        for node in _nodes(root, "rating"):
            community_rating = _first(node, "value", "rating")
            if community_rating:
                break
    parsed_rating = _number(community_rating)
    if parsed_rating is not None:
        values["communityRating"] = parsed_rating
    critic_rating = _number(
        _first(root, "criticrating", "criticscore", "metascore", "tomatometer")
    )
    if critic_rating is not None:
        values["criticRating"] = critic_rating

    if entity_type in {"artist", "release", "track"}:
        artists = _music_people(root)
        album_artist = _first(root, "albumartist", "album artist", "releaseartist")
        if entity_type == "artist" and not title:
            title = _first(root, "name", "artist")
            if title:
                values["title"] = title
        if album_artist:
            values["albumArtist"] = album_artist
        elif artists:
            values["albumArtist"] = artists[0]["name"]
        if artists:
            values["artists"] = artists
            values["contributingArtists"] = [dict(value) for value in artists]
        album = _first(root, "album", "albumtitle", "releasetitle")
        if album:
            values["album"] = album
            if entity_type == "release":
                values["title"] = album
        label = _first(root, "label", "publisher", "organization")
        if label:
            values["label"] = label
        album_type = _first(root, "albumtype", "releasetype", "type")
        if album_type and _tag(album_type) not in {"album", "artist", "release"}:
            values["albumType"] = album_type
        secondary_types = _values(root, "albumsecondarytype", "secondarytype")
        if secondary_types:
            values["albumSecondaryTypes"] = secondary_types
        if entity_type == "artist" and values.get("albumArtist"):
            values["title"] = values["albumArtist"]
    else:
        people = _video_people(root)
        if people:
            values["people"] = people

    identities = _nfo_ids_from_root(root, entity_type)
    if identities:
        values["ids"] = [
            {"provider": provider, "identifierType": identifier_type, "id": value}
            for provider, identifier_type, value in identities
        ]
    images = _nfo_images(root)
    values["images"] = images
    values["extraImages"] = []
    values["provider"] = "local"
    values["providerId"] = None
    return values


def parse_nfo(path: Path, entity_type: str | None = None) -> dict:
    """Compatibility alias for callers that treat NFO as a parser."""
    return parse_nfo_metadata(path, entity_type)
