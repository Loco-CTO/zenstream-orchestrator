import unittest
from unittest.mock import patch

from app import offline_conversion


class OfflineConversionCapabilitiesTest(unittest.TestCase):
    def test_first_lookup_runs_before_monotonic_cache_window(self):
        expected = {"video": False, "audio": False, "toneMapping": False}
        with (
            patch.object(offline_conversion, "_capabilities", {}),
            patch.object(offline_conversion, "_capability_time", None),
            patch.object(offline_conversion.time, "monotonic", return_value=1.0),
            patch.object(offline_conversion, "ffmpeg_path", return_value=None) as ffmpeg,
        ):
            self.assertEqual(offline_conversion.conversion_capabilities(), expected)

        ffmpeg.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
