import tempfile
import unittest

from app.database import DatabaseHandler
from app.metadata_services import PersonCreditIngestService


class PersonCreditIngestTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute(
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY, entity_type TEXT)"
        )
        self.db.execute(
            "CREATE TABLE entity_provider_ids(entity_id TEXT, provider TEXT, provider_id TEXT, is_primary INTEGER, FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE)"
        )
        self.db.execute(
            "CREATE TABLE people(id TEXT PRIMARY KEY, provider TEXT, provider_person_id TEXT, image_url TEXT, local_path TEXT, image_blur_hash TEXT, created_at TEXT, updated_at TEXT, UNIQUE(provider,provider_person_id))"
        )
        self.db.execute(
            "CREATE TABLE person_localizations(person_id TEXT, locale TEXT, name TEXT, updated_at TEXT, PRIMARY KEY(person_id,locale), FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE CASCADE)"
        )
        self.db.execute(
            "CREATE TABLE entity_person_credits(id TEXT PRIMARY KEY, entity_id TEXT, person_id TEXT, provider TEXT, locale TEXT, credit_type TEXT, role TEXT, department TEXT, credit_order INTEGER, FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE, FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE CASCADE)"
        )
        for entity_id, primary in (
            ("movie-1", 1),
            ("movie-2", 1),
            ("movie-secondary", 0),
        ):
            self.db.execute(
                "INSERT INTO library_entities VALUES(?, 'movie')", (entity_id,)
            )
            self.db.execute(
                "INSERT INTO entity_provider_ids VALUES(?, 'tmdb', '99', ?)",
                (entity_id, primary),
            )

    def tearDown(self):
        self.db.close()

    def test_primary_provider_credits_reuse_people_and_replace_item_rows(self):
        cache = type("Cache", (), {"db": self.db})()
        with tempfile.TemporaryDirectory() as directory:
            service = PersonCreditIngestService(cache, image_root=directory)
            document = {
                "credits": {
                    "cast": [
                        {"id": "actor-1", "name": "Actor", "role": "Hero", "order": 2}
                    ],
                    "crew": [
                        {
                            "id": "director-1",
                            "name": "Director",
                            "role": "Director",
                            "department": "Directing",
                            "order": 0,
                        }
                    ],
                }
            }
            service.ingest("tmdb", "movie", "99", "en", document)
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM people")[0][0], 2)
            self.assertEqual(
                self.db.execute(
                    "SELECT COUNT(*) FROM entity_person_credits WHERE entity_id='movie-1'"
                )[0][0],
                2,
            )
            self.assertEqual(
                self.db.execute(
                    "SELECT COUNT(*) FROM entity_person_credits WHERE entity_id='movie-secondary'"
                )[0][0],
                0,
            )

            self.db.execute(
                "UPDATE entity_provider_ids SET provider_id='100' WHERE entity_id='movie-1'"
            )
            service.ingest("tmdb", "movie", "99", "en", document)
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM people")[0][0], 2)
            self.assertEqual(
                self.db.execute(
                    "SELECT COUNT(*) FROM entity_person_credits WHERE entity_id='movie-2'"
                )[0][0],
                2,
            )
            self.assertEqual(self.db.execute("PRAGMA foreign_key_check"), [])


if __name__ == "__main__":
    unittest.main()
