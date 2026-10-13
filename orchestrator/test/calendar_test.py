import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
from app.calendar import (
    ArrCalendarClient,
    CalendarConnectionStore,
    CalendarEventValue,
    CalendarFutureMetadataJob,
    CalendarFutureMetadataService,
    CalendarReadService,
    CalendarSyncService,
    _normalize_address,
    _normalize_base_url,
    _normalize_radarr,
    _normalize_sonarr,
    _parse_port,
    calendar_window,
    normalize_calendar_events,
    parse_calendar_window,
)
from app.database import DatabaseHandler
from app.models.calendar import FutureMetadataCache
from app.providers import ProviderError


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

    def test_sonarr_episode_without_tvdb_id_uses_nested_series_identity(self):
        """Use the series TVDB identity when Sonarr omits an episode identity."""
        event = _normalize_sonarr(
            {
                "id": 52921,
                "tvdbId": 0,
                "series": {"tvdbId": 429934},
                "airDateUtc": "2026-10-03T23:00:00Z",
                "seasonNumber": 2,
                "episodeNumber": 4,
            },
            "library",
        )

        self.assertIsNotNone(event)
        self.assertIsNone(event.tvdb_id)
        self.assertEqual(event.series_tvdb_id, "429934")
        self.assertEqual(event.source_event_id, "52921")

    def test_sonarr_episode_without_episode_or_series_identity_is_skipped(self):
        """Skip Sonarr events without either episode or series identity."""
        event = _normalize_sonarr(
            {
                "id": 52921,
                "tvdbId": 0,
                "series": {"tvdbId": 0},
                "airDateUtc": "2026-10-03T23:00:00Z",
            },
            "library",
        )

        self.assertIsNone(event)

    def test_sonarr_invalid_numbering_is_omitted(self):
        """Keep a valid event when provider numbering is malformed."""
        event = _normalize_sonarr(
            {
                "id": 52921,
                "tvdbId": 429934,
                "airDateUtc": "2026-10-03T23:00:00Z",
                "seasonNumber": "special",
                "episodeNumber": "4",
            },
            "library",
        )

        self.assertIsNotNone(event)
        self.assertIsNone(event.season_number)
        self.assertIsNone(event.episode_number)

    def test_radarr_normalizes_multiple_dates_and_fallback_release(self):
        """Normalize provider dates and use the generic release fallback."""
        events = _normalize_radarr(
            {
                "id": 91,
                "tmdbID": 777,
                "inCinemas": "not-a-date",
                "digitalRelease": "2026-10-15T12:00:00Z",
                "physicalRelease": None,
                "monitored": False,
            },
            "movies",
        )
        fallback = _normalize_radarr(
            {"id": 92, "tmdbId": 778, "releaseDate": "2026-11-01"},
            "movies",
        )

        self.assertEqual([event.release_type for event in events], ["digital"])
        self.assertFalse(events[0].monitored)
        self.assertEqual(fallback[0].release_type, "release")
        self.assertEqual(fallback[0].tmdb_id, "778")

    def test_normalize_events_dispatches_both_providers_and_skips_bad_ids(self):
        """Normalize Sonarr and Radarr records while ignoring invalid IDs."""
        sonarr = normalize_calendar_events(
            "sonarr",
            "tv",
            [
                {"id": 1, "tvdbId": 101, "airDate": "2026-10-03"},
                {"id": 2, "tvdbId": 0, "airDate": "2026-10-03"},
            ],
        )
        radarr = normalize_calendar_events(
            "radarr",
            "movies",
            [
                {"id": 1, "tmdbId": 201, "releaseDate": "2026-10-03"},
                {"id": None, "tmdbId": 202, "releaseDate": "2026-10-03"},
            ],
        )

        self.assertEqual(len(sonarr), 1)
        self.assertEqual(len(radarr), 1)

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

    def test_sonarr_v5_uses_only_selected_endpoint_and_v5_subresources(self):
        """Call the selected v5 endpoint with its supported subresource query."""
        response = MagicMock(status_code=200)
        response.json.return_value = []
        client = MagicMock()
        client.get.return_value = response
        http_client = MagicMock()
        http_client.return_value.__enter__.return_value = client
        connection = {
            "provider": "sonarr",
            "apiVersion": "v5",
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

        client.get.assert_called_once()
        self.assertEqual(
            client.get.call_args.args[0],
            "http://sonarr.local:8989/api/v5/calendar",
        )
        params = client.get.call_args.kwargs["params"]
        self.assertEqual(params["includeUnmonitored"], "false")
        self.assertEqual(params["includeSubresources"], ["series", "episodeFile"])
        self.assertNotIn("includeSeries", params)

    def test_v3_and_radarr_use_their_selected_query_parameters(self):
        """Send only the selected endpoint and query parameters."""
        for connection, expected_endpoint, expected_params in (
            (
                {
                    "provider": "sonarr",
                    "apiVersion": "v3",
                    "address": "sonarr.local",
                    "port": 8989,
                    "baseUrl": "/sonarr",
                    "useSsl": True,
                    "apiKey": "secret",
                },
                "https://sonarr.local:8989/sonarr/api/v3/calendar",
                {
                    "unmonitored": "false",
                    "includeSeries": "true",
                    "includeEpisodeFile": "true",
                    "includeEpisodeImages": "false",
                },
            ),
            (
                {
                    "provider": "radarr",
                    "address": "radarr.local",
                    "port": 7878,
                    "baseUrl": "",
                    "useSsl": False,
                    "apiKey": "secret",
                },
                "http://radarr.local:7878/api/v3/calendar",
                {"unmonitored": "false", "includeMovieFile": "true"},
            ),
        ):
            with self.subTest(provider=connection["provider"]):
                response = MagicMock(status_code=200)
                response.json.return_value = {"data": [{"id": 1}, "ignored"]}
                client = MagicMock()
                client.get.return_value = response
                http_client = MagicMock()
                http_client.return_value.__enter__.return_value = client

                with patch("app.calendar.httpx.Client", http_client):
                    result = ArrCalendarClient(connection).fetch(
                        datetime(2026, 10, 1, tzinfo=timezone.utc),
                        datetime(2026, 10, 31, tzinfo=timezone.utc),
                    )

                self.assertEqual(result, [{"id": 1}])
                self.assertEqual(client.get.call_args.args[0], expected_endpoint)
                params = client.get.call_args.kwargs["params"]
                for key, value in expected_params.items():
                    self.assertEqual(params[key], value)

    def test_selected_sonarr_endpoint_is_not_retried_as_another_version(self):
        """Report a missing configured endpoint without probing another version."""
        response = MagicMock(status_code=404)
        client = MagicMock()
        client.get.return_value = response
        http_client = MagicMock()
        http_client.return_value.__enter__.return_value = client
        connection = {
            "provider": "sonarr",
            "apiVersion": "v5",
            "address": "sonarr.local",
            "port": 8989,
            "baseUrl": "",
            "useSsl": False,
            "apiKey": "secret",
        }

        with (
            patch("app.calendar.httpx.Client", http_client),
            self.assertRaisesRegex(ProviderError, "endpoint was not found"),
        ):
            ArrCalendarClient(connection).fetch(
                datetime(2026, 10, 1, tzinfo=timezone.utc),
                datetime(2026, 10, 31, tzinfo=timezone.utc),
            )

        client.get.assert_called_once()

    def test_transient_response_retries_same_endpoint(self):
        """Retry a transient response without switching the configured version."""
        response = MagicMock(status_code=200)
        response.json.return_value = []
        client = MagicMock()
        client.get.side_effect = [MagicMock(status_code=503), response]
        http_client = MagicMock()
        http_client.return_value.__enter__.return_value = client
        connection = {
            "provider": "sonarr",
            "apiVersion": "v5",
            "address": "sonarr.local",
            "port": 8989,
            "baseUrl": "",
            "useSsl": False,
            "apiKey": "secret",
        }

        with (
            patch("app.calendar.httpx.Client", http_client),
            patch("app.calendar.time.sleep"),
        ):
            self.assertEqual(
                ArrCalendarClient(connection).fetch(
                    datetime(2026, 10, 1, tzinfo=timezone.utc),
                    datetime(2026, 10, 31, tzinfo=timezone.utc),
                ),
                [],
            )

        self.assertEqual(client.get.call_count, 2)
        self.assertTrue(
            all(
                call.args[0] == "http://sonarr.local:8989/api/v5/calendar"
                for call in client.get.call_args_list
            )
        )

    def test_transport_error_retries_and_exhaustion_becomes_provider_error(self):
        """Retry transport errors and report the final failed request."""
        response = MagicMock(status_code=200)
        response.json.return_value = []
        connection = {
            "provider": "sonarr",
            "apiVersion": "v5",
            "address": "sonarr.local",
            "port": 8989,
            "baseUrl": "",
            "useSsl": False,
            "apiKey": "secret",
        }
        client = MagicMock()
        client.get.side_effect = [httpx.ConnectError("offline"), response]
        http_client = MagicMock()
        http_client.return_value.__enter__.return_value = client

        with (
            patch("app.calendar.httpx.Client", http_client),
            patch("app.calendar.time.sleep"),
        ):
            self.assertEqual(
                ArrCalendarClient(connection).fetch(
                    datetime(2026, 10, 1, tzinfo=timezone.utc),
                    datetime(2026, 10, 31, tzinfo=timezone.utc),
                ),
                [],
            )

        self.assertEqual(client.get.call_count, 2)

        client.get.side_effect = [httpx.ConnectError("offline") for _ in range(3)]
        with (
            patch("app.calendar.httpx.Client", http_client),
            patch("app.calendar.time.sleep"),
            self.assertRaisesRegex(ProviderError, "ConnectError"),
        ):
            ArrCalendarClient(connection).fetch(
                datetime(2026, 10, 1, tzinfo=timezone.utc),
                datetime(2026, 10, 31, tzinfo=timezone.utc),
            )

        self.assertEqual(client.get.call_count, 5)

    def test_invalid_response_payload_is_rejected(self):
        """Reject provider payloads without a supported calendar envelope."""
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": "not-an-array"}
        client = MagicMock()
        client.get.return_value = response
        http_client = MagicMock()
        http_client.return_value.__enter__.return_value = client
        connection = {
            "provider": "sonarr",
            "apiVersion": "v5",
            "address": "sonarr.local",
            "port": 8989,
            "baseUrl": "",
            "useSsl": False,
            "apiKey": "secret",
        }

        with (
            patch("app.calendar.httpx.Client", http_client),
            self.assertRaisesRegex(ProviderError, "invalid response"),
        ):
            ArrCalendarClient(connection).fetch(
                datetime(2026, 10, 1, tzinfo=timezone.utc),
                datetime(2026, 10, 31, tzinfo=timezone.utc),
            )

    def test_http_response_errors_are_wrapped_as_provider_errors(self):
        """Wrap HTTP status failures in the provider-facing error type."""
        response = MagicMock(status_code=500)
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "provider failed", request=MagicMock(), response=MagicMock()
        )
        client = MagicMock()
        client.get.return_value = response
        http_client = MagicMock()
        http_client.return_value.__enter__.return_value = client
        connection = {
            "provider": "sonarr",
            "apiVersion": "v5",
            "address": "sonarr.local",
            "port": 8989,
            "baseUrl": "",
            "useSsl": False,
            "apiKey": "secret",
        }

        with (
            patch("app.calendar.httpx.Client", http_client),
            self.assertRaisesRegex(ProviderError, "HTTPStatusError"),
        ):
            ArrCalendarClient(connection).fetch(
                datetime(2026, 10, 1, tzinfo=timezone.utc),
                datetime(2026, 10, 31, tzinfo=timezone.utc),
            )


class CalendarConnectionStoreTest(unittest.TestCase):
    """Tests for persisting selected calendar provider versions."""

    def test_save_persists_selected_sonarr_api_version_and_keeps_saved_key(self):
        """Save the selected version while retaining an encrypted saved key."""
        database = MagicMock()
        database.execute.side_effect = [
            [("tv_series",)],
            [("encrypted-api-key", "v3")],
            None,
        ]
        with patch("app.calendar.Config") as config:
            config.return_value.database = database
            store = CalendarConnectionStore()

        with patch.object(
            store, "public", return_value={"sonarr": {"apiVersion": "v5"}}
        ):
            result = store.save(
                "sonarr",
                {
                    "address": "localhost",
                    "port": 8989,
                    "useSsl": False,
                    "libraryId": "library",
                    "apiVersion": "v5",
                },
            )

        insert = database.execute.call_args_list[2]
        self.assertIn("api_version", insert.args[0])
        self.assertEqual(insert.args[1][1], "v5")
        self.assertEqual(insert.args[1][7], "encrypted-api-key")
        self.assertEqual(result["apiVersion"], "v5")

    def test_save_rejects_unsupported_sonarr_api_version(self):
        """Reject unsupported Sonarr versions before persisting the connection."""
        database = MagicMock()
        database.execute.side_effect = [[("tv_series",)], []]
        with patch("app.calendar.Config") as config:
            config.return_value.database = database
            store = CalendarConnectionStore()

        with self.assertRaisesRegex(ValueError, "must be v3 or v5"):
            store.save(
                "sonarr",
                {
                    "address": "localhost",
                    "port": 8989,
                    "useSsl": False,
                    "libraryId": "library",
                    "apiVersion": "v4",
                    "apiKey": "secret",
                },
            )
        self.assertEqual(database.execute.call_count, 2)

    def test_internal_returns_selected_version_and_decrypted_key(self):
        """Expose the stored API version and decrypted key to the sync client."""
        database = MagicMock()
        database.execute.return_value = [
            ("sonarr", "v5", "localhost", 8989, "/", 0, "tv", "ciphertext")
        ]
        with patch("app.calendar.Config") as config:
            config.return_value.database = database
            store = CalendarConnectionStore()

        with patch("app.calendar.decrypt_calendar_api_key", return_value="secret"):
            result = store.internal()

        self.assertEqual(result[0]["apiVersion"], "v5")
        self.assertEqual(result[0]["apiKey"], "secret")

    def test_public_reports_configured_version_and_unconfigured_defaults(self):
        """Expose saved versions while defaulting missing providers to v3."""
        database = MagicMock()
        database.execute.side_effect = [
            [
                (
                    "sonarr",
                    "v5",
                    "localhost",
                    8989,
                    "/sonarr",
                    1,
                    "tv",
                    "ciphertext",
                    "validated-at",
                    "synced-at",
                    None,
                    "updated-at",
                    "TV",
                )
            ],
            [],
        ]
        with patch("app.calendar.Config") as config:
            config.return_value.database = database
            store = CalendarConnectionStore()

        result = store.public()

        self.assertEqual(result["sonarr"]["apiVersion"], "v5")
        self.assertTrue(result["sonarr"]["apiKeyConfigured"])
        self.assertEqual(result["sonarr"]["libraryName"], "TV")
        self.assertNotIn("apiKey", result["sonarr"])
        self.assertEqual(
            result["radarr"],
            {
                "provider": "radarr",
                "apiVersion": "v3",
                "configured": False,
                "apiKeyConfigured": False,
            },
        )

    def test_internal_skips_connections_with_undecryptable_keys(self):
        """Skip a provider whose stored API key can no longer be decrypted."""
        database = MagicMock()
        database.execute.return_value = [
            ("sonarr", "v5", "localhost", 8989, "/", 0, "tv", "ciphertext")
        ]
        with patch("app.calendar.Config") as config:
            config.return_value.database = database
            store = CalendarConnectionStore()

        with (
            patch(
                "app.calendar.decrypt_calendar_api_key",
                side_effect=ValueError("invalid ciphertext"),
            ),
            self.assertLogs("zenstream.calendar", level="ERROR"),
        ):
            self.assertEqual(store.internal(), [])

    def test_disabled_settings_clear_connection(self):
        """Clear a provider connection when the legacy disabled flag is false."""
        database = MagicMock()
        with patch("app.calendar.Config") as config:
            config.return_value.database = database
            store = CalendarConnectionStore()

        with (
            patch.object(store, "clear") as clear,
            patch.object(
                store, "public", return_value={"sonarr": {"configured": False}}
            ),
        ):
            result = store.save("sonarr", {"enabled": False})

        clear.assert_called_once_with("sonarr")
        self.assertFalse(result["configured"])

    def test_save_defaults_new_connections_to_v3_and_encrypts_key(self):
        """Persist a new connection with the default API version and key."""
        database = MagicMock()
        database.execute.side_effect = [[("tv_series",)], [], None]
        with patch("app.calendar.Config") as config:
            config.return_value.database = database
            store = CalendarConnectionStore()

        with (
            patch(
                "app.calendar.encrypt_calendar_api_key", return_value="encrypted"
            ) as encrypt,
            patch.object(
                store, "public", return_value={"sonarr": {"apiVersion": "v3"}}
            ),
        ):
            result = store.save(
                "sonarr",
                {
                    "address": "localhost",
                    "port": 8989,
                    "useSsl": False,
                    "libraryId": "tv",
                    "apiKey": "  secret  ",
                },
            )

        encrypt.assert_called_once_with("secret")
        self.assertEqual(database.execute.call_args_list[2].args[1][1], "v3")
        self.assertEqual(result["apiVersion"], "v3")

    def test_save_preserves_existing_version_when_legacy_client_omits_it(self):
        """Retain a saved version when an older dashboard omits the field."""
        database = MagicMock()
        database.execute.side_effect = [
            [("tv_series",)],
            [("encrypted", "v5")],
            None,
        ]
        with patch("app.calendar.Config") as config:
            config.return_value.database = database
            store = CalendarConnectionStore()

        with patch.object(
            store, "public", return_value={"sonarr": {"apiVersion": "v5"}}
        ):
            result = store.save(
                "sonarr",
                {
                    "address": "localhost",
                    "port": 8989,
                    "useSsl": False,
                    "libraryId": "tv",
                },
            )

        self.assertEqual(database.execute.call_args_list[2].args[1][1], "v5")
        self.assertEqual(result["apiVersion"], "v5")

    def test_save_rejects_invalid_provider_shape_and_ssl(self):
        """Reject invalid settings before they reach the persistence layer."""
        database = MagicMock()
        with patch("app.calendar.Config") as config:
            config.return_value.database = database
            store = CalendarConnectionStore()

        with self.assertRaisesRegex(ValueError, "Unsupported calendar provider"):
            store.save("other", {})
        with self.assertRaisesRegex(ValueError, "must be an object"):
            store.save("sonarr", [])
        with self.assertRaisesRegex(ValueError, "useSsl"):
            store.save(
                "sonarr",
                {"address": "localhost", "port": 8989, "useSsl": "false"},
            )
        database.execute.assert_not_called()

    def test_save_rejects_missing_library_and_wrong_provider_library_type(self):
        """Require a selected library of the provider's matching media type."""
        database = MagicMock()
        database.execute.side_effect = [[], [("movies",)]]
        with patch("app.calendar.Config") as config:
            config.return_value.database = database
            store = CalendarConnectionStore()

        base = {"address": "localhost", "port": 8989, "useSsl": False}
        with self.assertRaisesRegex(ValueError, "Choose a library"):
            store.save("sonarr", base)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            store.save("sonarr", {**base, "libraryId": "missing"})
        with self.assertRaisesRegex(ValueError, "must be mapped"):
            store.save("sonarr", {**base, "libraryId": "movies"})

    def test_save_rejects_radarr_v5_and_missing_new_api_key(self):
        """Keep Radarr on v3 and require a key when no saved key exists."""
        database = MagicMock()
        database.execute.side_effect = [[("movies",)], [], [("movies",)], []]
        with patch("app.calendar.Config") as config:
            config.return_value.database = database
            store = CalendarConnectionStore()

        base = {
            "address": "localhost",
            "port": 7878,
            "useSsl": False,
            "libraryId": "movies",
        }
        with self.assertRaisesRegex(ValueError, "Radarr API version must be v3"):
            store.save("radarr", {**base, "apiVersion": "v5", "apiKey": "secret"})
        with self.assertRaisesRegex(ValueError, "API key is required"):
            store.save("radarr", base)


class CalendarConnectionInputTest(unittest.TestCase):
    """Tests for validating administrator-supplied Arr connection fields."""

    def test_base_url_normalization_and_rejection(self):
        """Normalize paths and reject URLs, query strings, and fragments."""
        self.assertEqual(_normalize_base_url("  sonarr/api///  "), "/sonarr/api")
        self.assertEqual(_normalize_base_url("   "), "")
        self.assertEqual(_normalize_base_url("///"), "")
        for value in ("https://sonarr", "/api?key=value", "/api#calendar"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "must be a path"),
            ):
                _normalize_base_url(value)

    def test_address_validation(self):
        """Accept host and IP values while rejecting paths and query data."""
        self.assertEqual(_normalize_address(" localhost "), "localhost")
        self.assertEqual(_normalize_address("[::1]"), "[::1]")
        for value in (
            "",
            "host name",
            "https://host",
            "host/path",
            "host?query",
            "host#fragment",
            ":8989",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _normalize_address(value)
        with self.assertRaisesRegex(ValueError, "non-empty"):
            _normalize_address("a" * 256)

    def test_port_validation(self):
        """Accept valid TCP ports and reject nonnumeric or out-of-range values."""
        self.assertEqual(_parse_port("8989"), 8989)
        for value in (None, "port", 0, 65536):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "between 1 and 65535"),
            ):
                _parse_port(value)


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

    def test_constructor_and_event_id(self):
        """Build sync dependencies and generate stable event identifiers."""
        database = MagicMock()
        with (
            patch("app.calendar.Config") as config,
            patch("app.calendar.CalendarConnectionStore") as connections,
            patch("app.calendar.FutureMetadataCache") as cache,
        ):
            config.return_value.database = database
            service = CalendarSyncService()

        event_id = service._event_id(self.event())

        self.assertIs(service.db, database)
        self.assertIs(service.connections, connections.return_value)
        self.assertIs(service.future_cache, cache.return_value)
        self.assertEqual(event_id, service._event_id(self.event()))

    def test_matching_entities_uses_episode_and_series_ids(self):
        """Find and de-duplicate catalog matches for both Sonarr identities."""
        service = CalendarSyncService.__new__(CalendarSyncService)
        service.db = MagicMock()
        service.db.execute.side_effect = [
            [("episode-id", "episode"), ("episode-id", "episode")],
            [("series-id", "series")],
        ]

        matches = service._matching_entities(self.event())

        self.assertEqual(matches, [("episode-id", "episode"), ("series-id", "series")])
        self.assertEqual(service.db.execute.call_count, 2)

    def test_unusable_sonarr_payload_does_not_delete_saved_events(self):
        """Preserve persisted events when Sonarr returns unusable identities."""
        service = CalendarSyncService.__new__(CalendarSyncService)
        service.db = MagicMock()

        with self.assertRaisesRegex(ProviderError, "without usable"):
            service._replace_provider_events(
                "sonarr",
                "library",
                [{"id": 1, "tvdbId": 0, "airDateUtc": "2026-10-04"}],
                "seen-at",
                datetime(2026, 10, 1, tzinfo=timezone.utc),
                datetime(2026, 10, 31, tzinfo=timezone.utc),
            )

        service.db.execute.assert_not_called()

    def test_series_match_alone_stays_future(self):
        service = self.service([("series-id", "series")])

        self.assertFalse(service._upsert(self.event(), "2026-08-20T00:00:00+00:00"))
        update = service.db.execute.call_args_list[-1]
        self.assertEqual(update.args[1][0], "future")
        service.future_cache.promote_identity.assert_not_called()

    def test_exact_episode_match_is_existing_and_only_episode_is_linked(self):
        service = self.service([("series-id", "series"), ("episode-id", "episode")])
        cursor = service.db.transaction.return_value.__enter__.return_value

        self.assertTrue(service._upsert(self.event(), "2026-08-20T00:00:00+00:00"))
        linked = cursor.executemany.call_args.args[1]
        self.assertEqual(linked, [(linked[0][0], "episode-id")])
        update = service.db.execute.call_args_list[-1]
        self.assertEqual(update.args[1][0], "existing")

    def test_episode_without_episode_tvdb_id_promotes_series_metadata(self):
        """Promote series metadata when a matched episode has no TVDB ID."""
        service = self.service([("episode-id", "episode")])
        event = replace(self.event(), tvdb_id=None)

        self.assertTrue(service._upsert(event, "2026-08-20T00:00:00+00:00"))

        service.future_cache.promote_identity.assert_called_once_with(
            "tvdb", "series", "series-tvdb"
        )

    def test_movie_match_promotes_tmdb_metadata(self):
        """Promote the TMDB identity when a movie calendar event matches."""
        service = self.service([("movie-id", "movie")])
        event = replace(
            self.event(),
            provider="radarr",
            kind="movie",
            release_type="cinema",
            tvdb_id=None,
            series_tvdb_id=None,
            tmdb_id="movie-tmdb",
            season_number=None,
            episode_number=None,
        )

        self.assertTrue(service._upsert(event, "2026-08-20T00:00:00+00:00"))

        service.future_cache.promote_identity.assert_called_once_with(
            "tmdb", "movie", "movie-tmdb"
        )

    def test_reconcile_catalog_links_skips_when_required_tables_are_missing(self):
        """Avoid querying or mutating a partial pre-migration database."""
        service = CalendarSyncService.__new__(CalendarSyncService)
        service.db = MagicMock()
        service.db.execute.return_value = []

        self.assertEqual(service.reconcile_catalog_links("library"), 0)

        service.db.transaction.assert_not_called()

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

    def test_reconcile_catalog_links_matches_catalog_identities(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            db.execute(
                "CREATE TABLE calendar_events (id TEXT PRIMARY KEY,library_id TEXT,kind TEXT,tvdb_id TEXT,tmdb_id TEXT,series_tvdb_id TEXT,state TEXT,updated_at TEXT)"
            )
            db.execute(
                "CREATE TABLE calendar_event_entities (event_id TEXT,entity_id TEXT,PRIMARY KEY(event_id,entity_id))"
            )
            db.execute(
                "CREATE TABLE library_entities (id TEXT PRIMARY KEY,library_id TEXT,entity_type TEXT)"
            )
            db.execute(
                "CREATE TABLE entity_provider_ids (entity_id TEXT,provider TEXT,identifier_type TEXT,provider_id TEXT,PRIMARY KEY(entity_id,provider,identifier_type))"
            )
            db.execute("CREATE TABLE media_files (entity_id TEXT,role TEXT)")
            db.execute(
                "INSERT INTO calendar_events VALUES(?,?,?,?,?,?,?,?)",
                (
                    "episode-event",
                    "library",
                    "episode",
                    "episode-tvdb",
                    None,
                    "series-tvdb",
                    "future",
                    "before",
                ),
            )
            db.execute(
                "INSERT INTO calendar_events VALUES(?,?,?,?,?,?,?,?)",
                (
                    "movie-event",
                    "library",
                    "movie",
                    None,
                    "movie-tmdb",
                    None,
                    "future",
                    "before",
                ),
            )
            db.execute(
                "INSERT INTO calendar_events VALUES(?,?,?,?,?,?,?,?)",
                (
                    "series-only-event",
                    "library",
                    "episode",
                    "missing-episode",
                    None,
                    "series-tvdb",
                    "future",
                    "before",
                ),
            )
            db.execute(
                "INSERT INTO library_entities VALUES(?,?,?)",
                ("series-1", "library", "series"),
            )
            db.execute(
                "INSERT INTO library_entities VALUES(?,?,?)",
                ("episode-1", "library", "episode"),
            )
            db.execute(
                "INSERT INTO library_entities VALUES(?,?,?)",
                ("movie-1", "library", "movie"),
            )
            db.execute(
                "INSERT INTO entity_provider_ids VALUES(?,?,?,?)",
                ("series-1", "tvdb", "series", "series-tvdb"),
            )
            db.execute(
                "INSERT INTO entity_provider_ids VALUES(?,?,?,?)",
                ("episode-1", "tvdb", "episode", "episode-tvdb"),
            )
            db.execute(
                "INSERT INTO entity_provider_ids VALUES(?,?,?,?)",
                ("movie-1", "tmdb", "movie", "movie-tmdb"),
            )
            db.execute(
                "INSERT INTO media_files VALUES(?,?)",
                ("episode-1", "media"),
            )
            db.execute(
                "INSERT INTO media_files VALUES(?,?)",
                ("movie-1", "media"),
            )

            service = CalendarSyncService.__new__(CalendarSyncService)
            service.db = db
            service.future_cache = MagicMock()

            self.assertEqual(service.reconcile_catalog_links("library"), 2)
            self.assertEqual(
                db.execute(
                    "SELECT event_id,entity_id FROM calendar_event_entities ORDER BY event_id"
                ),
                [("episode-event", "episode-1"), ("movie-event", "movie-1")],
            )
            self.assertEqual(
                db.execute("SELECT id,state FROM calendar_events ORDER BY id"),
                [
                    ("episode-event", "existing"),
                    ("movie-event", "existing"),
                    ("series-only-event", "future"),
                ],
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM calendar_event_entities x JOIN media_files m ON m.entity_id=x.entity_id AND m.role='media'"
                )[0][0],
                2,
            )
            service.future_cache.promote_identity.assert_any_call(
                "tvdb", "episode", "episode-tvdb"
            )
            service.future_cache.promote_identity.assert_any_call(
                "tvdb", "series", "series-tvdb"
            )
            service.future_cache.promote_identity.assert_any_call(
                "tmdb", "movie", "movie-tmdb"
            )
        finally:
            db.close()

    def test_reconcile_catalog_links_removes_stale_entity_links(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            db.execute(
                "CREATE TABLE calendar_events (id TEXT PRIMARY KEY,library_id TEXT,kind TEXT,tvdb_id TEXT,tmdb_id TEXT,series_tvdb_id TEXT,state TEXT,updated_at TEXT)"
            )
            db.execute(
                "CREATE TABLE calendar_event_entities (event_id TEXT,entity_id TEXT,PRIMARY KEY(event_id,entity_id))"
            )
            db.execute(
                "CREATE TABLE library_entities (id TEXT PRIMARY KEY,library_id TEXT,entity_type TEXT)"
            )
            db.execute(
                "CREATE TABLE entity_provider_ids (entity_id TEXT,provider TEXT,identifier_type TEXT,provider_id TEXT,PRIMARY KEY(entity_id,provider,identifier_type))"
            )
            db.execute(
                "INSERT INTO calendar_events VALUES(?,?,?,?,?,?,?,?)",
                (
                    "event-1",
                    "library",
                    "episode",
                    "episode-tvdb",
                    None,
                    "series-tvdb",
                    "existing",
                    "before",
                ),
            )
            db.execute(
                "INSERT INTO library_entities VALUES(?,?,?)",
                ("episode-1", "library", "episode"),
            )
            db.execute(
                "INSERT INTO calendar_event_entities VALUES(?,?)",
                ("event-1", "episode-1"),
            )

            service = CalendarSyncService.__new__(CalendarSyncService)
            service.db = db
            service.future_cache = MagicMock()

            self.assertEqual(service.reconcile_catalog_links("library"), 1)
            self.assertEqual(db.execute("SELECT * FROM calendar_event_entities"), [])
            self.assertEqual(
                db.execute("SELECT state FROM calendar_events WHERE id='event-1'"),
                [("future",)],
            )
        finally:
            db.close()


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
            @staticmethod
            def get_locales(provider, entity_type, provider_id):
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
            @staticmethod
            def get_locales(provider, entity_type, provider_id):
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
            @staticmethod
            def refetch(should_terminate, progress):
                progress(0, 2, None, False)
                progress(1, 2, "TVDB episode 1", False)
                progress(2, 2, "TVDB episode 2", True)
                return {"targets": 2, "updated": 1, "errors": ["failed"]}

        store = Store()
        with patch(
            "app.calendar.CalendarFutureMetadataService", return_value=Service()
        ):
            CalendarFutureMetadataJob(store).run("run-1", lambda: False)

        self.assertEqual(len(store.updates), 4)
        self.assertEqual(store.updates[1][1]["progress_stage_current"], 1)
        self.assertEqual(store.updates[1][1]["progress_stage_total"], 2)
        self.assertEqual(store.updates[2][1]["progress_current_item"], "TVDB episode 2")
        self.assertEqual(store.updates[-1][1]["state"], "completed")


if __name__ == "__main__":
    unittest.main()
