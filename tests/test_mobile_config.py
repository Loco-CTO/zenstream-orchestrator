import os
import sys
import unittest
import asyncio
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from app.app import mobile_config


class MobileConfigTests(unittest.TestCase):
    def test_returns_configured_jellyfin_url_without_authentication(self):
        with patch.dict(os.environ, {"JELLYFIN_URL": "https://jellyfin.example/"}):
            self.assertEqual(
                asyncio.run(mobile_config()),
                {"jellyfinUrl": "https://jellyfin.example"},
            )

    def test_rejects_missing_jellyfin_url(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(Exception) as raised:
                asyncio.run(mobile_config())
            self.assertEqual(raised.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
