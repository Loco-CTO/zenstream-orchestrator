import unittest

from app.metadata_domain import rank_artwork_candidates


class BackdropLanguageRankingTest(unittest.TestCase):
    def setUp(self):
        self.images = [
            {
                "type": "Backdrop",
                "url": "requested",
                "language": "fr-FR",
                "provider": "tmdb",
            },
            {
                "type": "Backdrop",
                "url": "neutral-none",
                "language": None,
                "provider": "tmdb",
            },
            {
                "type": "Backdrop",
                "url": "neutral-empty",
                "language": "",
                "provider": "tmdb",
            },
            {
                "type": "Backdrop",
                "url": "english",
                "language": "en",
                "provider": "tmdb",
            },
            {
                "type": "Backdrop",
                "url": "original",
                "language": "ja",
                "provider": "tmdb",
            },
        ]

    def test_default_order_is_preserved(self):
        ranked = rank_artwork_candidates(
            self.images, "fr", "Backdrop", "ja", ["tmdb"]
        )
        self.assertEqual(
            [image["url"] for image in ranked],
            ["requested", "neutral-none", "neutral-empty", "english", "original"],
        )

    def test_preference_moves_neutral_candidates_before_requested(self):
        ranked = rank_artwork_candidates(
            self.images,
            "fr",
            "Backdrop",
            "ja",
            ["tmdb"],
            prefer_no_language_for_backdrop=True,
        )
        self.assertEqual(
            [image["url"] for image in ranked],
            ["neutral-none", "neutral-empty", "requested", "english", "original"],
        )

    def test_preference_does_not_change_other_artwork_types(self):
        ranked = rank_artwork_candidates(
            [
                {**self.images[0], "type": "Primary", "url": "requested"},
                {**self.images[1], "type": "Primary", "url": "neutral"},
            ],
            "fr",
            "Primary",
            "ja",
            ["tmdb"],
            prefer_no_language_for_backdrop=True,
        )
        self.assertEqual([image["url"] for image in ranked], ["requested", "neutral"])


if __name__ == "__main__":
    unittest.main()
