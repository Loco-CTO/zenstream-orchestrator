import os
import unittest
from unittest.mock import patch

from app.models.playback_settings import PlaybackSettings


class MemoryDatabase:
    def __init__(self):
        self.row = None

    def execute(self, query, params=None):
        if query.startswith("SELECT"):
            return [self.row] if self.row else []
        self.row = (params[0], params[1])
        return []


class PlaybackSettingsTest(unittest.TestCase):
    def test_defaults_follow_environment_until_saved(self):
        with patch.dict(
            os.environ,
            {"MAX_TRANSCODES": "4", "MAX_TRANSCODES_PER_USER": "2"},
            clear=False,
        ):
            settings = PlaybackSettings(MemoryDatabase())
            self.assertEqual(
                settings.get(),
                {"maxTranscodes": 4, "maxTranscodesPerUser": 2},
            )

    def test_saved_values_round_trip(self):
        database = MemoryDatabase()
        settings = PlaybackSettings(database)
        self.assertEqual(
            settings.set(6, 3),
            {"maxTranscodes": 6, "maxTranscodesPerUser": 3},
        )
        self.assertEqual(settings.get(), settings.set(6, 3))

    def test_per_user_limit_cannot_exceed_global_limit(self):
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            PlaybackSettings.normalize(2, 3)

    def test_limits_have_a_safe_upper_bound(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 64"):
            PlaybackSettings.normalize(65, 1)

