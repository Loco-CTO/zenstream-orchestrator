import unittest

from langcodes import Language

from app.language_registry import (
    SUPPORTED_LANGUAGE_SET,
    language_options,
    normalize_metadata_locale,
    normalize_track_language,
)


class LanguageRegistryTest(unittest.TestCase):
    def test_canonicalizes_legacy_and_provider_codes(self):
        self.assertEqual(normalize_metadata_locale("eng"), "en")
        self.assertEqual(normalize_metadata_locale("jpn"), "ja")
        self.assertEqual(normalize_metadata_locale("zhtw"), "zh-TW")
        self.assertEqual(normalize_metadata_locale("zh_tw"), "zh-TW")
        self.assertEqual(normalize_metadata_locale("ja-JP"), "ja")

    def test_rejects_invalid_and_unlisted_languages(self):
        with self.assertRaises(ValueError):
            normalize_metadata_locale("jp")
        with self.assertRaises(ValueError):
            normalize_metadata_locale("ja-JA")
        with self.assertRaises(ValueError):
            normalize_metadata_locale("xx")

    def test_track_tags_drop_unknown_values_and_keep_supported_regions(self):
        self.assertEqual(normalize_track_language("jpn"), "ja")
        self.assertEqual(normalize_track_language("pt-BR"), "pt-BR")
        self.assertIsNone(normalize_track_language("und"))
        self.assertIsNone(normalize_track_language("xx"))

    def test_options_are_the_same_curated_registry(self):
        options = language_options()
        self.assertEqual({option["value"] for option in options}, SUPPORTED_LANGUAGE_SET)
        self.assertTrue(all(option["metadata"] and option["tracks"] for option in options))
        zh_tw = next(option for option in options if option["value"] == "zh-TW")
        language = Language.get("zh-TW")
        self.assertEqual(
            zh_tw["label"],
            language.display_name("en"),
        )

        en_gb = next(option for option in options if option["value"] == "en-GB")
        self.assertEqual(en_gb["label"], Language.get("en-GB").display_name("en"))

        zh_cn = next(option for option in options if option["value"] == "zh-CN")
        zh_cn_language = Language.get("zh-CN")
        self.assertEqual(
            zh_cn["label"],
            f"{zh_cn_language.display_name('en')} ({zh_cn_language.autonym()})",
        )

        english_in_japanese = next(
            option for option in language_options("ja") if option["value"] == "en"
        )
        english = Language.get("en")
        self.assertEqual(
            english_in_japanese["label"],
            f"{english.display_name('ja')} ({english.autonym()})",
        )
