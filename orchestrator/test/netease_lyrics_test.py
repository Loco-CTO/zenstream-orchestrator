import asyncio
import unittest
from unittest.mock import patch

import httpx
from app.netease_lyrics import NetEaseUnavailable, fetch_netease_lyrics

TRACK = {
    "type": "track",
    "name": "Ａ Song",
    "metadata": {"artists": [{"name": "Artist"}], "album": "Record"},
    "durationSeconds": 200,
}


def song(song_id, *, title="A Song", artist="Artist", album="Record", duration=200000):
    return {
        "id": song_id,
        "name": title,
        "artists": [{"name": artist}],
        "album": {"name": album},
        "duration": duration,
    }


class NetEaseLyricsTest(unittest.IsolatedAsyncioTestCase):
    async def lookup(self, handler):
        client = httpx.AsyncClient
        with patch(
            "app.netease_lyrics.httpx.AsyncClient",
            side_effect=lambda **kwargs: client(
                **kwargs, transport=httpx.MockTransport(handler)
            ),
        ):
            return await fetch_netease_lyrics(TRACK)

    async def test_selects_the_recording_and_parses_original_lrc(self):
        requests = []

        def handler(request):
            requests.append(request.url)
            if request.url.path.endswith("/get"):
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "result": {
                            "songs": [
                                song(1, artist="Cover artist"),
                                song(2, duration=180000),
                                song(3, album="Compilation"),
                                song(4),
                            ]
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "lrc": {"lyric": "[00:02.50]Original"},
                    "tlyric": {"lyric": "[00:02.50]Translation"},
                },
            )

        self.assertEqual(
            await self.lookup(handler),
            {
                "source": "netease",
                "language": None,
                "timed": True,
                "lines": [{"text": "Original", "startSeconds": 2.5, "endSeconds": 200}],
            },
        )
        self.assertEqual(requests[0].params["s"], "Artist Ａ Song")
        self.assertEqual(requests[1].params["id"], "4")

    async def test_ambiguous_recordings_do_not_trigger_a_lyric_request(self):
        def handler(request):
            self.assertEqual(request.url.path, "/api/search/get")
            return httpx.Response(
                200, json={"code": 200, "result": {"songs": [song(1), song(2)]}}
            )

        self.assertIsNone(await self.lookup(handler))

    async def test_slow_lyric_request_raises_retryable_provider_error(self):
        async def handler(request):
            if request.url.path.endswith("/get"):
                return httpx.Response(
                    200, json={"code": 200, "result": {"songs": [song(1)]}}
                )
            await asyncio.sleep(5)

        with (
            patch("app.netease_lyrics._TIMEOUT_SECONDS", 0.1),
            self.assertRaises(NetEaseUnavailable),
        ):
            await self.lookup(handler)
