import os
import unittest
import gzip
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

    async def test_stream_drops_compression_and_upstream_length_headers(self):
        class BodyStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                # MockTransport does not run HTTPX's decompressor. The live
                # transport hands this method an already-decoded body while
                # retaining the upstream headers, which is what we normalize.
                yield b"abc"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "content-type": "image/jpeg",
                    "content-encoding": "gzip",
                    "content-length": "23",
                },
                stream=BodyStream(),
                request=request,
            )

        gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = SimpleNamespace(method="GET", headers={})
        response = await gateway._stream_response(
            request,
            "token",
            "https://jellyfin.example/Items/item/Images/Primary",
        )
        chunks = [chunk async for chunk in response.body_iterator]

        self.assertEqual(b"abc", b"".join(chunks))
        self.assertNotIn("content-encoding", response.headers)
        self.assertNotIn("content-length", response.headers)

    async def test_buffered_assets_request_identity_and_rebuild_body_headers(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["accept-encoding"] = request.headers.get("accept-encoding")
            return httpx.Response(
                200,
                headers={
                    "content-type": "image/webp",
                    "content-encoding": "gzip",
                    "content-length": "999",
                },
                content=gzip.compress(b"image-bytes"),
                request=request,
            )

        gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = SimpleNamespace(headers={})
        response = await gateway._buffered_asset_response(request, "token", "https://jellyfin.example/image")

        self.assertEqual(b"image-bytes", response.body)
        self.assertEqual("identity", seen["accept-encoding"])
        self.assertNotIn("content-encoding", response.headers)
        self.assertNotEqual("999", response.headers.get("content-length"))

    async def test_hls_segments_are_buffered_before_proxying(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "video/mp2t", "content-length": "7"},
                content=b"segment",
                request=request,
            )

        gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = SimpleNamespace(method="GET", headers={})
        response = await gateway._stream_response(
            request,
            "token",
            "https://jellyfin.example/videos/item/segment.ts",
            manifest_item_id="item-1",
        )

        self.assertEqual(b"segment", response.body)
        self.assertEqual("video/mp2t", response.headers["content-type"])

    async def test_json_proxy_rebuilds_decoded_body_headers(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                    "content-length": "999",
                },
                content=gzip.compress(b'{"Items": []}'),
                request=request,
            )

        gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = SimpleNamespace(method="GET", headers={})
        response = await gateway._upstream_json(
            request,
            "token",
            "/Items",
        )

        self.assertEqual(b'{"Items": []}', response.body)
        self.assertNotIn("content-encoding", response.headers)
        self.assertNotEqual("999", response.headers.get("content-length"))

    async def test_login_uses_a_complete_jellyfin_device_profile(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["authorization"] = request.headers.get("authorization", "")
            return httpx.Response(
                200,
                json={
                    "AccessToken": "token",
                    "User": {"Id": "user-1", "Name": "test"},
                },
                request=request,
            )

        gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = SimpleNamespace(body=lambda: None)
        request.body = lambda: __import__("asyncio").sleep(0, result=b'{"username":"test","password":"password"}')
        response = await gateway.login(request)

        self.assertEqual(200, response.status_code)
        self.assertIn('Client="ZenStream"', seen["authorization"])
        self.assertIn('Device="ZenStream Server"', seen["authorization"])
        self.assertIn('DeviceId="Orchestrator"', seen["authorization"])
        self.assertIn('Version="0.0.1b"', seen["authorization"])
        self.assertIn("ResourceTicket", response.body.decode())

    async def test_media_ticket_returns_decoded_lease_payload(self):
        token = "media-token"
        await gateway._remember_token(token)
        lease = gateway._asset_ticket(
            token,
            "user-1",
            path="/Videos/item/master.m3u8",
            query=[("MediaSourceId", "source-1")],
        )
        request = SimpleNamespace(headers={}, query_params={})
        with patch.object(gateway, "authenticated_user_id", return_value="user-1"):
            authenticated_token, payload = await gateway._media_ticket(request, lease)

        self.assertEqual(token, authenticated_token)
        self.assertEqual("media", payload["kind"])
        self.assertEqual("/Videos/item/master.m3u8", payload["path"])

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
