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
        self.row = tuple(params[:7])
        return []


class PlaybackSettingsTest(unittest.TestCase):
    def test_zero_defaults_to_unlimited(self):
        settings = PlaybackSettings(MemoryDatabase())
        self.assertEqual(
            settings.get(),
            {
                "maxTranscodes": 0,
                "maxTranscodesPerUser": 0,
                "trickplayFrameWidth": 320,
                "trickplayFrameHeight": 180,
                "trickplayIntervalSeconds": 10,
                "trickplayWorkers": 1,
                "trickplayFfmpegThreads": 4,
            },
        )
        self.assertEqual(
            settings.set(0, 0),
            {
                "maxTranscodes": 0,
                "maxTranscodesPerUser": 0,
                "trickplayFrameWidth": 320,
                "trickplayFrameHeight": 180,
                "trickplayIntervalSeconds": 10,
                "trickplayWorkers": 1,
                "trickplayFfmpegThreads": 4,
            },
        )

    def test_unlimited_global_does_not_clamp_user_limit(self):
        with patch.dict(
            os.environ,
            {"MAX_TRANSCODES": "0", "MAX_TRANSCODES_PER_USER": "4"},
            clear=False,
        ):
            self.assertEqual(
                PlaybackSettings(MemoryDatabase()).get(),
                {
                    "maxTranscodes": 0,
                    "maxTranscodesPerUser": 4,
                    "trickplayFrameWidth": 320,
                    "trickplayFrameHeight": 180,
                    "trickplayIntervalSeconds": 10,
                    "trickplayWorkers": 1,
                    "trickplayFfmpegThreads": 4,
                },
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
                {
                    "maxTranscodes": 4,
                    "maxTranscodesPerUser": 2,
                    "trickplayFrameWidth": 320,
                    "trickplayFrameHeight": 180,
                    "trickplayIntervalSeconds": 10,
                    "trickplayWorkers": 1,
                    "trickplayFfmpegThreads": 4,
                },
            )

    def test_saved_values_round_trip(self):
        database = MemoryDatabase()
        settings = PlaybackSettings(database)
        self.assertEqual(
            settings.set(6, 3),
            {
                "maxTranscodes": 6,
                "maxTranscodesPerUser": 3,
                "trickplayFrameWidth": 320,
                "trickplayFrameHeight": 180,
                "trickplayIntervalSeconds": 10,
                "trickplayWorkers": 1,
                "trickplayFfmpegThreads": 4,
            },
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
            {
                "maxTranscodes": 0,
                "maxTranscodesPerUser": 4,
                "trickplayFrameWidth": 320,
                "trickplayFrameHeight": 180,
                "trickplayIntervalSeconds": 10,
                "trickplayWorkers": 1,
                "trickplayFfmpegThreads": 4,
            },
        )
        self.assertEqual(
            PlaybackSettings.normalize(4, 0),
            {
                "maxTranscodes": 4,
                "maxTranscodesPerUser": 0,
                "trickplayFrameWidth": 320,
                "trickplayFrameHeight": 180,
                "trickplayIntervalSeconds": 10,
                "trickplayWorkers": 1,
                "trickplayFfmpegThreads": 4,
            },
        )

    def test_trickplay_frame_width_derives_an_exact_16_by_9_height(self):
        self.assertEqual(
            PlaybackSettings.normalize(0, 0, 640, 360, 60, 64)["trickplayFrameHeight"],
            360,
        )
        with self.assertRaisesRegex(ValueError, "divisible by 16"):
            PlaybackSettings.normalize(0, 0, 321, 181, 10)
        with self.assertRaisesRegex(ValueError, "between 1 and 60"):
            PlaybackSettings.normalize(0, 0, 320, 180, 61)

    def test_trickplay_workers_are_bounded_and_persisted(self):
        self.assertEqual(
            PlaybackSettings.normalize(0, 0, 320, 180, 10, 64)["trickplayWorkers"], 64
        )
        with self.assertRaisesRegex(
            ValueError, "trickplayWorkers must be between 1 and 64"
        ):
            PlaybackSettings.normalize(0, 0, 320, 180, 10, 0)
        with self.assertRaisesRegex(
            ValueError, "trickplayWorkers must be between 1 and 64"
        ):
            PlaybackSettings.normalize(0, 0, 320, 180, 10, 65)
        database = MemoryDatabase()
        settings = PlaybackSettings(database)
        self.assertEqual(settings.set(0, 0, 320, 180, 10, 4)["trickplayWorkers"], 4)
        self.assertEqual(settings.get()["trickplayWorkers"], 4)

    def test_trickplay_ffmpeg_threads_allow_auto_and_are_bounded(self):
        self.assertEqual(
            PlaybackSettings.normalize(0, 0, 320, 180, 10, 1, 0)[
                "trickplayFfmpegThreads"
            ],
            0,
        )
        self.assertEqual(
            PlaybackSettings.normalize(0, 0, 320, 180, 10, 1, 64)[
                "trickplayFfmpegThreads"
            ],
            64,
        )
        with self.assertRaisesRegex(
            ValueError, "trickplayFfmpegThreads must be between 0 and 64"
        ):
            PlaybackSettings.normalize(0, 0, 320, 180, 10, 1, -1)
        with self.assertRaisesRegex(
            ValueError, "trickplayFfmpegThreads must be between 0 and 64"
        ):
            PlaybackSettings.normalize(0, 0, 320, 180, 10, 1, 65)
