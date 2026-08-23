import unittest
from pathlib import Path
from unittest.mock import patch

from app.bazarr import (
    BazarrMatchError,
    BazarrTarget,
    _associated_sidecar,
    _effective_bazarr_root,
    _find_episode,
    _path_key,
    _search_candidates,
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


class _CachedBazarrClient(_BazarrClient):
    cache_key = ("test", 6767, "", False)

    def __init__(self, series, episodes):
        super().__init__(series, episodes)
        self.series_calls = 0
        self.episode_calls = 0

    def series(self):
        self.series_calls += 1
        return super().series()

    def episodes(self, series_id):
        self.episode_calls += 1
        return super().episodes(series_id)


class _SearchBazarrClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def search(self, episode_id):
        self.calls += 1
        return self.responses.pop(0)


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
    def test_unmapped_library_uses_zenstream_directory(self):
        self.assertEqual(
            _effective_bazarr_root(None, r"C:\\Media\\TV"),
            r"C:\\Media\\TV",
        )
        self.assertEqual(
            _effective_bazarr_root("/tv", r"C:\\Media\\TV"),
            "/tv",
        )

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

    def test_exact_resolution_is_reused_for_status_and_search(self):
        client = _CachedBazarrClient(
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

        _find_episode(client, _target())
        _find_episode(client, _target())

        self.assertEqual(client.series_calls, 1)
        self.assertEqual(client.episode_calls, 1)

    @patch("app.bazarr.BAZARR_EMPTY_SEARCH_RETRY_DELAY_SECONDS", 0)
    def test_empty_provider_search_is_retried(self):
        client = _SearchBazarrClient(
            [
                [],
                [
                    {
                        "provider": "opensubtitles",
                        "subtitle": "subtitle-1",
                        "language": "en",
                        "name": "English",
                    }
                ],
            ]
        )

        matches = _search_candidates(client, 90)

        self.assertEqual(client.calls, 2)
        self.assertEqual(matches[0]["subtitle"], "subtitle-1")

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
