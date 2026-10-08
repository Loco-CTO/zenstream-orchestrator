from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.lumi_host import LumiHost, LumiHostError, _create_runtime_adapter
from app.lumi_release import LumiReleaseCompatibilityError


class FakeReleaseManager:
    def __init__(self, *_args):
        self.enable_calls: list[str] = []
        self.disabled = 0
        self.restart_required = False

    async def list_published_releases(self, limit: int):
        return [
            {"tag": "v1.2.3", "releasedAt": None},
            {"tag": "v1.3.0-rc.1", "releasedAt": None},
            {"tag": "main", "releasedAt": None},
        ][:limit]

    async def enable(self, tag: str):
        self.enable_calls.append(tag)
        return self.loaded_release(tag)

    @staticmethod
    def loaded_release(tag: str):
        model = SimpleNamespace(
            model_id="qwen3.5:2b",
            label="Qwen3.5 2B",
            supports_thinking=True,
        )
        package_module = SimpleNamespace(supported_models=lambda: (model,))
        manifest = SimpleNamespace(installer_dependencies=())
        return SimpleNamespace(
            tag=tag,
            package_module=package_module,
            manifest=manifest,
        )

    async def disable(self):
        self.disabled += 1


class LumiHostTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_adapter_prefers_llama_cpp_and_preserves_settings(self):
        class Config:
            def __init__(self, **values):
                self.values = values

        class Runtime:
            def __init__(self, configuration):
                self.configuration = configuration

        module = SimpleNamespace(
            LlamaCppConfig=Config,
            LlamaCppChatRuntime=Runtime,
            OrtGenAIConfig=Config,
            OrtGenAIChatRuntime=Runtime,
        )
        artifacts = {"qwen3.5:2b": object()}
        limits = {
            "idleUnloadSeconds": 300,
            "maxContextTokens": 8192,
            "maxOutputTokens": 2048,
        }

        runtime = _create_runtime_adapter(
            module, artifacts, limits, "llama-cpp-python"
        )

        self.assertIsInstance(runtime, Runtime)
        self.assertIsInstance(runtime.configuration, Config)
        self.assertEqual(
            runtime.configuration.values,
            {
                "model_artifacts": artifacts,
                "idle_unload_seconds": 300,
                "max_context_tokens": 8192,
                "max_output_tokens": 2048,
            },
        )

    def test_runtime_adapter_keeps_legacy_onnx_releases_working(self):
        class Config:
            def __init__(self, **values):
                self.values = values

        class Runtime:
            def __init__(self, configuration):
                self.configuration = configuration

        runtime = _create_runtime_adapter(
            SimpleNamespace(OrtGenAIConfig=Config, OrtGenAIChatRuntime=Runtime),
            {},
            {
                "idleUnloadSeconds": 300,
                "maxContextTokens": 8192,
                "maxOutputTokens": 2048,
            },
            "onnxruntime-genai",
        )

        self.assertIsInstance(runtime, Runtime)
        self.assertEqual(runtime.configuration.values["model_artifacts"], {})

    def test_runtime_adapter_rejects_releases_without_a_local_backend(self):
        with self.assertRaisesRegex(LumiHostError, "local runtime"):
            _create_runtime_adapter(
                SimpleNamespace(), {}, {}, "onnxruntime-genai"
            )

    def test_runtime_adapter_follows_release_backend_when_both_are_exported(self):
        class LlamaConfig:
            def __init__(self, **_values):
                self.backend = "llama-cpp-python"

        class OrtConfig:
            def __init__(self, **_values):
                self.backend = "onnxruntime-genai"

        class Runtime:
            def __init__(self, configuration):
                self.configuration = configuration

        module = SimpleNamespace(
            LlamaCppConfig=LlamaConfig,
            LlamaCppChatRuntime=Runtime,
            OrtGenAIConfig=OrtConfig,
            OrtGenAIChatRuntime=Runtime,
        )
        runtime = _create_runtime_adapter(
            module,
            {},
            {
                "idleUnloadSeconds": 300,
                "maxContextTokens": 8192,
                "maxOutputTokens": 2048,
            },
            "onnxruntime-genai",
        )

        self.assertEqual(runtime.configuration.backend, "onnxruntime-genai")

    def test_clean_host_construction_does_not_create_a_release_manager(self):
        created = []

        def factory(*args):
            created.append(args)
            return FakeReleaseManager(*args)

        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(data_directory=directory, release_manager_factory=factory)

        self.assertEqual(created, [])
        self.assertFalse(host.status()["integration"]["enabled"])
        self.assertIsNone(host.service)

    async def test_release_list_contains_only_stable_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(
                data_directory=directory,
                release_manager_factory=FakeReleaseManager,
            )

            releases = await host.list_releases()

        self.assertEqual(releases, [{"tag": "v1.2.3", "releasedAt": None}])

    async def test_release_download_starts_only_after_explicit_enable_and_can_disable(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(
                data_directory=directory,
                release_manager_factory=FakeReleaseManager,
            )
            host._rebuild_service = AsyncMock()

            with self.assertRaises(LumiHostError):
                await host.enable("main")
            self.assertIsNone(host._release_manager)

            await host.enable("v1.2.3")
            await host._operation

            self.assertEqual(host._release_manager.enable_calls, ["v1.2.3"])
            self.assertTrue(host.status()["integration"]["enabled"])
            self.assertTrue(host.status()["integration"]["installed"])
            self.assertEqual(host._rebuild_service.await_count, 1)

            await host.disable()

            self.assertEqual(host._release_manager.disabled, 1)
            self.assertFalse(host.status()["integration"]["enabled"])
            self.assertFalse(host.status()["integration"]["installed"])

    async def test_saved_enabled_release_is_restored_only_after_startup_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "lumi" / "integration.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps(
                    {"schemaVersion": 1, "enabled": True, "releaseTag": "v1.2.3"}
                ),
                encoding="utf-8",
            )
            host = LumiHost(
                data_directory=directory,
                release_manager_factory=FakeReleaseManager,
            )
            host._rebuild_service = AsyncMock()

            self.assertIsNone(host._release_manager)
            await host.load_saved_integration()
            await host._operation

            self.assertEqual(host._release_manager.enable_calls, ["v1.2.3"])
            self.assertTrue(host.status()["integration"]["enabled"])

            await host.disable()

    async def test_install_failure_is_reported_without_enabling_the_integration(self):
        class BrokenReleaseManager(FakeReleaseManager):
            async def enable(self, _tag: str):
                raise OSError("private package path")

        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(
                data_directory=directory,
                release_manager_factory=BrokenReleaseManager,
            )
            await host.enable("v1.2.3")
            with self.assertLogs("zenstream.lumi", level="ERROR"):
                await host._operation

        status = host.status()["integration"]
        self.assertEqual(status["state"], "error")
        self.assertFalse(status["enabled"])
        self.assertEqual(status["error"], "OSError")
        self.assertNotIn("private package path", str(status))

    async def test_release_compatibility_error_is_reported_with_safe_details(self):
        class IncompatibleReleaseManager(FakeReleaseManager):
            async def enable(self, _tag: str):
                raise LumiReleaseCompatibilityError(
                    "Lumi release declares too many runtime wheels"
                )

        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(
                data_directory=directory,
                release_manager_factory=IncompatibleReleaseManager,
            )
            await host.enable("v1.2.3")
            with self.assertLogs("zenstream.lumi", level="ERROR"):
                await host._operation

        status = host.status()["integration"]
        self.assertEqual(status["state"], "error")
        self.assertFalse(status["enabled"])
        self.assertEqual(
            status["error"], "Lumi release declares too many runtime wheels"
        )

    async def test_disable_gates_chat_before_a_pending_install_finishes(self):
        release_ready = asyncio.Event()

        class SlowReleaseManager(FakeReleaseManager):
            async def enable(self, tag: str):
                await release_ready.wait()
                self.enable_calls.append(tag)
                return self.loaded_release(tag)

        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(
                data_directory=directory,
                release_manager_factory=SlowReleaseManager,
            )
            host._rebuild_service = AsyncMock()
            await host.enable("v1.2.3")
            await asyncio.sleep(0)

            disabling = asyncio.create_task(host.disable())
            await asyncio.sleep(0)

            self.assertFalse(host.status()["integration"]["enabled"])
            release_ready.set()
            await disabling

        self.assertEqual(host._release_manager.disabled, 1)
        self.assertEqual(host.status()["integration"]["state"], "disabled")
        self.assertIsNone(host.service)

    def test_local_tool_registry_does_not_require_a_web_search_url(self):
        fake_tool = lambda *_args, **_kwargs: object()
        modules = {
            "lumi.tools": SimpleNamespace(ToolRegistry=lambda tools: tuple(tools)),
            "lumi.orchestrator_tools": SimpleNamespace(
                _OrchestratorReadError=RuntimeError,
                CatalogSearchTool=fake_tool,
                CatalogItemDetailTool=fake_tool,
                HomeRecommendationsTool=fake_tool,
                ContinueWatchingTool=fake_tool,
                NextUpTool=fake_tool,
                FavoritesTool=fake_tool,
            ),
        }
        imported = []

        def import_module(name):
            imported.append(name)
            return modules[name]

        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(data_directory=directory, catalog=object())
            with patch(
                "app.lumi_host.importlib.import_module", side_effect=import_module
            ):
                registry = host._create_tool_registry(SimpleNamespace())

        self.assertEqual(len(registry), 6)
        self.assertNotIn("lumi.web_research", imported)

    def test_web_research_tools_are_added_only_when_a_search_url_is_configured(self):
        fake_tool = lambda *_args, **_kwargs: object()
        search_tool = object()
        open_tool = object()
        web_module = SimpleNamespace(
            WebResearchConfig=Mock(return_value="search-config"),
            build_web_research_tools=Mock(return_value=(search_tool, open_tool)),
        )
        modules = {
            "lumi.tools": SimpleNamespace(ToolRegistry=lambda tools: tuple(tools)),
            "lumi.orchestrator_tools": SimpleNamespace(
                _OrchestratorReadError=RuntimeError,
                CatalogSearchTool=fake_tool,
                CatalogItemDetailTool=fake_tool,
                HomeRecommendationsTool=fake_tool,
                ContinueWatchingTool=fake_tool,
                NextUpTool=fake_tool,
                FavoritesTool=fake_tool,
            ),
            "lumi.web_research": web_module,
        }

        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(data_directory=directory, catalog=object())
            host.configure_web_search("http://search.test:8080")
            with patch(
                "app.lumi_host.importlib.import_module",
                side_effect=lambda name: modules[name],
            ):
                registry = host._create_tool_registry(SimpleNamespace())

        self.assertEqual(len(registry), 8)
        web_module.WebResearchConfig.assert_called_once_with(
            searxng_url="http://search.test:8080"
        )
        web_module.build_web_research_tools.assert_called_once_with("search-config")


if __name__ == "__main__":
    unittest.main()
