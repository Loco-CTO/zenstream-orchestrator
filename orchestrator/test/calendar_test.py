import unittest
from unittest.mock import MagicMock, patch

from app.calendar import CalendarEventValue, CalendarReadService, CalendarSyncService


class CalendarSyncTest(unittest.TestCase):
    @staticmethod
    def event():
        return CalendarEventValue(
            provider="sonarr",
            library_id="library",
            source_event_id="sonarr-episode",
            kind="episode",
            release_type="air",
            event_at="2026-08-20T12:00:00+00:00",
            event_date="2026-08-20",
            all_day=False,
            tvdb_id="episode-tvdb",
            series_tvdb_id="series-tvdb",
            season_number=1,
            episode_number=2,
        )

    @staticmethod
    def service(matches):
        service = CalendarSyncService.__new__(CalendarSyncService)
        service.db = MagicMock()
        service.future_cache = MagicMock()
        service._matching_entities = MagicMock(return_value=matches)
        return service

    def test_series_match_alone_stays_future(self):
        service = self.service([("series-id", "series")])

        self.assertFalse(service._upsert(self.event(), "2026-08-20T00:00:00+00:00"))
        update = service.db.execute.call_args_list[-1]
        self.assertEqual(update.args[1][0], "future")
        service.future_cache.promote_identity.assert_not_called()

    def test_exact_episode_match_is_existing_and_only_episode_is_linked(self):
        service = self.service(
            [("series-id", "series"), ("episode-id", "episode")]
        )
        cursor = service.db.transaction.return_value.__enter__.return_value

        self.assertTrue(service._upsert(self.event(), "2026-08-20T00:00:00+00:00"))
        linked = cursor.executemany.call_args.args[1]
        self.assertEqual(linked, [(linked[0][0], "episode-id")])
        update = service.db.execute.call_args_list[-1]
        self.assertEqual(update.args[1][0], "existing")


class CalendarReadTest(unittest.TestCase):
    def test_series_only_link_does_not_become_episode_catalog_item(self):
        self.assertEqual(
            CalendarReadService._linked_item(
                [{"entityId": "series-id", "entityType": "series"}],
                "episode",
            ),
            (None, None),
        )

    def test_title_falls_back_to_configured_english(self):
        class Cache:
            def get_locales(self, provider, entity_type, provider_id):
                return {
                    "en": {
                        "title": "English episode title",
                        "originalLanguage": "en",
                    }
                }

        service = CalendarReadService.__new__(CalendarReadService)
        service.normal_cache = Cache()
        service.future_cache = Cache()
        with patch("app.calendar.MetadataLanguageSettings") as settings:
            settings.return_value.get.return_value = ["en", "ja"]
            self.assertEqual(
                service._title("tvdb", "episode", "episode-tvdb", "ja", False),
                "English episode title",
            )


if __name__ == "__main__":
    unittest.main()
