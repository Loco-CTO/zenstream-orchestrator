import unittest
from pathlib import Path
from unittest.mock import patch

from api.zenstream import bazarr_routes
from app.bazarr import (
    BazarrError,
    BazarrMappingStore,
    BazarrMatchError,
    BazarrSubtitleService,
    BazarrSyncService,
    BazarrTarget,
    _associated_sidecar,
    _effective_bazarr_root,
    _find_episode,
    _path_key,
    _resolve_episode_values,
    _resolve_series_values,
    _search_candidates,
    mapped_path,
)
from app.database import DatabaseHandler


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


class _SyncBazarrClient:
    def __init__(self, series_values, episode_values=None, episode_error=None):
        self.series_values = series_values
        self.episode_values = episode_values or []
        self.episode_error = episode_error
        self.series_calls = 0
        self.episode_calls = []
        self.closed = False

    def series(self):
        self.series_calls += 1
        return self.series_values

    def episodes(self, series_id):
        self.episode_calls.append(series_id)
        if self.episode_error:
            raise self.episode_error
        return self.episode_values

    def close(self):
        self.closed = True


def _target(**values):
    defaults = {
        "user_id": "user",
        "entity_id": "episode",
        "source_id": "source",
        "media_file_id": "media",
        "library_id": "library",
        "series_entity_id": "series",
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


def _mapping_db():
    db = DatabaseHandler("sqlite", {}, ":memory:")
    for statement in (
        "CREATE TABLE libraries(id TEXT PRIMARY KEY,directory TEXT,type TEXT)",
        "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,relative_path TEXT,season_number INTEGER,episode_number INTEGER)",
        "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,size INTEGER,modified_ns INTEGER,quick_fingerprint TEXT)",
        "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,identifier_type TEXT,provider_id TEXT)",
        "CREATE TABLE bazarr_library_mappings(library_id TEXT PRIMARY KEY,bazarr_root_path TEXT)",
        "CREATE TABLE bazarr_series_mappings(series_entity_id TEXT PRIMARY KEY,library_id TEXT,target_path TEXT,bazarr_series_id INTEGER,state TEXT,message TEXT,updated_at TEXT,synced_at TEXT)",
        "CREATE TABLE bazarr_episode_mappings(media_file_id TEXT PRIMARY KEY,entity_id TEXT,series_entity_id TEXT,target_path TEXT,size INTEGER,modified_ns INTEGER,quick_fingerprint TEXT,bazarr_series_id INTEGER,bazarr_episode_id INTEGER,state TEXT,title TEXT,season_number INTEGER,episode_number INTEGER,subtitles_json TEXT,message TEXT,updated_at TEXT,synced_at TEXT)",
    ):
        db.execute(statement)
    return db


def _seed_mapping_inventory(
    db, *, size=100, modified_ns=200, fingerprint="fingerprint"
):
    db.execute(
        "INSERT INTO libraries(id,directory,type) VALUES('library','/media','tv_series')"
    )
    db.execute(
        "INSERT INTO bazarr_library_mappings(library_id,bazarr_root_path) VALUES('library','/tv')"
    )
    db.execute(
        "INSERT INTO library_entities(id,library_id,parent_id,entity_type,relative_path) VALUES('series','library',NULL,'series','Show')"
    )
    db.execute(
        "INSERT INTO library_entities(id,library_id,parent_id,entity_type,relative_path) VALUES('season','library','series','season','Show/Season 1')"
    )
    db.execute(
        "INSERT INTO library_entities(id,library_id,parent_id,entity_type,relative_path,season_number,episode_number) VALUES('episode','library','season','episode','Show/Season 1/Show - S01E02.mkv',1,2)"
    )
    db.execute(
        "INSERT INTO media_files(id,entity_id,relative_path,role,size,modified_ns,quick_fingerprint) VALUES('media','episode','Show/Season 1/Show - S01E02.mkv','media',?,?,?)",
        (size, modified_ns, fingerprint),
    )
    db.execute(
        "INSERT INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id) VALUES('series','tvdb','series','123')"
    )


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

    def test_series_mapping_uses_exact_normalized_parent_path(self):
        result = _resolve_series_values(
            r"C:\Bazarr\TV",
            "Show",
            (("tvdb", "123"),),
            [
                {
                    "path": "c:/bazarr/tv/Show/./",
                    "sonarrSeriesId": 9,
                    "tvdbId": "123",
                }
            ],
        )
        self.assertEqual(result["sonarrSeriesId"], 9)

    def test_duplicate_series_paths_are_ambiguous(self):
        with self.assertRaises(BazarrMatchError) as error:
            _resolve_series_values(
                "/tv",
                "Show",
                (),
                [
                    {"path": "/tv/Show", "sonarrSeriesId": 9},
                    {"path": "/tv/Show/./", "sonarrSeriesId": 10},
                ],
            )
        self.assertEqual(error.exception.code, "ambiguous")

    def test_series_provider_identity_conflict_is_rejected(self):
        with self.assertRaises(BazarrMatchError) as error:
            _resolve_series_values(
                "/tv",
                "Show",
                (("tvdb", "123"),),
                [{"path": "/tv/Show", "sonarrSeriesId": 9, "tvdbId": "999"}],
            )
        self.assertEqual(error.exception.code, "identity_conflict")

    def test_episode_numbering_conflict_is_rejected_after_path_match(self):
        with self.assertRaises(BazarrMatchError) as error:
            _resolve_episode_values(
                "/tv/Show/Season 1/Show - S01E02.mkv",
                1,
                2,
                {"sonarrSeriesId": 9},
                [
                    {
                        "path": "/tv/Show/Season 1/Show - S01E02.mkv",
                        "sonarrEpisodeId": 90,
                        "season": 1,
                        "episode": 3,
                    }
                ],
            )
        self.assertEqual(error.exception.code, "identity_conflict")

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


class BazarrMappingCacheTest(unittest.TestCase):
    def setUp(self):
        self.db = _mapping_db()
        _seed_mapping_inventory(self.db)

    def tearDown(self):
        self.db.close()

    def _insert_mapping(self, *, size=100, modified_ns=200, fingerprint="fingerprint"):
        self.db.execute(
            "INSERT INTO bazarr_series_mappings(series_entity_id,library_id,target_path,bazarr_series_id,state,message,updated_at,synced_at) VALUES(?,?,?,?,?,?,?,?)",
            ("series", "library", "/tv/Show", 9, "matched", None, "now", "now"),
        )
        self.db.execute(
            "INSERT INTO bazarr_episode_mappings(media_file_id,entity_id,series_entity_id,target_path,size,modified_ns,quick_fingerprint,bazarr_series_id,bazarr_episode_id,state,title,season_number,episode_number,subtitles_json,message,updated_at,synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "media",
                "episode",
                "series",
                "/tv/Show/Season 1/Show - S01E02.mkv",
                size,
                modified_ns,
                fingerprint,
                9,
                90,
                "matched",
                "Episode 2",
                1,
                2,
                '[{"language":"en"}]',
                None,
                "now",
                "now",
            ),
        )

    def test_local_mapping_resolves_without_bazarr_lookup(self):
        self._insert_mapping()
        resolution = BazarrMappingStore(self.db).resolve(_target())
        self.assertEqual(resolution["seriesId"], 9)
        self.assertEqual(resolution["episodeId"], 90)
        self.assertEqual(resolution["episode"]["subtitles"][0]["language"], "en")

    def test_changed_signature_or_root_returns_sync_pending(self):
        self._insert_mapping()
        for target in (
            _target(size=101),
            _target(
                bazarr_root_path="/new-tv",
                target_path="/new-tv/Show/Season 1/Show - S01E02.mkv",
            ),
        ):
            with self.assertRaises(BazarrMatchError) as error:
                BazarrMappingStore(self.db).resolve(target)
            self.assertEqual(error.exception.code, "sync_pending")

    def test_status_reads_local_mapping_without_constructing_client(self):
        target = _target()
        resolution = {
            "seriesId": 9,
            "episodeId": 90,
            "episode": {
                "title": "Episode 2",
                "season": 1,
                "episode": 2,
                "subtitles": [{"language": "en", "hearingImpaired": True}],
            },
        }
        with (
            patch("app.bazarr._target", return_value=target),
            patch(
                "app.bazarr.BazarrConnectionStore.internal",
                return_value={"address": "bazarr"},
            ),
            patch("app.bazarr.BazarrMappingStore.resolve", return_value=resolution),
            patch("app.bazarr.BazarrClient") as client,
        ):
            result = BazarrSubtitleService().status("user", "episode", "source")
        self.assertEqual(result["state"], "matched")
        self.assertTrue(result["episode"]["subtitles"][0]["hearingImpaired"])
        client.assert_not_called()

    def test_settings_save_queues_mapping_sync(self):
        with (
            patch.object(
                bazarr_routes.BazarrConnectionStore,
                "save",
                return_value={"configured": True},
            ),
            patch("app.jobs.scheduler") as scheduler,
        ):
            result = bazarr_routes._save_settings({"enabled": True})
        self.assertEqual(result, {"configured": True})
        scheduler.enqueue_bazarr_sync.assert_called_once_with()

    def test_sync_maps_series_once_and_each_episode_exactly(self):
        client = _SyncBazarrClient(
            [{"path": "/tv/Show", "sonarrSeriesId": 9, "tvdbId": "123"}],
            [
                {
                    "seriesId": 9,
                    "path": "/tv/Show/Season 1/Show - S01E02.mkv",
                    "sonarrEpisodeId": 90,
                    "season": 1,
                    "episode": 2,
                    "subtitles": [{"language": "en", "provider": "embedded"}],
                }
            ],
        )
        with (
            patch(
                "app.bazarr.BazarrConnectionStore.internal",
                return_value={"address": "bazarr"},
            ),
            patch("app.bazarr.BazarrClient", return_value=client),
        ):
            result = BazarrSyncService(self.db).sync()
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["matched_series"], 1)
        self.assertEqual(client.series_calls, 1)
        self.assertEqual(client.episode_calls, [9])
        self.assertTrue(client.closed)
        self.assertEqual(
            self.db.execute(
                "SELECT bazarr_series_id,state FROM bazarr_series_mappings"
            ),
            [(9, "matched")],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT bazarr_episode_id,state FROM bazarr_episode_mappings"
            ),
            [(90, "matched")],
        )

    def test_episode_inventory_failure_preserves_existing_mapping(self):
        self._insert_mapping()
        client = _SyncBazarrClient(
            [{"path": "/tv/Show", "sonarrSeriesId": 9, "tvdbId": "123"}],
            episode_error=BazarrError("temporary failure"),
        )
        with (
            patch(
                "app.bazarr.BazarrConnectionStore.internal",
                return_value={"address": "bazarr"},
            ),
            patch("app.bazarr.BazarrClient", return_value=client),
        ):
            result = BazarrSyncService(self.db).sync()
        self.assertEqual(result["deferred_series"], 1)
        self.assertEqual(
            self.db.execute(
                "SELECT bazarr_episode_id,state FROM bazarr_episode_mappings"
            ),
            [(90, "matched")],
        )


if __name__ == "__main__":
    unittest.main()
