import ssl
import unittest
from unittest.mock import MagicMock, patch

import httpx
from app.providers import ProviderClient, TVDBClient


class TVDBTLSCompatibilityTest(unittest.TestCase):
    def setUp(self):
        TVDBClient._tokens.clear()

    @patch("app.providers.httpx.post")
    def test_login_uses_verified_tls_12(self, post):
        response = MagicMock()
        response.json.return_value = {"data": {"token": "token"}}
        post.return_value = response

        TVDBClient({"apiKey": "key"})._token()

        context = post.call_args.kwargs["verify"]
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(context.maximum_version, ssl.TLSVersion.TLSv1_2)

    def test_api_requests_use_the_tls_12_context(self):
        response = MagicMock()
        response.json.return_value = {}
        response.status_code = 200
        response.headers = {}
        client = TVDBClient({"apiKey": "key"})
        transport = MagicMock()
        transport.get.return_value = response

        with patch.object(client, "_http_client", return_value=transport) as factory:
            client._get("https://api4.thetvdb.com/v4/languages")

        context = factory.call_args.args[0]
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(context.maximum_version, ssl.TLSVersion.TLSv1_2)

    def test_transport_disconnect_is_retried(self):
        response = MagicMock()
        response.json.return_value = {"ok": True}
        response.status_code = 200
        response.headers = {}
        client = ProviderClient()
        transport = MagicMock()
        transport.get.side_effect = [httpx.ReadError("disconnected"), response]

        with (
            patch.object(client, "_http_client", return_value=transport),
            patch("app.providers.random.uniform", return_value=1.0),
            patch("app.providers.time.sleep") as sleep,
        ):
            payload = client._get("https://provider.example/item")

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(transport.get.call_count, 2)
        sleep.assert_called_once_with(0.25)


if __name__ == "__main__":
    unittest.main()
