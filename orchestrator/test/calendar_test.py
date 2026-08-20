import unittest
from unittest.mock import MagicMock, patch

from app.calendar import (
    CalendarEventValue,
    CalendarFutureMetadataJob,
    CalendarFutureMetadataService,
    CalendarReadService,
    CalendarSyncService,
)


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

    def test_title_skips_provider_placeholder_before_english_fallback(self):
        class Cache:
            def get_locales(self, provider, entity_type, provider_id):
                return {
                    "ja": {"title": "TBA", "originalLanguage": "ja"},
                    "en": {"title": "Episode 16", "originalLanguage": "en"},
                }

        service = CalendarReadService.__new__(CalendarReadService)
        service.normal_cache = Cache()
        service.future_cache = Cache()
        with patch("app.calendar.MetadataLanguageSettings") as settings:
            settings.return_value.get.return_value = ["en", "ja"]
            self.assertEqual(
                service._title("tvdb", "episode", "episode-tvdb", "ja", False),
                "Episode 16",
            )


class CalendarFutureMetadataTest(unittest.TestCase):
    def test_target_window_includes_the_entire_boundary_date(self):
        service = CalendarFutureMetadataService.__new__(CalendarFutureMetadataService)
        service.db = MagicMock()
        service.db.execute.return_value = []
        with patch("app.calendar.calendar_window") as window:
            from datetime import datetime, timezone

            window.return_value = (
                datetime(2026, 8, 13, 18, 57, tzinfo=timezone.utc),
                datetime(2026, 11, 18, 18, 57, tzinfo=timezone.utc),
            )
            service._targets()

        query, params = service.db.execute.call_args.args
        self.assertIn("e.event_date>=?", query)
        self.assertEqual(params, ("2026-08-13", "2026-11-18"))

    def test_refetch_reports_each_identity(self):
        service = CalendarFutureMetadataService.__new__(CalendarFutureMetadataService)
        service.db = MagicMock()
        service.cache = MagicMock()
        service.metadata = MagicMock()
        service._ingest_images = MagicMock()
        service._targets = MagicMock(
            return_value=[
                ("tvdb", "episode", "episode-1"),
                ("tvdb", "series", "series-1"),
            ]
        )
        service.metadata.fetch_locales.return_value = {
            "en": {"title": "Episode title", "images": []}
        }
        progress = []

        with patch("app.calendar.MetadataLanguageSettings") as settings:
            settings.return_value.get.return_value = ["en"]
            result = service.refetch(
                progress=lambda current, total, item, failed: progress.append(
                    (current, total, item, failed)
                )
            )

        self.assertEqual(result["targets"], 2)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(progress[0], (0, 2, None, False))
        self.assertEqual(progress[-1][:2], (2, 2))
        self.assertEqual(len(progress), 3)

    def test_job_persists_progress_detail(self):
        class Store:
            def __init__(self):
                self.updates = []

            def update_run(self, run_id, **values):
                self.updates.append((run_id, values))

        class Service:
            def refetch(self, should_terminate, progress):
                progress(0, 2, None, False)
                progress(1, 2, "TVDB episode 1", False)
                progress(2, 2, "TVDB episode 2", True)
                return {"targets": 2, "updated": 1, "errors": ["failed"]}

        store = Store()
        with patch("app.calendar.CalendarFutureMetadataService", return_value=Service()):
            CalendarFutureMetadataJob(store).run("run-1", lambda: False)

        self.assertEqual(len(store.updates), 4)
        self.assertEqual(store.updates[1][1]["progress_stage_current"], 1)
        self.assertEqual(store.updates[1][1]["progress_stage_total"], 2)
        self.assertEqual(store.updates[2][1]["progress_current_item"], "TVDB episode 2")
        self.assertEqual(store.updates[-1][1]["state"], "completed")


if __name__ == "__main__":
    unittest.main()
