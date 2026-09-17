import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path

from app.database import DatabaseHandler
from app.library_cleanup import (
    _CLEANUP_BATCH_SIZE,
    _remove_cached_files,
    _sweep_metadata_cache_files,
    cleanup_orphans,
)


class LibraryCleanupTest(unittest.TestCase):
    @staticmethod
    def _database(root: Path) -> DatabaseHandler:
        return DatabaseHandler("sqlite", {}, str(root / "orchestrator.db"))

    @staticmethod
    def _reference_query_counter(db):
        original_execute = db.execute
        queries = []

        def execute(query, params=None):
            if "SELECT local_path FROM metadata_images WHERE local_path IN" in str(
                query
            ):
                queries.append((query, params))
            return original_execute(query, params)

        db.execute = execute
        return queries

    def test_cached_artwork_checks_are_batched_and_preserve_references(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = self._database(root)
            try:
                db.execute("CREATE TABLE metadata_images(local_path TEXT)")
                db.execute("CREATE TABLE catalog_artwork_selection(local_path TEXT)")
                image_root = root / "metadata-cache" / "images"
                image_root.mkdir(parents=True)
                metadata_path = image_root / "metadata.webp"
                selected_path = image_root / "selected.webp"
                orphan_path = image_root / "orphan.webp"
                metadata_path.touch()
                selected_path.touch()
                orphan_path.touch()
                db.execute(
                    "INSERT INTO metadata_images VALUES(?)", (str(metadata_path),)
                )
                db.execute("INSERT INTO metadata_images VALUES(?)", (str(orphan_path),))
                db.execute(
                    "INSERT INTO catalog_artwork_selection VALUES(?)",
                    (str(selected_path),),
                )
                db.execute(
                    "DELETE FROM metadata_images WHERE local_path=?",
                    (str(orphan_path),),
                )

                paths = {
                    str(image_root / f"candidate-{index}.webp")
                    for index in range(_CLEANUP_BATCH_SIZE * 3 + 1)
                }
                paths.update({str(metadata_path), str(selected_path), str(orphan_path)})
                queries = self._reference_query_counter(db)

                self.assertTrue(
                    _remove_cached_files(
                        db,
                        {"metadata_images", "catalog_artwork_selection"},
                        paths,
                    )
                )

                expected_batches = (
                    len(paths) + _CLEANUP_BATCH_SIZE - 1
                ) // _CLEANUP_BATCH_SIZE
                self.assertEqual(len(queries), expected_batches)
                self.assertTrue(metadata_path.exists())
                self.assertTrue(selected_path.exists())
                self.assertFalse(orphan_path.exists())
            finally:
                db.close()

    def test_metadata_cache_sweep_batches_checks_and_keeps_selected_artwork(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = self._database(root)
            try:
                db.execute("CREATE TABLE metadata_images(local_path TEXT)")
                db.execute("CREATE TABLE catalog_artwork_selection(local_path TEXT)")
                image_root = root / "metadata-cache" / "images"
                image_root.mkdir(parents=True)
                old = time.time() - 3600
                paths = []
                for index in range(_CLEANUP_BATCH_SIZE * 2 + 1):
                    path = image_root / f"sweep-{index}.webp"
                    path.touch()
                    os.utime(path, (old, old))
                    paths.append(path)
                metadata_path = paths[0]
                selected_path = paths[1]
                db.execute(
                    "INSERT INTO metadata_images VALUES(?)", (str(metadata_path),)
                )
                db.execute(
                    "INSERT INTO catalog_artwork_selection VALUES(?)",
                    (str(selected_path),),
                )
                queries = self._reference_query_counter(db)

                self.assertTrue(
                    _sweep_metadata_cache_files(
                        db,
                        {"metadata_images", "catalog_artwork_selection"},
                        grace_seconds=0,
                    )
                )

                expected_batches = (
                    len(paths) + _CLEANUP_BATCH_SIZE - 1
                ) // _CLEANUP_BATCH_SIZE
                self.assertEqual(len(queries), expected_batches)
                self.assertTrue(metadata_path.exists())
                self.assertTrue(selected_path.exists())
                self.assertFalse(paths[-1].exists())
            finally:
                db.close()

    def test_cached_artwork_cleanup_can_resume_after_batch_termination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = self._database(root)
            try:
                db.execute("CREATE TABLE metadata_images(local_path TEXT)")
                image_root = root / "metadata-cache" / "images"
                image_root.mkdir(parents=True)
                paths = {
                    str(image_root / f"resume-{index}.webp")
                    for index in range(_CLEANUP_BATCH_SIZE * 2 + 1)
                }
                for path in paths:
                    Path(path).touch()
                calls = 0

                def should_terminate():
                    nonlocal calls
                    calls += 1
                    return calls > 1

                self.assertFalse(
                    _remove_cached_files(
                        db,
                        {"metadata_images"},
                        paths,
                        should_terminate=should_terminate,
                    )
                )
                self.assertTrue(Path(next(iter(paths))).exists())
            finally:
                db.close()

    def test_cleanup_preserves_local_artwork_referenced_by_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = self._database(root)
            try:
                db.execute(
                    "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT)"
                )
                db.execute(
                    "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,identifier_type TEXT,provider_id TEXT)"
                )
                db.execute(
                    "CREATE TABLE media_files(id TEXT,entity_id TEXT,relative_path TEXT,role TEXT,quick_fingerprint TEXT)"
                )
                db.execute(
                    "CREATE TABLE metadata_images(provider TEXT,entity_type TEXT,provider_id TEXT,local_path TEXT)"
                )
                db.execute(
                    "CREATE TABLE catalog_artwork_selection(entity_id TEXT,local_path TEXT,provider TEXT)"
                )
                local_root = root / "image-cache" / "local"
                local_root.mkdir(parents=True)
                source_hash = hashlib.sha256(b"source").hexdigest()
                retained = local_root / f"{source_hash}.webp"
                orphaned = local_root / f"{'f' * 64}.webp"
                retained.touch()
                orphaned.touch()
                db.execute(
                    "INSERT INTO library_entities VALUES('entity-1','library-1',NULL,'movie')"
                )
                db.execute(
                    "INSERT INTO media_files VALUES('image-1','entity-1','poster.jpg','image',?)",
                    (source_hash,),
                )
                db.execute(
                    "INSERT INTO catalog_artwork_selection VALUES(?,?,?)",
                    ("entity-1", str(retained), "local"),
                )

                self.assertTrue(cleanup_orphans(db))
                self.assertTrue(retained.exists())
                self.assertFalse(orphaned.exists())
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
