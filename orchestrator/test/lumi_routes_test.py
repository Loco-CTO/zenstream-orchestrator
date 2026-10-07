from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from api.zenstream import lumi_routes


class FakeLumiService:
    def __init__(self):
        self.account_ids: list[str] = []

    async def list_conversations_for_account(self, account_id: str, limit: int):
        self.account_ids.append(account_id)
        self.limit = limit
        return [
            FakeConversation(
                id="conversation-1",
                title="What to watch next",
                model="qwen3.5:2b",
                thinking=True,
                created_at="2026-01-01T12:00:00Z",
                updated_at="2026-01-01T12:01:00Z",
            ),
        ]


@dataclass(frozen=True)
class FakeConversation:
    id: str
    title: str
    model: str
    thinking: bool
    created_at: str
    updated_at: str


class LumiRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_integration_gates_a_stale_service_reference(self):
        fake_host = SimpleNamespace(
            status=lambda: {"integration": {"enabled": False}},
            service=object(),
        )
        with patch.object(lumi_routes, "lumi_host", fake_host):
            with self.assertRaises(lumi_routes.HTTPException) as error:
                lumi_routes._service()

        self.assertEqual(error.exception.status_code, 503)

    async def test_conversation_list_uses_authenticated_account_and_is_private(self):
        service = FakeLumiService()
        with (
            patch.object(
                lumi_routes,
                "_account",
                new=AsyncMock(return_value={"id": "trusted-account"}),
            ),
            patch.object(lumi_routes, "_service", return_value=service),
        ):
            response = await lumi_routes.lumi_conversations(None)

        payload = json.loads(response.body)
        self.assertEqual(service.account_ids, ["trusted-account"])
        self.assertEqual(service.limit, 50)
        self.assertEqual(payload["conversations"][0]["id"], "conversation-1")
        self.assertEqual(response.headers["cache-control"], "private, no-store")

    async def test_service_errors_do_not_leak_internal_paths(self):
        with (
            patch.object(
                lumi_routes,
                "_account",
                new=AsyncMock(return_value={"id": "trusted-account"}),
            ),
            patch.object(lumi_routes, "_service", return_value=BrokenLumiService()),
        ):
            with self.assertRaises(lumi_routes.HTTPException) as error:
                await lumi_routes.lumi_conversations(None)

        self.assertEqual(error.exception.status_code, 503)
        self.assertNotIn("private.db", error.exception.detail)


class BrokenLumiService:
    async def list_conversations_for_account(self, *_args, **_kwargs):
        raise OSError("C:/private/private.db")


if __name__ == "__main__":
    unittest.main()
