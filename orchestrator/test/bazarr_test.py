import unittest
from pathlib import Path

from app.bazarr import (
    BazarrMatchError,
    BazarrTarget,
    _associated_sidecar,
    _find_episode,
    _path_key,
    mapped_path,
)


class _BazarrClient:
    def __init__(self, series, episodes):
        self._series = series
        self._episodes = episodes

    def series(self):
        return self._series

    def episodes(self, series_id):
        return [
            episode
            for episode in self._episodes
            if episode.get("seriesId") == series_id
        ]


def _target(**values):
    defaults = {
        "user_id": "user",
        "entity_id": "episode",
        "source_id": "source",
        "media_file_id": "media",
        "library_id": "library",
        "library_directory": "/media",
        "relative_path": "Show/Season 1/Show - S01E02.mkv",
        "series_relative_path": "Show",
        "season_number": 1,
        "episode_number": 2,
        "size": 100,
        "modified_ns": 200,
        "quick_fingerprint": "fingerprint",
        "bazarr_root_path": "/tv",
        "target_path": "/tv/Show/Season 1/Show - S01E02.mkv",
        "local_subtitles": (),
        "series_provider_ids": (("tvdb", "123"),),
    }
    defaults.update(values)
    return BazarrTarget(**defaults)


class BazarrMatchingTest(unittest.TestCase):
    def test_path_mapping_normalizes_container_and_windows_separators(self):
        self.assertEqual(
            _path_key(mapped_path(r"C:\\Bazarr\\TV", "Show/./Episode.mkv")),
            "c:/bazarr/tv/show/episode.mkv",
        )
        self.assertTrue(
            _path_key(r"C:\\Bazarr\\TV\\Show\\Episode.mkv")
            == _path_key("c:/bazarr/tv/show/episode.mkv")
        )

    def test_sidecar_uses_the_longest_matching_media_stem(self):
        self.assertTrue(
            _associated_sidecar(
                Path("Show/Episode.mkv"),
                Path("Show/Episode.en.srt"),
            )
        )
        self.assertFalse(
            _associated_sidecar(
                Path("Show/Episode.mkv"),
                Path("Show/Episode.alt.en.srt"),
                ["Show/Episode.mkv", "Show/Episode.alt.mkv"],
            )
        )
        self.assertTrue(
            _associated_sidecar(
                Path("Show/Episode.alt.mkv"),
                Path("Show/Episode.alt.en.srt"),
                ["Show/Episode.mkv", "Show/Episode.alt.mkv"],
            )
        )

    def test_provider_id_does_not_fallback_to_a_different_series_path(self):
        client = _BazarrClient(
            [{"path": "/tv/Other Show", "sonarrSeriesId": 9, "tvdbId": "123"}],
            [],
        )
        with self.assertRaises(BazarrMatchError) as error:
            _find_episode(client, _target())
        self.assertEqual(error.exception.code, "unmatched")

    def test_exact_path_and_episode_number_are_required(self):
        client = _BazarrClient(
            [{"path": "/tv/Show", "sonarrSeriesId": 9, "tvdbId": "123"}],
            [
                {
                    "seriesId": 9,
                    "path": "/tv/Show/Season 1/Show - S01E02.mkv",
                    "sonarrEpisodeId": 90,
                    "season": 1,
                    "episode": 2,
                }
            ],
        )
        result = _find_episode(client, _target())
        self.assertEqual(result["seriesId"], 9)
        self.assertEqual(result["episodeId"], 90)

    def test_duplicate_exact_episode_entries_are_ambiguous(self):
        series = [{"path": "/tv/Show", "sonarrSeriesId": 9}]
        episodes = [
            {
                "seriesId": 9,
                "path": "/tv/Show/Season 1/Show - S01E02.mkv",
                "sonarrEpisodeId": 90,
                "season": 1,
                "episode": 2,
            },
            {
                "seriesId": 9,
                "path": "/tv/Show/Season 1/Show - S01E02.mkv/./",
                "sonarrEpisodeId": 91,
                "season": 1,
                "episode": 2,
            },
        ]
        with self.assertRaises(BazarrMatchError) as error:
            _find_episode(_BazarrClient(series, episodes), _target())
        self.assertEqual(error.exception.code, "ambiguous")


if __name__ == "__main__":
    unittest.main()
