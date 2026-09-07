import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.lyrics import (
    choose_lyrics,
    embedded_lyrics,
    lyrics_to_vtt,
    parse_lyrics_text,
)
from app.playback import PlaybackManager


class LyricsParserTest(unittest.TestCase):
    def test_lrc_supports_metadata_multiple_timestamps_and_ends(self):
        result = parse_lyrics_text(
            "[ar:Artist]\n[al:Album]\n[00:01.50][00:03:00]Hello\n[00:07]World",
            12,
        )

        self.assertIsNotNone(result)
        self.assertTrue(result["timed"])
        self.assertEqual(
            result["lines"],
            [
                {"text": "Hello", "startSeconds": 1.5, "endSeconds": 3.0},
                {"text": "Hello", "startSeconds": 3.0, "endSeconds": 7.0},
                {"text": "World", "startSeconds": 7.0, "endSeconds": 12.0},
            ],
        )

    def test_plain_text_is_preserved_when_no_timestamps_exist(self):
        result = parse_lyrics_text("[ti:Song]\nFirst line\n\nSecond line")

        self.assertEqual(
            result,
            {
                "timed": False,
                "lines": [{"text": "First line"}, {"text": "Second line"}],
            },
        )

    def test_embedded_synchronized_and_unsynchronized_tags_are_supported(self):
        sylt = SimpleNamespace(
            FrameID="SYLT",
            lang="jpn",
            text=[("first", 0), ("second", 4200)],
        )
        uslt = SimpleNamespace(
            FrameID="USLT",
            lang="eng",
            text="plain embedded lyrics",
        )

        class Tags(dict):
            pass

        fake_audio = SimpleNamespace(tags=Tags(sylt=sylt, uslt=uslt))
        fake_mutagen = SimpleNamespace(File=lambda path, easy=False: fake_audio)
        with patch.dict(sys.modules, {"mutagen": fake_mutagen}):
            values = embedded_lyrics(Path("track.mp3"), 8)

        self.assertEqual(len(values), 2)
        self.assertTrue(values[0]["timed"])
        self.assertEqual(values[0]["language"], "jpn")
        self.assertFalse(values[1]["timed"])

    def test_embedded_vorbis_mp4_or_ape_lyric_keys_are_supported(self):
        fake_audio = SimpleNamespace(tags={"LYRICS": "[00:02]Tagged line"})
        fake_mutagen = SimpleNamespace(File=lambda path, easy=False: fake_audio)
        with patch.dict(sys.modules, {"mutagen": fake_mutagen}):
            values = embedded_lyrics(Path("track.flac"))

        self.assertEqual(values[0]["source"], "embedded")
        self.assertEqual(values[0]["lines"][0]["startSeconds"], 2.0)

    def test_timed_embedded_lyrics_win_over_plain_sidecars(self):
        selected = choose_lyrics(
            [
                {
                    "source": "sidecar",
                    "timed": False,
                    "lines": [{"text": "plain"}],
                    "_order": 0,
                },
                {
                    "source": "embedded",
                    "timed": True,
                    "lines": [{"text": "timed", "startSeconds": 0}],
                    "_order": 4,
                },
            ]
        )

        self.assertEqual(selected["source"], "embedded")
        self.assertTrue(selected["timed"])

    def test_plain_sidecar_is_selected_without_exposing_its_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "song.flac").write_bytes(b"not a real audio file")
            (root / "song.lrc").write_text(
                "[ar:Artist]\n[00:01]local lyric", encoding="utf-8"
            )
            database = MagicMock()
            database.execute.side_effect = [
                [
                    ("media-1", "song.flac", None, "media", directory),
                    ("lyrics-1", "song.lrc", "ja", "lyrics", directory),
                ],
                [("media-1", 42.0)],
            ]
            manager = object.__new__(PlaybackManager)
            manager.db = database
            manager.catalog = MagicMock()

            result = manager.lyrics("user-1", "track-1")

        self.assertEqual(result["trackId"], "track-1")
        self.assertEqual(result["lyrics"]["source"], "sidecar")
        self.assertEqual(result["lyrics"]["language"], "ja")
        self.assertNotIn(directory, repr(result))

    def test_plain_text_vtt_retains_the_legacy_long_cue(self):
        value = lyrics_to_vtt("plain line")

        self.assertIn("00:00:00.000 --> 99:59:59.000", value)


if __name__ == "__main__":
    unittest.main()
