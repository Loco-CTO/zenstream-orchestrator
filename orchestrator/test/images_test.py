import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.database import DatabaseHandler
from app.images import LocalArtworkCache, encode_blurhash


class LocalArtworkCacheTest(unittest.TestCase):
    def test_encodes_a_standard_blurhash_from_rgb_pixels(self):
        value = encode_blurhash(bytes([255, 0, 0]) * 4, 2, 2)

        self.assertEqual(len(value), 28)
        self.assertTrue(value.startswith("L"))

    def test_materializes_by_content_hash_and_prunes_unreferenced_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = DatabaseHandler("sqlite", {}, str(root / "orchestrator.db"))
            try:
                db.execute("CREATE TABLE media_files(file_hash TEXT,role TEXT)")
                source = root / "poster.jpg"
                source.write_bytes(b"source")
                content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                db.execute("INSERT INTO media_files VALUES(?, 'image')", (content_hash,))
                cache = LocalArtworkCache(db)

                def encode(_source, target):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"webp")

                with patch("app.images.encode_webp", side_effect=encode) as encoder:
                    target = cache.materialize(source, content_hash)
                    self.assertEqual(target.suffix, ".webp")
                    self.assertTrue(target.is_file())
                    cache.materialize(source, content_hash)
                    encoder.assert_called_once()

                db.execute("DELETE FROM media_files")
                cache.prune()
                self.assertFalse(target.exists())
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
