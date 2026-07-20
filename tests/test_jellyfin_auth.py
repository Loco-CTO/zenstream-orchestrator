import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from jellyfin import api_service


class JellyfinAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.previous_url = os.environ.get("JELLYFIN_URL")
        os.environ["JELLYFIN_URL"] = "https://jellyfin.example"
        with api_service._auth_cache_lock:
            api_service._auth_cache.clear()

    def tearDown(self):
        if self.previous_url is None:
            os.environ.pop("JELLYFIN_URL", None)
        else:
            os.environ["JELLYFIN_URL"] = self.previous_url
        with api_service._auth_cache_lock:
            api_service._auth_cache.clear()

    @staticmethod
    def response(user_id="user"):
        result = Mock(status_code=200)
        result.json.return_value = {"Id": user_id}
        return result

    def test_reuses_a_recent_successful_authentication(self):
        with patch(
            "jellyfin.api_service.requests.get", return_value=self.response()
        ) as request:
            self.assertEqual(api_service.authenticated_user_id("token"), "user")
            self.assertEqual(api_service.authenticated_user_id("token"), "user")

        request.assert_called_once()

    def test_revalidates_after_the_short_cache_period(self):
        with (
            patch("jellyfin.api_service.time.monotonic", side_effect=[100, 100, 131]),
            patch(
                "jellyfin.api_service.requests.get", return_value=self.response()
            ) as request,
        ):
            self.assertEqual(api_service.authenticated_user_id("token"), "user")
            self.assertEqual(api_service.authenticated_user_id("token"), "user")
            self.assertEqual(api_service.authenticated_user_id("token"), "user")

        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
