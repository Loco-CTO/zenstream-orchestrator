from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
    async def test_admin_release_list_runs_in_control_lane_and_returns_releases(self):
        list_releases = Mock(
            return_value=[{"tag": "v0.3.4", "releasedAt": "2026-10-08T10:00:00Z"}]
        )
        release_manager = object()
        fake_host = SimpleNamespace(
            release_manager=release_manager,
            list_releases_sync=list_releases,
        )
        run_control = AsyncMock(side_effect=lambda function, *args: function(*args))
        with (
            patch.object(lumi_routes, "lumi_host", fake_host),
            patch.object(lumi_routes, "_admin", new=AsyncMock(return_value="admin")),
            patch.object(lumi_routes, "run_control", run_control),
        ):
            response = await lumi_routes.admin_lumi_releases(None)

        run_control.assert_awaited_once_with(list_releases, release_manager)
        list_releases.assert_called_once_with(release_manager)
        self.assertEqual(json.loads(response.body)["releases"][0]["tag"], "v0.3.4")
        self.assertEqual(response.headers["cache-control"], "private, no-store")

    async def test_admin_release_list_logs_failure_and_keeps_http_error_safe(self):
        fake_host = SimpleNamespace(release_manager=object(), list_releases_sync=Mock())
        run_control = AsyncMock(side_effect=OSError("C:/private/release-cache.json"))
        with (
            patch.object(lumi_routes, "lumi_host", fake_host),
            patch.object(lumi_routes, "_admin", new=AsyncMock(return_value="admin")),
            patch.object(lumi_routes, "run_control", run_control),
            patch.object(lumi_routes.logger, "exception") as log_exception,
        ):
            with self.assertRaises(lumi_routes.HTTPException) as error:
                await lumi_routes.admin_lumi_releases(None)

        self.assertEqual(error.exception.status_code, 503)
        self.assertNotIn("private/release-cache", error.exception.detail)
        log_exception.assert_called_once_with("Lumi published release discovery failed")

    async def test_admin_removal_endpoint_calls_host_and_returns_private_status(self):
        fake_host = SimpleNamespace(
            remove_installation=AsyncMock(),
            status=lambda: {
                "integration": {
                    "enabled": False,
                    "installed": False,
                    "loaded": False,
                    "state": "disabled",
                    "releaseTag": None,
                    "restartRequired": False,
                    "error": None,
                    "progress": None,
                },
                "models": [],
                "defaultModel": None,
                "defaultThinking": False,
            },
        )
        with (
            patch.object(lumi_routes, "lumi_host", fake_host),
            patch.object(lumi_routes, "_admin", new=AsyncMock(return_value="admin")),
        ):
            response = await lumi_routes.remove_admin_lumi_installation(None)

        fake_host.remove_installation.assert_awaited_once_with()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertFalse(json.loads(response.body)["integration"]["installed"])

    async def test_admin_removal_conflict_is_reported_as_client_conflict(self):
        fake_host = SimpleNamespace(
            remove_installation=AsyncMock(
                side_effect=lumi_routes.LumiHostError(
                    "restart Orchestrator before removing Lumi runtime files"
                )
            )
        )
        with (
            patch.object(lumi_routes, "lumi_host", fake_host),
            patch.object(lumi_routes, "_admin", new=AsyncMock(return_value="admin")),
        ):
            with self.assertRaises(lumi_routes.HTTPException) as error:
                await lumi_routes.remove_admin_lumi_installation(None)

        self.assertEqual(error.exception.status_code, 409)

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
