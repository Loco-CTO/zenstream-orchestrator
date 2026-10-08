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
class FakeStreamEvent:
    kind: str
    text: str | None = None
    turn: object | None = None
    reason: str | None = None


@dataclass(frozen=True)
class FakeAnswer:
    markdown: str
    references: tuple = ()
    sources: tuple = ()


@dataclass(frozen=True)
class FakeTurn:
    conversation: object
    answer: FakeAnswer


class StreamingFakeLumiService:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.call = None

    async def stream_chat_for_account(self, *args, **kwargs):
        self.call = (args, kwargs)
        try:
            for event in self.events:
                yield event
        finally:
            self.closed = True


class FakeRequest:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    async def body(self):
        return self.payload


@dataclass(frozen=True)
class FakeConversation:
    id: str
    title: str
    model: str
    thinking: bool
    created_at: str
    updated_at: str


class LumiRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_turn_relays_visible_deltas_then_one_complete_event(self):
        turn = FakeTurn(
            FakeConversation(
                id="conversation-1",
                title="What to watch next",
                model="qwen3.5:2b",
                thinking=True,
                created_at="2026-01-01T12:00:00Z",
                updated_at="2026-01-01T12:01:00Z",
            ),
            FakeAnswer("Hello **there**"),
        )
        service = StreamingFakeLumiService(
            [
                FakeStreamEvent("delta", text="Hello "),
                FakeStreamEvent("tool_call", text="private tool arguments"),
                FakeStreamEvent("delta", text="**there**"),
                FakeStreamEvent("complete", turn=turn),
            ]
        )
        with (
            patch.object(
                lumi_routes,
                "_account",
                new=AsyncMock(return_value={"id": "trusted-account"}),
            ),
            patch.object(lumi_routes, "_service", return_value=service),
        ):
            response = await lumi_routes.lumi_turn_stream(
                FakeRequest({"message": "hello", "model": "qwen3.5:2b", "thinking": True}),
                "conversation-1",
            )
            body = b"".join([chunk async for chunk in response.body_iterator]).decode("utf-8")

        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(response.headers["cache-control"], "private, no-store, no-transform")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertEqual(body.count("event: delta"), 2)
        self.assertEqual(body.count("event: complete"), 1)
        self.assertNotIn("private tool arguments", body)
        self.assertIn('"markdown":"Hello **there**"', body)
        self.assertEqual(service.call[0], ("trusted-account", "conversation-1", "hello"))
        self.assertTrue(service.closed)

    async def test_stream_turn_uses_complete_event_for_older_installed_release(self):
        turn = FakeTurn(
            FakeConversation(
                id="conversation-1",
                title="Older release",
                model="qwen3.5:2b",
                thinking=False,
                created_at="2026-01-01T12:00:00Z",
                updated_at="2026-01-01T12:01:00Z",
            ),
            FakeAnswer("Complete response from the installed release"),
        )
        service = SimpleNamespace(chat_for_account=AsyncMock(return_value=turn))
        with (
            patch.object(
                lumi_routes,
                "_account",
                new=AsyncMock(return_value={"id": "trusted-account"}),
            ),
            patch.object(lumi_routes, "_service", return_value=service),
        ):
            response = await lumi_routes.lumi_turn_stream(
                FakeRequest({"message": "hello"}), "conversation-1"
            )
            body = b"".join([chunk async for chunk in response.body_iterator]).decode("utf-8")

        self.assertEqual(body.count("event: complete"), 1)
        self.assertNotIn("event: delta", body)
        self.assertIn('"markdown":"Complete response from the installed release"', body)
        service.chat_for_account.assert_awaited_once_with(
            "trusted-account",
            "conversation-1",
            "hello",
            model=None,
            thinking=None,
        )

    async def test_stream_turn_cancellation_closes_lumi_generator(self):
        service = StreamingFakeLumiService([FakeStreamEvent("delta", text="first")])
        with (
            patch.object(
                lumi_routes,
                "_account",
                new=AsyncMock(return_value={"id": "trusted-account"}),
            ),
            patch.object(lumi_routes, "_service", return_value=service),
        ):
            response = await lumi_routes.lumi_turn_stream(
                FakeRequest({"message": "hello"}), "conversation-1"
            )
            iterator = response.body_iterator
            self.assertIn(b'"text":"first"', await iterator.__anext__())
            await iterator.aclose()

        self.assertTrue(service.closed)

    async def test_stream_turn_relays_multiple_safe_reset_reasons(self):
        turn = FakeTurn(
            FakeConversation(
                id="conversation-1",
                title="Retry",
                model="qwen3.5:2b",
                thinking=False,
                created_at="2026-01-01T12:00:00Z",
                updated_at="2026-01-01T12:01:00Z",
            ),
            FakeAnswer("CPU response"),
        )
        service = StreamingFakeLumiService(
            [
                FakeStreamEvent("delta", text="discarded tool preamble"),
                FakeStreamEvent("reset", reason="intermediate"),
                FakeStreamEvent("delta", text="discarded retry preamble"),
                FakeStreamEvent("reset", reason="intermediate"),
                FakeStreamEvent("delta", text="discarded GPU response"),
                FakeStreamEvent("reset", reason="cpu_fallback"),
                FakeStreamEvent("delta", text="CPU response"),
                FakeStreamEvent("complete", turn=turn),
            ]
        )
        with (
            patch.object(
                lumi_routes,
                "_account",
                new=AsyncMock(return_value={"id": "trusted-account"}),
            ),
            patch.object(lumi_routes, "_service", return_value=service),
        ):
            response = await lumi_routes.lumi_turn_stream(
                FakeRequest({"message": "hello"}), "conversation-1"
            )
            body = b"".join([chunk async for chunk in response.body_iterator]).decode("utf-8")

        self.assertEqual(body.count("event: reset"), 3)
        self.assertEqual(body.count("event: complete"), 1)
        self.assertIn('event: reset\ndata: {"reason":"intermediate"}\n\n', body)
        self.assertIn('event: reset\ndata: {"reason":"cpu_fallback"}\n\n', body)
        self.assertIn('"markdown":"CPU response"', body)
        self.assertTrue(service.closed)

    async def test_stream_turn_errors_use_safe_event_messages(self):
        class BrokenStreamingService:
            async def stream_chat_for_account(self, *_args, **_kwargs):
                raise RuntimeError("C:/private/model.gguf")
                yield

        with (
            patch.object(
                lumi_routes,
                "_account",
                new=AsyncMock(return_value={"id": "trusted-account"}),
            ),
            patch.object(lumi_routes, "_service", return_value=BrokenStreamingService()),
            patch.object(lumi_routes.logger, "exception"),
        ):
            response = await lumi_routes.lumi_turn_stream(
                FakeRequest({"message": "hello"}), "conversation-1"
            )
            body = b"".join([chunk async for chunk in response.body_iterator]).decode("utf-8")

        self.assertIn(
            'event: error\ndata: {"message":"Lumi is temporarily unavailable."}',
            body,
        )
        self.assertNotIn("private/model.gguf", body)

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

    async def test_admin_runtime_settings_accepts_gpu_mode(self):
        fake_host = SimpleNamespace(
            update_runtime_settings=AsyncMock(return_value={"gpuMode": "cpu_only"})
        )
        with (
            patch.object(lumi_routes, "lumi_host", fake_host),
            patch.object(lumi_routes, "_admin", new=AsyncMock(return_value="admin")),
        ):
            response = await lumi_routes.update_admin_lumi_runtime_settings(
                FakeRequest({"gpuMode": "cpu_only"})
            )

        fake_host.update_runtime_settings.assert_awaited_once_with(
            default_thinking=None,
            limits=None,
            gpu_mode="cpu_only",
        )
        self.assertEqual(json.loads(response.body), {"gpuMode": "cpu_only"})

    async def test_admin_runtime_settings_rejects_invalid_gpu_mode(self):
        with (
            patch.object(lumi_routes, "_admin", new=AsyncMock(return_value="admin")),
        ):
            with self.assertRaises(lumi_routes.HTTPException) as error:
                await lumi_routes.update_admin_lumi_runtime_settings(
                    FakeRequest({"gpuMode": "--n-gpu-layers 99"})
                )

        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(error.exception.detail, "The Lumi runtime settings are invalid.")

    async def test_admin_runtime_settings_accepts_null_optional_gpu_mode(self):
        fake_host = SimpleNamespace(
            update_runtime_settings=AsyncMock(return_value={"gpuMode": "automatic"})
        )
        with (
            patch.object(lumi_routes, "lumi_host", fake_host),
            patch.object(lumi_routes, "_admin", new=AsyncMock(return_value="admin")),
        ):
            response = await lumi_routes.update_admin_lumi_runtime_settings(
                FakeRequest({"gpuMode": None})
            )

        fake_host.update_runtime_settings.assert_awaited_once_with(
            default_thinking=None,
            limits=None,
            gpu_mode=None,
        )
        self.assertEqual(json.loads(response.body), {"gpuMode": "automatic"})

    async def test_admin_can_request_model_download_cancellation(self):
        fake_host = SimpleNamespace(
            cancel_model_install=AsyncMock(
                return_value={"id": "qwen3.5:2b", "cancellationRequested": True}
            ),
            model_settings=lambda: {"models": []},
        )
        with (
            patch.object(lumi_routes, "lumi_host", fake_host),
            patch.object(lumi_routes, "_admin", new=AsyncMock(return_value="admin")),
        ):
            response = await lumi_routes.cancel_admin_lumi_model_download(
                None, "qwen3.5:2b"
            )

        fake_host.cancel_model_install.assert_awaited_once_with("qwen3.5:2b")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(json.loads(response.body), {"models": []})

    async def test_admin_model_download_cancellation_reports_conflict(self):
        fake_host = SimpleNamespace(
            cancel_model_install=AsyncMock(
                side_effect=lumi_routes.LumiHostError("There is no active download.")
            )
        )
        with (
            patch.object(lumi_routes, "lumi_host", fake_host),
            patch.object(lumi_routes, "_admin", new=AsyncMock(return_value="admin")),
        ):
            with self.assertRaises(lumi_routes.HTTPException) as error:
                await lumi_routes.cancel_admin_lumi_model_download(None, "missing")

        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail, "There is no active download.")

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
