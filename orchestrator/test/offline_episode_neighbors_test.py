from app.offline_bundle import episode_neighbors
from test.playlists_test import PlaylistServiceTest


class OfflineEpisodeNeighborsTest(PlaylistServiceTest):
    def test_neighbors_include_unselected_middle_episodes(self):
        for item in ("episode-1", "episode-2"):
            self.db.execute("INSERT INTO media_files VALUES(?,'media')", (item,))
        self.assertEqual(
            episode_neighbors(self.catalog, "owner", "episode-1"),
            {"previous": None, "next": "episode-2"},
        )

    def test_double_episodes_skip_covered_numbers_and_cross_seasons(self):
        self.db.execute(
            "UPDATE library_entities SET episode_end_number=2 WHERE id='episode-1'"
        )
        self._entity("third", "episode", "series", season_number=1, episode_number=3)
        self._entity("second-season", "season", "series", season_number=2)
        self._entity(
            "next-season", "episode", "second-season", season_number=2, episode_number=1
        )
        for item in ("episode-1", "episode-2", "third", "next-season"):
            self.db.execute("INSERT INTO media_files VALUES(?,'media')", (item,))
        self.assertEqual(
            episode_neighbors(self.catalog, "owner", "episode-1")["next"], "third"
        )
        self.db.execute("DELETE FROM media_files WHERE entity_id='episode-2'")
        self.assertEqual(
            episode_neighbors(self.catalog, "owner", "third"),
            {"previous": "episode-1", "next": "next-season"},
        )

    def test_neighbors_exclude_inaccessible_and_unplayable_items(self):
        self.db.execute("INSERT INTO media_files VALUES('episode-1','media')")
        self.db.execute(
            "UPDATE library_entities SET library_id='hidden' WHERE id='episode-2'"
        )
        self.db.execute("INSERT INTO media_files VALUES('episode-2','media')")
        self.assertIsNone(episode_neighbors(self.catalog, "owner", "episode-1")["next"])
