import ssl
import unittest
from unittest.mock import MagicMock, patch

from app.providers import TVDBClient


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

    @patch("app.providers.httpx.get")
    def test_api_requests_use_the_tls_12_context(self, get):
        response = MagicMock()
        response.json.return_value = {}
        get.return_value = response

        TVDBClient({"apiKey": "key"})._get("https://api4.thetvdb.com/v4/languages")

        context = get.call_args.kwargs["verify"]
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(context.maximum_version, ssl.TLSVersion.TLSv1_2)


if __name__ == "__main__":
    unittest.main()
