from __future__ import annotations

import asyncio
import json
import os
import unicodedata
from typing import Literal

import httpx
from app.lyrics import parse_lyrics_text

LyricsFallback = Literal["netease"]
_NETEASE_URL = "https://music.163.com"
_TIMEOUT_SECONDS = 5
_MAX_RESPONSE_BYTES = 256 * 1024


class NetEaseUnavailable(Exception):
    """NetEase could not complete the lookup."""


def netease_enabled() -> bool:
    return os.getenv("ZENSTREAM_NETEASE_LYRICS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _name(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", _name(value)).casefold().split())


def _artist_names(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [
        name
        for value in values
        if (
            name := _name(
                (value.get("name") or value.get("Name"))
                if isinstance(value, dict)
                else value
            )
        )
    ]


def _duration(value: object) -> float | None:
    if isinstance(value, (int, float)) and 0 < value < 86400:
        return float(value)
    return None


def _select_song(
    songs: list, title: str, artists: list[str], album: str, duration: float | None
) -> int | None:
    title_key = _normalized(title)
    artist_keys = {_normalized(artist) for artist in artists}
    album_key = _normalized(album)
    candidates: dict[int, tuple[bool, bool, float]] = {}
    for song in songs:
        if not isinstance(song, dict) or _normalized(song.get("name")) != title_key:
            continue
        song_id = song.get("id")
        if type(song_id) is not int or song_id <= 0:
            continue
        credits = {_normalized(name) for name in _artist_names(song.get("artists"))}
        if not artist_keys.intersection(credits):
            continue
        # Search durations are in milliseconds.
        milliseconds = song.get("duration")
        length = (
            _duration(milliseconds / 1000)
            if isinstance(milliseconds, (int, float))
            else None
        )
        if duration is not None and (length is None or abs(duration - length) > 8):
            continue
        release = song.get("album")
        same_album = bool(
            album_key
            and isinstance(release, dict)
            and _normalized(release.get("name")) == album_key
        )
        difference = abs(duration - length) if duration and length else 0.0
        candidates[song_id] = (artist_keys != credits, not same_album, difference)
    if not candidates:
        return None
    ranked = sorted(candidates, key=candidates.__getitem__)
    if len(ranked) > 1:
        best, second = (candidates[song_id] for song_id in ranked[:2])
        if best[:2] == second[:2] and second[2] - best[2] < 2:
            return None
    return ranked[0]


async def _get_json(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    async with client.stream("GET", path, params=params) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=8192):
            if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise NetEaseUnavailable
            body.extend(chunk)
    value = json.loads(body)
    if not isinstance(value, dict) or value.get("code") != 200:
        raise NetEaseUnavailable
    return value


async def fetch_netease_lyrics(track: dict) -> dict | None:
    if track.get("type") != "track":
        return None
    metadata = track.get("metadata") or {}
    title = _name(track.get("name"))
    artists = _artist_names(metadata.get("contributingArtists")) or _artist_names(
        metadata.get("artists")
    )
    if not artists and (album_artist := _name(metadata.get("albumArtist"))):
        artists = [album_artist]
    if not title or not artists:
        return None
    duration = _duration(track.get("durationSeconds"))
    try:
        async with (
            asyncio.timeout(_TIMEOUT_SECONDS),
            httpx.AsyncClient(
                base_url=_NETEASE_URL, timeout=_TIMEOUT_SECONDS
            ) as client,
        ):
            search = await _get_json(
                client,
                "/api/search/get",
                {"s": f"{artists[0]} {title}", "type": 1, "limit": 5, "offset": 0},
            )
            result = search.get("result")
            if result is not None and not isinstance(result, dict):
                raise NetEaseUnavailable
            songs = result.get("songs") if result else None
            if songs is not None and not isinstance(songs, list):
                raise NetEaseUnavailable
            song_id = _select_song(
                songs or [], title, artists, _name(metadata.get("album")), duration
            )
            if song_id is None:
                return None
            response = await _get_json(
                client, "/api/song/lyric", {"id": song_id, "kv": -1, "lv": -1}
            )
    except (TimeoutError, httpx.HTTPError, ValueError) as exc:
        raise NetEaseUnavailable from exc
    if response.get("nolyric") or response.get("uncollected"):
        return None
    original = response.get("lrc")
    text = _name(original.get("lyric")) if isinstance(original, dict) else ""
    parsed = parse_lyrics_text(text, duration)
    return {"source": "netease", "language": None, **parsed} if parsed else None
