import tempfile
import unittest
from pathlib import Path

from app.library import guess_media, provider_ids
from app.providers import choose_image


class LibraryMetadataTest(unittest.TestCase):
    def test_jellyfin_style_provider_ids_are_extracted(self):
        self.assertEqual(
            provider_ids("The Matrix (1999) [tmdbid-603] [tvdbid-Movie-123]"),
            [("tmdb", "movie", "603"), ("tvdb", "series", "Movie-123")],
        )

    def test_guessit_fallback_reads_season_and_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Show - S02E03 - Episode.mkv"
            path.touch()
            parsed = guess_media(path)
            self.assertEqual(int(parsed["season"]), 2)
            self.assertEqual(int(parsed["episode"]), 3)

    def test_image_fallback_order_is_requested_no_language_english_any(self):
        images = [
            {"type": "poster", "language": "fr", "url": "fr"},
            {"type": "poster", "language": "en", "url": "en"},
            {"type": "poster", "language": None, "url": "neutral"},
            {"type": "poster", "language": "ja", "url": "ja"},
        ]
        self.assertEqual(choose_image(images, "ja-JP", "poster")["url"], "ja")
        self.assertEqual(choose_image(images, "de-DE", "poster")["url"], "neutral")


if __name__ == "__main__":
    unittest.main()
