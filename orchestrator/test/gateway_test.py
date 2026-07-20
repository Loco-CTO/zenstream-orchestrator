import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from api.zenstream import gateway


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["SECRET_KEY"] = "gateway-test-secret"
        os.environ["JELLYFIN_URL"] = "https://jellyfin.example"
        gateway._token_vault.clear()

    async def asyncTearDown(self):
        if gateway._client:
            await gateway._client.aclose()
        gateway._client = None

    def test_rewrites_hls_uris_to_zso_leases(self):
        content = b'#EXT-X-KEY:METHOD=AES-128,URI="keys/key.bin"\nsegment-1.ts\n'
        rewritten = gateway._rewrite_manifest(
            "item-1",
            "token",
            "https://jellyfin.example/Videos/item/master.m3u8",
            content,
        ).decode()
        self.assertIn('/api/video/item-1/stream?lease=', rewritten)
        self.assertNotIn("jellyfin.example", rewritten)
        self.assertNotIn("api_key", rewritten)

    async def test_stream_forwards_range_and_preserves_partial_response(self):
        seen = {}

        class BodyStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"abc"

        def handler(request: httpx.Request) -> httpx.Response:
            seen["range"] = request.headers.get("range")
            seen["authorization"] = request.headers.get("authorization")
            return httpx.Response(
                206,
                headers={
                    "content-type": "video/mp4",
                    "content-range": "bytes 0-2/3",
                    "content-length": "3",
                    "accept-ranges": "bytes",
                },
                stream=BodyStream(),
                request=request,
            )

        gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = SimpleNamespace(
            method="GET",
            headers={"range": "bytes=0-2"},
        )
        response = await gateway._stream_response(
            request,
            "token",
            "https://jellyfin.example/Videos/item/stream",
        )
        chunks = [chunk async for chunk in response.body_iterator]
        self.assertEqual(b"abc", b"".join(chunks))
        self.assertEqual(206, response.status_code)
        self.assertEqual("bytes 0-2/3", response.headers["content-range"])
        self.assertEqual("bytes=0-2", seen["range"])
        self.assertIn('Token="token"', seen["authorization"])

    def test_rewrites_playback_source_without_upstream_origin(self):
        rewritten = gateway.rewrite_playback_urls(
            {
                "MediaSources": [
                    {
                        "TranscodingUrl": "/Videos/item/master.m3u8?PlaySessionId=session&api_key=secret",
                    }
                ]
            },
            "token",
            "user-1",
            "item-1",
        )
        url = rewritten["MediaSources"][0]["TranscodingUrl"]
        self.assertTrue(url.startswith("/api/video/item-1/stream?lease="))
        self.assertNotIn("jellyfin", url)
        self.assertNotIn("api_key", url)


if __name__ == "__main__":
    unittest.main()
