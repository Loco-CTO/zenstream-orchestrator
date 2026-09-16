import tempfile
import unittest
from pathlib import Path

from app.artwork_variants import (
    ArtworkVariantCache,
    ArtworkVariantSource,
    selected_sources,
)
from app.database import DatabaseHandler


class ArtworkVariantCacheTest(unittest.TestCase):
    def test_cache_is_source_versioned_and_prunes_only_invalid_or_obsolete_files(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = ArtworkVariantCache(str(root / "orchestrator.db"))
            source_path = root / "poster.webp"
            source_path.write_bytes(b"source")
            current = ArtworkVariantSource(source_path, "revision-1")
            obsolete = ArtworkVariantSource(source_path, "revision-0")

            current_target = cache.path_for(current, 160)
            obsolete_target = cache.path_for(obsolete, 160)
            incomplete_target = cache.path_for(current, 320)
            assert current_target is not None
            assert obsolete_target is not None
            assert incomplete_target is not None
            current_target.parent.mkdir(parents=True, exist_ok=True)
            obsolete_target.parent.mkdir(parents=True, exist_ok=True)
            incomplete_target.parent.mkdir(parents=True, exist_ok=True)
            current_target.write_bytes(b"current")
            obsolete_target.write_bytes(b"obsolete")
            incomplete_target.write_bytes(b"")

            removed = cache.prune_stale([current])

            self.assertEqual(removed, 2)
            self.assertTrue(current_target.is_file())
            self.assertFalse(obsolete_target.exists())
            self.assertFalse(incomplete_target.exists())
            self.assertNotEqual(
                cache.path_for(current, 160), cache.path_for(obsolete, 160)
            )

    def test_selection_snapshot_reports_incomplete_when_the_canonical_table_is_missing(
        self,
    ):
        database = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            sources, complete = selected_sources(database)
        finally:
            database.close()

        self.assertEqual(sources, [])
        self.assertFalse(complete)


if __name__ == "__main__":
    unittest.main()
