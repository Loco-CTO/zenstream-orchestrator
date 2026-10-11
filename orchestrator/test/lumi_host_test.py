from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.lumi_host import (
    LumiHost,
    LumiHostError,
    _create_agent_limits,
    _create_runtime_adapter,
    _install_qwen_model,
    _supports_model_download_cancellation,
)
from app.lumi_release import LumiReleaseCompatibilityError


class FakeReleaseManager:
    def __init__(self, *_args):
        self.enable_calls: list[str] = []
        self.disabled = 0
        self.restart_required = False
        self.installed_tags: set[str] = set()

    def list_published_releases_sync(self, limit: int):
        return [
            {"tag": "v1.2.3", "releasedAt": None},
            {"tag": "v1.3.0-rc.1", "releasedAt": None},
            {"tag": "main", "releasedAt": None},
        ][:limit]

    async def list_published_releases(self, limit: int):
        return self.list_published_releases_sync(limit)

    async def enable(self, tag: str):
        self.enable_calls.append(tag)
        self.installed_tags.add(tag)
        return self.loaded_release(tag)

    def has_installed_release(self, tag: str) -> bool:
        return tag in self.installed_tags

    async def remove_installed_releases(self) -> bool:
        removed = bool(self.installed_tags)
        self.installed_tags.clear()
        return removed

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
    def test_download_cancellation_is_only_enabled_for_compatible_releases(self):
        class CancellableInstaller:
            def install_model(self, _model_id, *, progress=None, cancel_event=None):
                return None

        class LegacyInstaller:
            def install_model(self, _model_id, *, progress=None):
                return None

        class GenericKeywordInstaller:
            def install_model(self, _model_id, **_kwargs):
                return None

        self.assertTrue(
            _supports_model_download_cancellation(
                SimpleNamespace(Qwen35ModelInstaller=CancellableInstaller)
            )
        )
        self.assertFalse(
            _supports_model_download_cancellation(
                SimpleNamespace(Qwen35ModelInstaller=LegacyInstaller)
            )
        )
        self.assertFalse(
            _supports_model_download_cancellation(
                SimpleNamespace(Qwen35ModelInstaller=GenericKeywordInstaller)
            )
        )
        self.assertFalse(_supports_model_download_cancellation(SimpleNamespace()))

    class PendingOperation:
        def done(self):
            return False

        def cancel(self):
            return None

    def test_install_adapter_forwards_cancellation_to_supported_installer(self):
        captured = {}

        class CancellableInstaller:
            def __init__(self, _model_root):
                pass

            def install_model(self, model_id, *, progress=None, cancel_event=None):
                captured.update(
                    model_id=model_id,
                    progress=progress,
                    cancel_event=cancel_event,
                )
                return "artifact"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_file = root / "site-packages" / "lumi" / "__init__.py"
            package_file.parent.mkdir(parents=True)
            package_file.touch()
            dependency = root / "installer-dependencies" / "native"
            dependency.mkdir(parents=True)
            cancellation = threading.Event()
            progress = object()
            package_module = SimpleNamespace(
                __file__=str(package_file),
                Qwen35ModelInstaller=CancellableInstaller,
            )

            result = _install_qwen_model(
                package_module,
                root / "models",
                "qwen3.5:2b",
                (dependency,),
                progress,
                cancellation,
            )

        self.assertEqual(result, "artifact")
        self.assertEqual(captured["model_id"], "qwen3.5:2b")
        self.assertIs(captured["progress"], progress)
        self.assertIs(captured["cancel_event"], cancellation)

    async def test_model_download_cancel_is_cooperative_and_visible_in_status(self):
        class CancellableInstaller:
            def install_model(self, _model_id, *, progress=None, cancel_event=None):
                return None

        model = SimpleNamespace(
            model_id="qwen3.5:2b",
            label="Qwen3.5 2B",
            supports_thinking=True,
        )
        package_module = SimpleNamespace(
            supported_models=lambda: (model,),
            Qwen35ModelInstaller=CancellableInstaller,
        )
        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(data_directory=directory)
            host._loaded_release = SimpleNamespace(package_module=package_module)
            host._release_manager = SimpleNamespace(model_install_available=False)
            host._model_install_id = model.model_id
            host._model_operation = self.PendingOperation()
            host._model_cancel_event = threading.Event()
            host._model_progress = {"stage": "Downloading", "current": 5, "total": 10}

            result = await host.cancel_model_install(model.model_id)
            status = host.model_settings()["models"][0]

            self.assertEqual(
                result,
                {"id": model.model_id, "cancellationRequested": True},
            )
            self.assertTrue(host._model_cancel_event.is_set())
            self.assertTrue(status["downloadCancelRequested"])
            self.assertFalse(status["downloadCancelAvailable"])
            self.assertEqual(status["downloadStage"], "Stopping model download")
            host._model_operation.cancel()

    async def test_model_download_cancel_rejects_releases_without_support(self):
        class LegacyInstaller:
            def install_model(self, _model_id, *, progress=None):
                return None

        model = SimpleNamespace(
            model_id="qwen3.5:2b",
            label="Qwen3.5 2B",
            supports_thinking=True,
        )
        package_module = SimpleNamespace(
            supported_models=lambda: (model,),
            Qwen35ModelInstaller=LegacyInstaller,
        )
        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(data_directory=directory)
            host._loaded_release = SimpleNamespace(package_module=package_module)
            host._release_manager = SimpleNamespace(model_install_available=False)
            host._model_install_id = model.model_id
            host._model_operation = self.PendingOperation()
            host._model_cancel_event = threading.Event()
            host._model_progress = {"stage": "Downloading", "current": 5, "total": 10}

            with self.assertRaisesRegex(LumiHostError, "cannot cancel"):
                await host.cancel_model_install(model.model_id)
            self.assertFalse(host._model_cancel_event.is_set())
            host._model_operation.cancel()

    async def test_model_cancel_event_reaches_installer_dependency_preparation(self):
        class CancellableInstaller:
            def install_model(self, _model_id, *, progress=None, cancel_event=None):
                return None

        model = SimpleNamespace(
            model_id="qwen3.5:2b",
            label="Qwen3.5 2B",
            supports_thinking=True,
        )
        package_module = SimpleNamespace(
            supported_models=lambda: (model,),
            Qwen35ModelInstaller=CancellableInstaller,
        )
        cancellation = threading.Event()

        class CancellingReleaseManager:
            received_event = None

            async def install_model_dependencies(self, *, cancel_event=None):
                self.received_event = cancel_event
                cancel_event.set()
                raise RuntimeError("cancelled while preparing installer")

        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(data_directory=directory)
            manager = CancellingReleaseManager()
            host._release_manager = manager
            host._loaded_release = SimpleNamespace(package_module=package_module)
            host._model_install_id = model.model_id
            host._model_cancel_event = cancellation

            await host._install_model(model.model_id)

        self.assertIs(manager.received_event, cancellation)
        self.assertTrue(cancellation.is_set())
        self.assertEqual(host._model_cancelled_id, model.model_id)
        self.assertNotIn(model.model_id, host._model_errors)

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
        )
        artifacts = {"qwen3.5:2b": object()}
        limits = {
            "idleUnloadSeconds": 300,
            "maxContextTokens": 8192,
            "maxOutputTokens": 2048,
        }

        runtime = _create_runtime_adapter(module, artifacts, limits, "llama-cpp-python")

        self.assertIsInstance(runtime, Runtime)
        self.assertIsInstance(runtime.configuration, Config)
        self.assertEqual(
            runtime.configuration.values,
            {
                "model_artifacts": artifacts,
                "idle_unload_seconds": 300,
                "max_context_tokens": 8192,
                "max_output_tokens": 2048,
                "acceleration_mode": "automatic",
            },
        )

    def test_legacy_runtime_falls_back_to_cpu_for_gpu_modes(self):
        class LegacyConfig:
            def __init__(
                self,
                model_artifacts,
                idle_unload_seconds,
                max_context_tokens,
                max_output_tokens,
            ):
                self.model_artifacts = model_artifacts

        class Runtime:
            def __init__(self, configuration):
                self.configuration = configuration

        module = SimpleNamespace(
            LlamaCppConfig=LegacyConfig, LlamaCppChatRuntime=Runtime
        )
        runtime = _create_runtime_adapter(
            module,
            {},
            {
                "idleUnloadSeconds": 300,
                "maxContextTokens": 8192,
                "maxOutputTokens": 2048,
            },
            "llama-cpp-python",
            "automatic",
        )
        self.assertIsInstance(runtime, Runtime)
        gpu_preferred = _create_runtime_adapter(
            module,
            {},
            {
                "idleUnloadSeconds": 300,
                "maxContextTokens": 8192,
                "maxOutputTokens": 2048,
            },
            "llama-cpp-python",
            "gpu_preferred",
        )
        self.assertIsInstance(gpu_preferred, Runtime)

    def test_runtime_adapter_rejects_releases_without_a_local_backend(self):
        with self.assertRaisesRegex(LumiHostError, "local runtime"):
            _create_runtime_adapter(SimpleNamespace(), {}, {}, "onnxruntime-genai")

    def test_runtime_adapter_uses_only_llama_cpp_backend(self):
        class LlamaConfig:
            def __init__(self, **_values):
                self.backend = "llama-cpp-python"

        class Runtime:
            def __init__(self, configuration):
                self.configuration = configuration

        module = SimpleNamespace(
            LlamaCppConfig=LlamaConfig,
            LlamaCppChatRuntime=Runtime,
        )
        runtime = _create_runtime_adapter(
            module,
            {},
            {
                "idleUnloadSeconds": 300,
                "maxContextTokens": 8192,
                "maxOutputTokens": 2048,
            },
            "llama-cpp-python",
        )

        self.assertEqual(runtime.configuration.backend, "llama-cpp-python")

    def test_agent_limits_apply_saved_context_and_output_values(self):
        class AgentLimits:
            def __init__(self, **values):
                self.values = values

        result = _create_agent_limits(
            SimpleNamespace(AgentLimits=AgentLimits),
            {"maxContextTokens": 16_384, "maxOutputTokens": 4_096},
        )

        self.assertEqual(
            result.values,
            {"context_size": 16_384, "output_tokens": 4_096},
        )

    async def test_rebuild_passes_saved_agent_limits_to_embedded_service(self):
        class AgentLimits:
            def __init__(self, **values):
                self.values = values

        class Runtime:
            def __init__(self, configuration):
                self.configuration = configuration

        class VerifiedModelArtifact:
            def __init__(self, *values):
                self.values = values

        service = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        create_service = Mock(return_value=service)
        loaded = SimpleNamespace(
            runtime_module=SimpleNamespace(
                LlamaCppConfig=lambda **values: SimpleNamespace(values=values),
                LlamaCppChatRuntime=Runtime,
                VerifiedModelArtifact=VerifiedModelArtifact,
            ),
            manifest=SimpleNamespace(
                runtime_dependencies=(SimpleNamespace(distribution="llama-cpp-python"),)
            ),
            package_module=SimpleNamespace(),
            create_embedded_service=create_service,
        )
        model_option = {
            "id": "qwen3.5:2b",
            "label": "Qwen3.5 2B",
            "supportsThinking": True,
        }
        model_module = SimpleNamespace(
            QwenModelOption=lambda **values: SimpleNamespace(**values),
            ModelCatalog=lambda *values: values,
        )
        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(data_directory=directory)
            host._loaded_release = loaded
            host._settings["enabled"] = True
            host._settings["models"] = {
                "qwen3.5:2b": {
                    "directory": str(Path(directory) / "model"),
                    "manifestSha256": "0" * 64,
                    "enabled": True,
                }
            }
            host._settings["defaultModel"] = "qwen3.5:2b"
            host._settings["gpuMode"] = "gpu_preferred"
            host._settings["limits"].update(
                {"maxContextTokens": 16_384, "maxOutputTokens": 4_096}
            )
            host._model_catalog = Mock(return_value=[model_option])
            host._model_artifact = Mock(
                return_value=host._settings["models"]["qwen3.5:2b"]
            )
            host._create_tool_registry = Mock(return_value=object())

            def import_module(name):
                if name == "lumi.model_catalog":
                    return model_module
                if name == "lumi.agent":
                    return SimpleNamespace(AgentLimits=AgentLimits)
                raise AssertionError(f"unexpected Lumi module import: {name}")

            with patch(
                "app.lumi_host.importlib.import_module",
                side_effect=import_module,
            ):
                await host._rebuild_service()

        agent_limits = create_service.call_args.kwargs["agent_limits"]
        self.assertEqual(
            agent_limits.values,
            {"context_size": 16_384, "output_tokens": 4_096},
        )
        service.start.assert_awaited_once()
        self.assertEqual(
            host._runtime.configuration.values["acceleration_mode"], "gpu_preferred"
        )

    async def test_gpu_mode_persists_across_host_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            first_host = LumiHost(data_directory=directory)
            await first_host.update_runtime_settings(gpu_mode="cpu_only")

            restarted_host = LumiHost(data_directory=directory)

        self.assertEqual(restarted_host.model_settings()["gpuMode"], "cpu_only")
        self.assertEqual(
            restarted_host.model_settings()["gpuAcceleration"]["mode"], "cpu_only"
        )

    async def test_gpu_status_is_sanitized_for_admin_response(self):
        class Config:
            def __init__(self, **_values):
                pass

        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(data_directory=directory)
            host._loaded_release = SimpleNamespace(
                runtime_module=SimpleNamespace(LlamaCppConfig=Config),
                package_module=SimpleNamespace(supported_models=lambda: ()),
            )
            host._model_catalog = Mock(return_value=[])
            host._runtime = SimpleNamespace(
                acceleration_status=lambda: {
                    "state": "cpu_fallback",
                    "selectedBackend": "cpu",
                    "selectedDevice": "Local GPU",
                    "offloadedLayers": 0,
                    "totalLayers": 32,
                    "fallbackReason": "Failed to load C:\\private\\driver.dll",
                }
            )

            gpu_status = host.model_settings()["gpuAcceleration"]

        self.assertEqual(gpu_status["state"], "cpu_fallback")
        self.assertEqual(gpu_status["selectedBackend"], "cpu")
        self.assertEqual(gpu_status["offloadedLayers"], 0)
        self.assertEqual(
            gpu_status["fallbackReason"],
            "GPU acceleration failed; using the CPU fallback.",
        )

    async def test_runtime_settings_reject_active_conversation_limit_above_service_cap(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(data_directory=directory)

            with self.assertRaisesRegex(LumiHostError, "outside its supported range"):
                await host.update_runtime_settings(
                    limits={"maxActiveConversations": 2_049}
                )

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

    def test_sync_release_list_contains_only_stable_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(
                data_directory=directory,
                release_manager_factory=FakeReleaseManager,
            )

            releases = host.list_releases_sync(host.release_manager)

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
            self.assertTrue(host.status()["integration"]["installed"])
            self.assertFalse(host.status()["integration"]["loaded"])

    def test_disabled_release_remains_reported_as_installed_after_host_restart(self):
        release_manager = FakeReleaseManager()
        release_manager.installed_tags.add("v1.2.3")
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "lumi" / "integration.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps(
                    {"schemaVersion": 1, "enabled": False, "releaseTag": "v1.2.3"}
                ),
                encoding="utf-8",
            )
            host = LumiHost(
                data_directory=directory,
                release_manager_factory=lambda *_args: release_manager,
            )

            integration = host.status()["integration"]

        self.assertFalse(integration["enabled"])
        self.assertTrue(integration["installed"])
        self.assertFalse(integration["loaded"])

    async def test_remove_integration_keeps_conversations_and_model_files(self):
        with tempfile.TemporaryDirectory() as directory:
            host = LumiHost(
                data_directory=directory,
                release_manager_factory=FakeReleaseManager,
            )
            await host.enable("v1.2.3")
            await host._operation
            conversations = host.model_database_path
            conversations.parent.mkdir(parents=True, exist_ok=True)
            conversations.write_bytes(b"saved conversations")
            model_directory = host.data_directory / "models" / "qwen3.5-2b"
            model_directory.mkdir(parents=True)
            model_file = model_directory / "weights.safetensors"
            model_file.write_bytes(b"downloaded model")
            host._settings["models"]["qwen3.5:2b"] = {
                "directory": str(model_directory.resolve()),
                "manifestSha256": "0" * 64,
                "sizeBytes": model_file.stat().st_size,
                "enabled": False,
            }
            host._settings["defaultModel"] = "qwen3.5:2b"
            host._save_settings()

            await host.remove_installation()

            integration = host.status()["integration"]
            self.assertFalse(integration["enabled"])
            self.assertFalse(integration["installed"])
            self.assertFalse(integration["loaded"])
            self.assertIsNone(integration["releaseTag"])
            self.assertEqual(conversations.read_bytes(), b"saved conversations")
            self.assertEqual(model_file.read_bytes(), b"downloaded model")
            self.assertFalse(host._release_manager.installed_tags)
            settings = json.loads(host.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(
                settings["models"]["qwen3.5:2b"]["directory"],
                str(model_directory.resolve()),
            )
            self.assertEqual(settings["defaultModel"], "qwen3.5:2b")

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

            self.assertEqual(host._release_manager.enable_calls, ["v1.2.3"])
            self.assertTrue(host.status()["integration"]["enabled"])
            self.assertTrue(host._operation.done())

            await host.disable()

    async def test_saved_integration_startup_waits_for_release_activation(self):
        class DelayedReleaseManager(FakeReleaseManager):
            def __init__(self, *_args):
                super().__init__()
                self.activation_started = threading.Event()
                self.continue_activation = threading.Event()

            async def enable(self, tag: str):
                self.activation_started.set()
                await asyncio.to_thread(self.continue_activation.wait)
                return await super().enable(tag)

        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "lumi" / "integration.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps(
                    {"schemaVersion": 1, "enabled": True, "releaseTag": "v1.2.3"}
                ),
                encoding="utf-8",
            )
            manager = DelayedReleaseManager()
            host = LumiHost(
                data_directory=directory,
                release_manager_factory=lambda *_args: manager,
            )
            host._rebuild_service = AsyncMock()

            startup = asyncio.create_task(host.load_saved_integration())
            self.assertTrue(await asyncio.to_thread(manager.activation_started.wait, 5))
            self.assertFalse(startup.done())
            self.assertEqual(host.status()["integration"]["state"], "installing")

            manager.continue_activation.set()
            await asyncio.wait_for(startup, timeout=5)

            self.assertTrue(host._operation.done())
            self.assertTrue(host.status()["integration"]["enabled"])
            self.assertEqual(manager.enable_calls, ["v1.2.3"])

            await host.disable()

    async def test_restart_restores_persisted_model_settings_and_conversations(self):
        model_id = "qwen3.5:2b"
        with tempfile.TemporaryDirectory() as directory:
            metadata_directory = Path(directory) / "metadata"
            lumi_directory = metadata_directory / "lumi"
            model_directory = lumi_directory / "models" / "qwen3.5-2b"
            model_directory.mkdir(parents=True)
            manifest_bytes = json.dumps({"model": model_id}).encode("utf-8")
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            (model_directory / "lumi-model-manifest.json").write_bytes(manifest_bytes)
            model_file = model_directory / "Qwen35.gguf"
            model_file.write_bytes(b"verified persisted model")
            conversations = lumi_directory / "conversations.sqlite3"
            conversation_bytes = b"persisted conversation database"
            conversations.write_bytes(conversation_bytes)

            class Installer:
                model_install_calls = []

                def __init__(self, _root):
                    pass

                def list_models(self):
                    return (
                        SimpleNamespace(
                            model_id=model_id,
                            installed=True,
                            directory=str(model_directory.resolve()),
                            manifest_sha256=manifest_sha256,
                            size_bytes=model_file.stat().st_size,
                        ),
                    )

                def install_model(self, *_args, **_kwargs):
                    self.model_install_calls.append(model_id)
                    raise AssertionError("startup must not reinstall a saved model")

            package_module = SimpleNamespace(
                __file__=str(
                    lumi_directory / "release" / "package" / "lumi" / "__init__.py"
                ),
                Qwen35ModelInstaller=Installer,
                supported_models=lambda: (
                    SimpleNamespace(
                        model_id=model_id,
                        label="Qwen3.5 2B",
                        supports_thinking=True,
                    ),
                ),
            )

            class PersistedReleaseManager(FakeReleaseManager):
                def __init__(self, *_args):
                    super().__init__()
                    self.installed_tags.add("v1.2.3")
                    self.package_downloads = 0

                def loaded_release(self, tag):
                    return SimpleNamespace(
                        tag=tag,
                        package_module=package_module,
                        manifest=SimpleNamespace(installer_dependencies=()),
                    )

                async def enable(self, tag: str):
                    self.enable_calls.append(tag)
                    return self.loaded_release(tag)

            model_record = {
                "directory": str(model_directory.resolve()),
                "manifestSha256": manifest_sha256,
                "sizeBytes": model_file.stat().st_size,
                "enabled": True,
            }
            settings_path = lumi_directory / "integration.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "enabled": True,
                        "releaseTag": "v1.2.3",
                        "defaultModel": model_id,
                        "defaultThinking": True,
                        "gpuMode": "cpu_only",
                        "models": {model_id: model_record},
                    }
                ),
                encoding="utf-8",
            )
            manager = PersistedReleaseManager()
            host = LumiHost(
                data_directory=metadata_directory,
                release_manager_factory=lambda *_args: manager,
            )
            restored_store_paths: list[Path] = []

            async def rebuild_service():
                restored_store_paths.append(host.model_database_path)
                host._service = SimpleNamespace(
                    persisted_conversations=host.model_database_path.read_bytes()
                )
                host._runtime = object()

            host._rebuild_service = AsyncMock(side_effect=rebuild_service)

            await host.load_saved_integration()

            integration = host.status()["integration"]
            restored_model = host.model_settings()["models"][0]
            self.assertTrue(integration["enabled"])
            self.assertTrue(integration["loaded"])
            self.assertEqual(integration["state"], "ready")
            self.assertTrue(restored_model["installed"])
            self.assertTrue(restored_model["enabled"])
            self.assertEqual(host._settings["defaultModel"], model_id)
            self.assertTrue(host._settings["defaultThinking"])
            self.assertEqual(host._settings["gpuMode"], "cpu_only")
            self.assertEqual(manager.enable_calls, ["v1.2.3"])
            self.assertEqual(manager.package_downloads, 0)
            self.assertEqual(Installer.model_install_calls, [])
            self.assertEqual(restored_store_paths, [conversations])
            self.assertEqual(host.service.persisted_conversations, conversation_bytes)
            self.assertEqual(model_file.read_bytes(), b"verified persisted model")
            self.assertEqual(conversations.read_bytes(), conversation_bytes)

            await host.disable()

    async def test_saved_model_must_pass_installer_verification_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_id = "qwen3.5:2b"
            model_directory = root / "metadata" / "lumi" / "models" / "qwen3.5-2b"
            model_directory.mkdir(parents=True)
            manifest_bytes = b'{"model":"qwen3.5:2b"}'
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            (model_directory / "lumi-model-manifest.json").write_bytes(manifest_bytes)
            (model_directory / "Qwen35.gguf").write_bytes(b"verified-test-model")
            package_directory = root / "release" / "site-packages" / "lumi"
            model_option = SimpleNamespace(
                model_id=model_id,
                installed=False,
                directory=None,
                manifest_sha256=None,
                size_bytes=0,
            )

            class Installer:
                def __init__(self, _root):
                    pass

                def list_models(self):
                    return (model_option,)

                def remove_model(self, _model_id):
                    return True

            package_module = SimpleNamespace(
                __file__=str(package_directory / "__init__.py"),
                Qwen35ModelInstaller=Installer,
                supported_models=lambda: (
                    SimpleNamespace(
                        model_id=model_id,
                        label="Qwen3.5 2B",
                        supports_thinking=True,
                    ),
                ),
            )

            class PersistedReleaseManager(FakeReleaseManager):
                def loaded_release(self, tag):
                    return SimpleNamespace(
                        tag=tag,
                        package_module=package_module,
                        manifest=SimpleNamespace(installer_dependencies=()),
                    )

            settings_path = root / "metadata" / "lumi" / "integration.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "enabled": True,
                        "releaseTag": "v1.2.3",
                        "defaultModel": model_id,
                        "models": {
                            model_id: {
                                "directory": str(model_directory.resolve()),
                                "manifestSha256": manifest_sha256,
                                "sizeBytes": 20,
                                "enabled": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            host = LumiHost(
                data_directory=root / "metadata",
                release_manager_factory=PersistedReleaseManager,
            )
            host._rebuild_service = AsyncMock()

            await host.load_saved_integration()
            await host._operation

            self.assertTrue(host.status()["integration"]["enabled"])
            model_status = host.model_settings()["models"][0]
            self.assertTrue(model_status["installed"])
            self.assertFalse(model_status["enabled"])
            self.assertIn("integrity", model_status["downloadError"])
            self.assertEqual(host.public_models()["models"], [])
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertFalse(saved["models"][model_id]["enabled"])

    async def test_modified_model_files_stop_being_selectable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_id = "qwen3.5:2b"
            model_directory = root / "metadata" / "lumi" / "models" / "qwen3.5-2b"
            model_directory.mkdir(parents=True)
            manifest_bytes = b'{"model":"qwen3.5:2b"}'
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            (model_directory / "lumi-model-manifest.json").write_bytes(manifest_bytes)
            model_file = model_directory / "Qwen35.gguf"
            model_file.write_bytes(b"verified-test-model")
            package_directory = root / "release" / "site-packages" / "lumi"
            model_option = SimpleNamespace(
                model_id=model_id,
                installed=True,
                directory=str(model_directory.resolve()),
                manifest_sha256=manifest_sha256,
                size_bytes=20,
            )

            class Installer:
                def __init__(self, _root):
                    pass

                def list_models(self):
                    return (model_option,)

                def remove_model(self, _model_id):
                    return True

            package_module = SimpleNamespace(
                __file__=str(package_directory / "__init__.py"),
                Qwen35ModelInstaller=Installer,
                supported_models=lambda: (
                    SimpleNamespace(
                        model_id=model_id,
                        label="Qwen3.5 2B",
                        supports_thinking=True,
                    ),
                ),
            )

            class PersistedReleaseManager(FakeReleaseManager):
                def loaded_release(self, tag):
                    return SimpleNamespace(
                        tag=tag,
                        package_module=package_module,
                        manifest=SimpleNamespace(installer_dependencies=()),
                    )

            settings_path = root / "metadata" / "lumi" / "integration.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "enabled": True,
                        "releaseTag": "v1.2.3",
                        "defaultModel": model_id,
                        "models": {
                            model_id: {
                                "directory": str(model_directory.resolve()),
                                "manifestSha256": manifest_sha256,
                                "sizeBytes": 20,
                                "enabled": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            host = LumiHost(
                data_directory=root / "metadata",
                release_manager_factory=PersistedReleaseManager,
            )
            host._rebuild_service = AsyncMock()

            await host.load_saved_integration()
            await host._operation
            self.assertTrue(host.model_settings()["models"][0]["installed"])

            model_file.write_bytes(b"corrupt-test-model")

            models = host.model_settings()["models"]
            self.assertTrue(models[0]["installed"])
            self.assertFalse(models[0]["enabled"])
            self.assertIn("integrity", models[0]["downloadError"])
            self.assertIsNone(host._settings["defaultModel"])
            self.assertEqual(host.public_models()["models"], [])
            self.assertTrue(await host.remove_model(model_id))
            self.assertNotIn(model_id, host._settings["models"])

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

    def test_web_research_tools_are_built_without_a_search_url(self):
        fake_tool = lambda *_args, **_kwargs: object()
        search_tool = object()
        open_tool = object()
        web_module = SimpleNamespace(
            WebResearchConfig=Mock(return_value="default-search-config"),
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

        self.assertEqual(len(registry), 8)
        self.assertIn("lumi.web_research", imported)
        web_module.WebResearchConfig.assert_called_once_with(searxng_url=None)
        web_module.build_web_research_tools.assert_called_once_with(
            "default-search-config"
        )

    def test_older_web_research_release_can_return_no_default_search_tools(self):
        fake_tool = lambda *_args, **_kwargs: object()
        web_module = SimpleNamespace(
            WebResearchConfig=Mock(return_value="default-search-config"),
            build_web_research_tools=Mock(return_value=()),
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
            with patch(
                "app.lumi_host.importlib.import_module",
                side_effect=lambda name: modules[name],
            ):
                registry = host._create_tool_registry(SimpleNamespace())

        self.assertEqual(len(registry), 6)
        web_module.build_web_research_tools.assert_called_once_with(
            "default-search-config"
        )

    def test_web_research_tools_use_the_configured_search_url(self):
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
