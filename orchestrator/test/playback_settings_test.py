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
    def test_zero_defaults_to_unlimited(self):
        settings = PlaybackSettings(MemoryDatabase())
        self.assertEqual(
            settings.get(),
            {"maxTranscodes": 0, "maxTranscodesPerUser": 0},
        )
        self.assertEqual(
            settings.set(0, 0),
            {"maxTranscodes": 0, "maxTranscodesPerUser": 0},
        )

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
        with self.assertRaisesRegex(ValueError, "between 0 and 64"):
            PlaybackSettings.normalize(65, 1)

    def test_unlimited_global_or_user_limit_is_valid(self):
        self.assertEqual(
            PlaybackSettings.normalize(0, 4),
            {"maxTranscodes": 0, "maxTranscodesPerUser": 4},
        )
        self.assertEqual(
            PlaybackSettings.normalize(4, 0),
            {"maxTranscodes": 4, "maxTranscodesPerUser": 0},
        )
