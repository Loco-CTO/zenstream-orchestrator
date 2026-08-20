import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.calendar import (
    ArrCalendarClient,
    CalendarEventValue,
    CalendarFutureMetadataJob,
    CalendarFutureMetadataService,
    CalendarReadService,
    CalendarSyncService,
    _normalize_sonarr,
    calendar_window,
    parse_calendar_window,
)
from app.models.calendar import FutureMetadataCache


class CalendarWindowTest(unittest.TestCase):
    def test_window_is_one_week_back_and_sixteen_weeks_forward(self):
        start, end = calendar_window(datetime(2026, 8, 20, 12, tzinfo=timezone.utc))

        self.assertEqual(start, datetime(2026, 8, 9, tzinfo=timezone.utc))
        self.assertEqual(
            end,
            datetime(2026, 12, 12, 23, 59, 59, 999999, tzinfo=timezone.utc),
        )

    def test_default_api_range_accepts_the_provider_window(self):
        start, end = parse_calendar_window(None, None)

        self.assertLessEqual(end - start, timedelta(days=130))


class CalendarProviderTest(unittest.TestCase):
    def test_provider_file_flag_is_not_calendar_presence(self):
        event = _normalize_sonarr(
            {
                "id": 1,
                "tvdbId": 2,
                "airDateUtc": "2026-08-20T12:00:00Z",
                "hasFile": True,
            },
            "library",
        )

        self.assertIsNotNone(event)
        self.assertFalse(hasattr(event, "has_file"))

    def test_fetch_sends_the_inclusive_provider_dates(self):
        response = MagicMock(status_code=200)
        response.json.return_value = []
        client = MagicMock()
        client.get.return_value = response
        http_client = MagicMock()
        http_client.return_value.__enter__.return_value = client
        connection = {
            "provider": "sonarr",
            "address": "sonarr.local",
            "port": 8989,
            "baseUrl": "",
            "useSsl": False,
            "apiKey": "secret",
        }

        with patch("app.calendar.httpx.Client", http_client):
            self.assertEqual(
                ArrCalendarClient(connection).fetch(
                    datetime(2026, 8, 9, tzinfo=timezone.utc),
                    datetime(2026, 12, 12, 23, 59, 59, 999999, tzinfo=timezone.utc),
                ),
                [],
            )

        params = client.get.call_args.kwargs["params"]
        self.assertEqual(params["start"], "2026-08-09")
        self.assertEqual(params["end"], "2026-12-12")


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

    def test_out_of_window_provider_events_are_not_persisted(self):
        service = CalendarSyncService.__new__(CalendarSyncService)
        service.db = MagicMock()
        service._upsert = MagicMock(return_value=False)
        inside = self.event()
        outside = replace(
            inside,
            source_event_id="outside",
            event_at="2026-12-13T12:00:00+00:00",
            event_date="2026-12-13",
        )
        seen_at = "2026-08-20T00:00:00+00:00"

        with patch(
            "app.calendar.normalize_calendar_events",
            return_value=[inside, outside],
        ):
            result = service._replace_provider_events(
                "sonarr",
                "library",
                [],
                seen_at,
                datetime(2026, 8, 9, tzinfo=timezone.utc),
                datetime(2026, 12, 12, 23, 59, 59, 999999, tzinfo=timezone.utc),
            )

        self.assertEqual(result, (1, 0))
        service._upsert.assert_called_once_with(inside, seen_at)

    def test_unlinked_events_collect_episode_and_series_metadata_targets(self):
        service = CalendarSyncService.__new__(CalendarSyncService)
        service.db = MagicMock()
        service._upsert = MagicMock(return_value=False)
        targets = set()

        with patch(
            "app.calendar.normalize_calendar_events",
            return_value=[self.event()],
        ):
            service._replace_provider_events(
                "sonarr",
                "library",
                [],
                "2026-08-20T00:00:00+00:00",
                datetime(2026, 8, 9, tzinfo=timezone.utc),
                datetime(2026, 12, 12, 23, 59, 59, 999999, tzinfo=timezone.utc),
                targets,
            )

        self.assertEqual(
            targets,
            {
                ("tvdb", "episode", "episode-tvdb"),
                ("tvdb", "series", "series-tvdb"),
            },
        )

    def test_sync_fetches_unlinked_metadata_immediately(self):
        service = CalendarSyncService.__new__(CalendarSyncService)
        service.connections = MagicMock()
        service.connections.internal.return_value = [
            {"provider": "sonarr", "libraryId": "library"}
        ]
        event = self.event()

        def replace_events(*args):
            args[-1].update(
                {
                    ("tvdb", "episode", event.tvdb_id),
                    ("tvdb", "series", event.series_tvdb_id),
                }
            )
            return 1, 0

        service._replace_provider_events = MagicMock(side_effect=replace_events)
        metadata_result = {"targets": 2, "updated": 2, "errors": []}

        with (
            patch("app.calendar.ArrCalendarClient") as client_type,
            patch("app.calendar.CalendarFutureMetadataService") as future_type,
        ):
            client_type.return_value.fetch.return_value = []
            future_type.return_value.refetch.return_value = metadata_result

            result = service.sync()

        future_type.return_value.refetch.assert_called_once()
        kwargs = future_type.return_value.refetch.call_args.kwargs
        self.assertFalse(kwargs["force"])
        self.assertEqual(
            set(kwargs["targets"]),
            {
                ("tvdb", "episode", "episode-tvdb"),
                ("tvdb", "series", "series-tvdb"),
            },
        )
        self.assertEqual(result["metadata"], metadata_result)


class CalendarReadTest(unittest.TestCase):
    def test_has_file_comes_from_granted_playable_catalog_media(self):
        service = CalendarReadService.__new__(CalendarReadService)
        service.db = MagicMock()
        service.db.execute.return_value = [
            (
                "event-1",
                "sonarr",
                "library",
                "Anime",
                "episode",
                "air",
                "2026-08-20T12:00:00+00:00",
                "2026-08-20",
                0,
                "episode-tvdb",
                None,
                "series-tvdb",
                1,
                2,
                1,
                1,
                "future",
                "episode-1",
                "episode",
                "season-1",
                "season",
                "series-1",
                "series",
            )
        ]
        service._title = MagicMock(return_value=None)
        with patch("app.calendar.AccountPreference") as preference:
            preference.return_value.metadata_language.return_value = {"language": "en"}
            result = service.list(
                "user",
                datetime(2026, 8, 20, tzinfo=timezone.utc),
                datetime(2026, 8, 21, tzinfo=timezone.utc),
            )

        query, params = service.db.execute.call_args.args
        self.assertIn("JOIN user_library_access access", query)
        self.assertIn("playable_media.role='media'", query)
        self.assertEqual(params[0], "user")
        self.assertTrue(result["events"][0]["hasFile"])

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

    def test_refetch_accepts_explicit_targets_without_scanning_calendar_events(self):
        service = CalendarFutureMetadataService.__new__(CalendarFutureMetadataService)
        service.db = MagicMock()
        service.cache = MagicMock()
        service.metadata = MagicMock()
        service._targets = MagicMock(side_effect=AssertionError("unexpected scan"))
        service.metadata.fetch_locales.return_value = {
            "en": {"title": "Episode title", "images": []}
        }

        with patch("app.calendar.MetadataLanguageSettings") as settings:
            settings.return_value.get.return_value = ["en"]
            result = service.refetch(
                force=False,
                targets=[("tvdb", "episode", "episode-1")],
            )

        self.assertEqual(result["targets"], 1)
        service.metadata.fetch_locales.assert_called_once_with(
            "tvdb",
            "episode",
            "episode-1",
            ["en"],
            force=False,
            project=False,
            cache=service.cache,
        )

    def test_future_cache_drops_artwork_references(self):
        cache = FutureMetadataCache.__new__(FutureMetadataCache)
        cache.db = MagicMock()

        cache.put(
            "tvdb",
            "episode",
            "episode-1",
            "en",
            {"title": "Episode title", "images": [{"url": "https://example/image"}]},
        )

        payload = cache.db.execute.call_args.args[1][4]
        self.assertNotIn("images", json.loads(payload))

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
