from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.lumi_catalog import LumiCatalogAdapter


class CatalogUnavailable(RuntimeError):
    pass


class FakeCatalog:
    def __init__(self):
        self.calls = []

    def search(self, *args):
        self.calls.append(("search", args))
        return {
            "items": [
                {
                    "id": "series-1",
                    "type": "series",
                    "name": "Frieren",
                    "metadata": {"title": "Frieren", "overview": "A quiet journey."},
                    "userState": {"favorite": True},
                }
            ],
            "total": 1,
        }

    def detail(self, *args):
        self.calls.append(("detail", args))
        return {"item": self.search_item(), "backgroundItem": None, "seasons": []}

    def search_item(self):
        return {
            "id": "series-1",
            "type": "series",
            "name": "Frieren",
            "metadata": {"title": "Frieren"},
        }

    def home_recommendations(self, *args):
        self.calls.append(("recommendations", args))
        return [self.search_item()]

    def home_continue_watching(self, *args):
        self.calls.append(("continue", args))
        return [self.search_item()]

    def home_next_up(self, *args):
        self.calls.append(("next-up", args))
        return [self.search_item()]

    def favorites(self, *args):
        self.calls.append(("favorites", args))
        return {"items": [self.search_item()], "total": 1}


class FakePreferences:
    history_enabled = True

    def __init__(self, _user_id):
        pass

    def metadata_language(self):
        return {"language": "en"}

    def watch_history(self):
        return {"enabled": self.history_enabled}


class FakeLanguages:
    def get(self):
        return ["en", "ja"]


class LumiCatalogAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = FakeCatalog()

        async def run(function, *args, **kwargs):
            return function(*args, **kwargs)

        self.run_patcher = patch("app.lumi_catalog.run_foreground", side_effect=run)
        self.run_patcher.start()
        self.addCleanup(self.run_patcher.stop)
        self.preference_patcher = patch(
            "app.lumi_catalog.AccountPreference", FakePreferences
        )
        self.preference_patcher.start()
        self.addCleanup(self.preference_patcher.stop)
        self.language_patcher = patch(
            "app.lumi_catalog.MetadataLanguageSettings", FakeLanguages
        )
        self.language_patcher.start()
        self.addCleanup(self.language_patcher.stop)
        self.language_normalizer_patcher = patch(
            "app.lumi_catalog.normalize_metadata_locale", side_effect=lambda value: value
        )
        self.language_normalizer_patcher.start()
        self.addCleanup(self.language_normalizer_patcher.stop)
        self.adapter = LumiCatalogAdapter(self.catalog, tool_error=CatalogUnavailable)
        self.context = SimpleNamespace(account_id="trusted-account")

    async def test_search_uses_server_account_and_returns_compact_local_entities(self):
        result = await self.adapter.search(
            self.context,
            query="Frieren",
            item_type="series",
            limit=5,
            language="ja",
        )

        self.assertEqual(self.catalog.calls[0][1][0], "trusted-account")
        self.assertEqual(self.catalog.calls[0][1][2], "ja")
        self.assertEqual(result["items"][0]["id"], "series-1")
        self.assertEqual(result["items"][0]["title"], "Frieren")
        self.assertEqual(result["total"], 1)

    async def test_detail_uses_grant_checked_catalog_detail(self):
        result = await self.adapter.item_detail(
            self.context, entity_id="series-1", language=None
        )

        self.assertEqual(self.catalog.calls[0][0], "detail")
        self.assertEqual(self.catalog.calls[0][1][0], "trusted-account")
        self.assertEqual(result["item"]["id"], "series-1")

    async def test_history_rows_are_empty_when_watch_history_is_disabled(self):
        FakePreferences.history_enabled = False

        try:
            result = await self.adapter.continue_watching(self.context)
        finally:
            FakePreferences.history_enabled = True

        self.assertEqual(result, {"items": []})
        self.assertEqual(self.catalog.calls, [])

    async def test_catalog_exceptions_become_a_safe_tool_error(self):
        self.catalog.search = lambda *_args: (_ for _ in ()).throw(
            RuntimeError("private filesystem details")
        )

        with self.assertRaisesRegex(CatalogUnavailable, "lookup is unavailable") as error:
            await self.adapter.search(
                self.context,
                query="Frieren",
                item_type=None,
                limit=5,
                language=None,
            )

        self.assertNotIn("private filesystem", str(error.exception))


if __name__ == "__main__":
    unittest.main()
