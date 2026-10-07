"""Lazy in-process host for an explicitly installed Lumi release.

The Orchestrator remains the authentication and catalog boundary. Lumi's package is
loaded from its own verified GitHub release only after an administrator enables the
integration; this module never imports Lumi at Orchestrator startup by itself.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.lumi_catalog import LumiCatalogAdapter
from app.lumi_release import LumiReleaseCompatibilityError
from app.paths import metadata_directory
from version import __version__ as ORCHESTRATOR_VERSION

logger = logging.getLogger("zenstream.lumi")

_STABLE_RELEASE_TAG = re.compile(r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_DEFAULT_LIMITS = {
    "idleUnloadSeconds": 300,
    "maxContextTokens": 8192,
    "maxOutputTokens": 2048,
    "maxConcurrentChats": 1,
    "maxActiveConversations": 128,
}


class LumiHostError(RuntimeError):
    """A safe, user-facing Lumi host configuration or lifecycle error."""


class LumiHost:
    """Own Lumi's persisted host settings, verified package, and embedded service."""

    def __init__(
        self,
        *,
        data_directory: str | Path | None = None,
        orchestrator_version: str = ORCHESTRATOR_VERSION,
        catalog: Any | None = None,
        release_manager_factory=None,
    ) -> None:
        self.data_directory = Path(data_directory or metadata_directory()) / "lumi"
        self.orchestrator_version = orchestrator_version
        self.catalog = catalog
        self._release_manager_factory = release_manager_factory
        self._release_manager = None
        self._loaded_release = None
        self._service = None
        self._runtime = None
        self._operation: asyncio.Task | None = None
        self._model_operation: asyncio.Task | None = None
        self._model_install_id: str | None = None
        self._model_progress: dict[str, Any] | None = None
        self._model_errors: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._disable_requested = False
        self._settings = self._read_settings()
        self._state = "ready" if self._settings["enabled"] else "disabled"
        self._error: str | None = None

    @property
    def settings_path(self) -> Path:
        return self.data_directory / "integration.json"

    @property
    def model_database_path(self) -> Path:
        return self.data_directory / "conversations.sqlite3"

    @property
    def release_manager(self):
        if self._release_manager is None:
            factory = self._release_manager_factory
            if factory is None:
                from app.lumi_release import LumiReleaseManager

                factory = LumiReleaseManager
            self._release_manager = factory(
                self.data_directory,
                self.orchestrator_version,
            )
        return self._release_manager

    async def list_releases(self) -> list[dict[str, str | None]]:
        values = await self.release_manager.list_published_releases(limit=20)
        if isinstance(values, Mapping):
            values = values.get("releases", [])
        releases: list[dict[str, str | None]] = []
        if not isinstance(values, (tuple, list)):
            return releases
        for value in values:
            if isinstance(value, Mapping):
                tag = value.get("tag") or value.get("tag_name")
                released_at = value.get("releasedAt") or value.get("released_at")
            else:
                tag = getattr(value, "tag", None) or getattr(value, "tag_name", None)
                released_at = getattr(value, "released_at", None)
            if (
                isinstance(tag, str)
                and _STABLE_RELEASE_TAG.fullmatch(tag)
                and (released_at is None or isinstance(released_at, str))
            ):
                releases.append({"tag": tag, "releasedAt": released_at})
        return releases

    async def enable(self, tag: str) -> None:
        if not isinstance(tag, str) or not _STABLE_RELEASE_TAG.fullmatch(tag):
            raise LumiHostError("Choose a published stable Lumi release.")
        async with self._lock:
            if self.restart_required:
                raise LumiHostError("Restart Orchestrator before enabling Lumi again.")
            if self._operation is not None and not self._operation.done():
                raise LumiHostError("A Lumi integration operation is already running.")
            self._disable_requested = False
            self._settings["releaseTag"] = tag
            self._settings["enabled"] = False
            self._state = "installing"
            self._error = None
            self._save_settings()
            self._operation = asyncio.create_task(self._install_and_activate(tag))

    async def _install_and_activate(self, tag: str) -> None:
        try:
            loaded = await self.release_manager.enable(tag)
            if self._disable_requested:
                return
            self._loaded_release = loaded
            self._refresh_model_catalog()
            self._settings["enabled"] = True
            self._settings["releaseTag"] = loaded.tag
            self._save_settings()
            await self._rebuild_service()
            if self._disable_requested:
                return
            self._state = "ready"
            self._error = None
        except asyncio.CancelledError:
            if not self._disable_requested:
                self._state = "error"
                self._error = "installation_cancelled"
            raise
        except Exception as error:
            logger.exception("Lumi integration installation failed")
            self._loaded_release = None
            self._service = None
            self._runtime = None
            self._settings["enabled"] = False
            self._save_settings()
            self._state = "disabled" if self._disable_requested else "error"
            if self._disable_requested:
                self._error = None
            elif isinstance(error, LumiReleaseCompatibilityError):
                self._error = str(error)[:180]
            else:
                self._error = type(error).__name__[:80]
            if not self._disable_requested:
                try:
                    await self.release_manager.disable()
                except Exception:
                    logger.warning("Lumi activation cleanup failed", exc_info=True)

    async def disable(self) -> None:
        async with self._lock:
            if self._model_operation is not None and not self._model_operation.done():
                raise LumiHostError(
                    "Wait for the current Lumi model installation to finish."
                )
            self._disable_requested = True
            self._settings["enabled"] = False
            self._state = "disabled"
            self._error = None
            self._save_settings()
            operation = self._operation
            manager = self._release_manager
        if operation is not None and not operation.done():
            try:
                await operation
            except asyncio.CancelledError:
                pass
        async with self._lock:
            self._settings["enabled"] = False
            self._state = "disabled"
            self._error = None
            self._service = None
            self._runtime = None
            self._loaded_release = None
            self._save_settings()
        if manager is not None:
            await manager.disable()

    async def load_saved_integration(self) -> None:
        """Restore only a previously enabled release; a clean install stays dormant."""

        if not self._settings["enabled"]:
            self._state = "disabled"
            return
        tag = self._settings.get("releaseTag")
        if not isinstance(tag, str) or not _STABLE_RELEASE_TAG.fullmatch(tag):
            self._settings["enabled"] = False
            self._state = "error"
            self._error = "saved_release_invalid"
            self._save_settings()
            return
        self._state = "installing"
        self._operation = asyncio.create_task(self._install_and_activate(tag))

    async def shutdown(self) -> None:
        model_operation = self._model_operation
        if model_operation is not None and not model_operation.done():
            try:
                await asyncio.wait_for(asyncio.shield(model_operation), timeout=5)
            except TimeoutError:
                logger.warning("Lumi model installation did not stop before shutdown")
                return
            except Exception:
                pass
        operation = self._operation
        if operation is not None and not operation.done():
            operation.cancel()
            try:
                await asyncio.wait_for(operation, timeout=5)
            except (asyncio.CancelledError, TimeoutError):
                pass
            except Exception:
                pass
        manager = self._release_manager
        self._service = None
        self._runtime = None
        self._loaded_release = None
        if manager is not None:
            try:
                await asyncio.wait_for(manager.disable(), timeout=5)
            except Exception:
                logger.warning("Lumi integration did not stop cleanly", exc_info=True)

    @property
    def service(self):
        return self._service

    @property
    def restart_required(self) -> bool:
        return bool(getattr(self._release_manager, "restart_required", False))

    @property
    def web_search_url(self) -> str | None:
        value = self._settings.get("webSearchUrl")
        return value if isinstance(value, str) else None

    def configure_web_search(self, url: str | None) -> None:
        if url is not None and (not isinstance(url, str) or len(url) > 2_048):
            raise LumiHostError("The web search endpoint is invalid.")
        self._settings["webSearchUrl"] = url
        self._save_settings()

    def model_settings(self) -> dict[str, Any]:
        configured = self._settings["models"]
        models = []
        for option in self._model_catalog():
            model_id = option["id"]
            record = configured.get(model_id, {})
            installed = self._model_artifact(model_id) is not None
            enabled = bool(installed and record.get("enabled", False))
            is_downloading = self._model_install_id == model_id and (
                self._model_operation is not None and not self._model_operation.done()
            )
            progress = self._model_progress if is_downloading else None
            models.append(
                {
                    "id": model_id,
                    "label": option["label"],
                    "sizeBytes": record.get("sizeBytes") if installed else None,
                    "installed": installed,
                    "enabled": enabled,
                    "downloading": is_downloading,
                    "downloadProgress": (
                        progress["current"] * 100 / progress["total"]
                        if progress and progress["total"] > 0
                        else None
                    ),
                    "downloadStage": progress["stage"] if progress else None,
                    "downloadError": self._model_errors.get(model_id),
                    "supportsThinking": option["supportsThinking"],
                    "isDefault": enabled
                    and self._settings.get("defaultModel") == model_id,
                }
            )
        default = self._settings.get("defaultModel")
        if not any(item["id"] == default and item["enabled"] for item in models):
            default = next((item["id"] for item in models if item["enabled"]), None)
        return {
            "models": models,
            "defaultModel": default,
            "defaultThinking": bool(self._settings.get("defaultThinking", False)),
            "limits": dict(self._settings["limits"]),
            "modelInstallAvailable": bool(
                self._loaded_release
                and getattr(self.release_manager, "model_install_available", False)
            ),
        }

    def public_models(self) -> dict[str, Any]:
        settings = self.model_settings()
        return {
            "models": [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "supportsThinking": item["supportsThinking"],
                }
                for item in settings["models"]
                if item["enabled"]
            ],
            "defaultModel": settings["defaultModel"] or "",
            "defaultThinking": settings["defaultThinking"],
        }

    async def set_model_choice(
        self,
        model_id: str,
        *,
        enabled: bool,
        is_default: bool = False,
    ) -> None:
        async with self._lock:
            if self._loaded_release is None or not self._settings["enabled"]:
                raise LumiHostError("Enable Lumi before changing model access.")
            if not self._is_supported_model(model_id):
                raise LumiHostError(
                    "That Qwen3.5 model is not supported by the selected release."
                )
            if self._model_operation is not None and not self._model_operation.done():
                raise LumiHostError(
                    "Wait for the current Lumi model installation to finish."
                )
            if self._model_artifact(model_id) is None:
                raise LumiHostError("Download the model before enabling it.")
            models = self._settings["models"]
            record = models[model_id]
            current_default = self._settings.get("defaultModel")
            if is_default:
                if not enabled:
                    raise LumiHostError("The default Lumi model must remain enabled.")
                current_default = model_id
            if current_default == model_id and not enabled:
                raise LumiHostError(
                    "Choose another default model before disabling this one."
                )
            enabled_others = any(
                candidate != model_id
                and item.get("enabled")
                and self._model_artifact(candidate) is not None
                for candidate, item in models.items()
            )
            if not enabled and not enabled_others:
                raise LumiHostError(
                    "At least one installed Lumi model must remain enabled."
                )
            previous = (record.get("enabled"), self._settings.get("defaultModel"))
            record["enabled"] = bool(enabled)
            self._settings["defaultModel"] = current_default
            self._save_settings()
            try:
                if self._loaded_release is not None:
                    await self._rebuild_service()
            except Exception:
                record["enabled"], self._settings["defaultModel"] = previous
                self._save_settings()
                raise

    async def update_runtime_settings(
        self,
        *,
        default_thinking: bool | None = None,
        limits: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            if default_thinking is not None and type(default_thinking) is not bool:
                raise LumiHostError("The default thinking setting must be a boolean.")
            proposed_limits = dict(self._settings["limits"])
            if limits is not None:
                if not isinstance(limits, Mapping) or set(limits) - set(
                    _DEFAULT_LIMITS
                ):
                    raise LumiHostError("The Lumi runtime limits are invalid.")
                bounds = {
                    "idleUnloadSeconds": (0, 86_400),
                    "maxContextTokens": (512, 32_768),
                    "maxOutputTokens": (64, 8_192),
                    "maxConcurrentChats": (1, 8),
                    "maxActiveConversations": (1, 10_000),
                }
                for key, value in limits.items():
                    minimum, maximum = bounds[key]
                    if type(value) is not int or not minimum <= value <= maximum:
                        raise LumiHostError(
                            f"The Lumi {key} limit is outside its supported range."
                        )
                    proposed_limits[key] = value
            if (
                proposed_limits["maxOutputTokens"] + 256
                >= proposed_limits["maxContextTokens"]
            ):
                raise LumiHostError(
                    "Max context must leave at least 256 tokens beyond max output."
                )
            previous_thinking = self._settings["defaultThinking"]
            previous_limits = dict(self._settings["limits"])
            if default_thinking is not None:
                self._settings["defaultThinking"] = default_thinking
            self._settings["limits"] = proposed_limits
            self._save_settings()
            try:
                if self._loaded_release is not None:
                    await self._rebuild_service()
            except Exception:
                self._settings["defaultThinking"] = previous_thinking
                self._settings["limits"] = previous_limits
                self._save_settings()
                raise
            return self.model_settings()

    async def start_model_install(self, model_id: str) -> None:
        async with self._lock:
            if not self._settings["enabled"] or self._loaded_release is None:
                raise LumiHostError("Enable a Lumi release before downloading a model.")
            if not self._is_supported_model(model_id):
                raise LumiHostError(
                    "That Qwen3.5 model is not supported by the selected release."
                )
            if self._operation is not None and not self._operation.done():
                raise LumiHostError("Wait for the Lumi release installation to finish.")
            if self._model_operation is not None and not self._model_operation.done():
                raise LumiHostError(
                    "Another Lumi model installation is already running."
                )
            if self._model_artifact(model_id) is not None:
                raise LumiHostError("That Lumi model is already installed.")
            self._model_install_id = model_id
            self._model_progress = {
                "stage": "Preparing model installer",
                "current": 0,
                "total": 10_000,
            }
            self._model_errors.pop(model_id, None)
            self._model_operation = asyncio.create_task(self._install_model(model_id))

    async def _install_model(self, model_id: str) -> None:
        previous_models: dict[str, Any] | None = None
        previous_default: str | None = None
        try:
            loaded = self._loaded_release
            if loaded is None:
                raise LumiHostError("Enable a Lumi release before downloading a model.")
            installer_paths = await self.release_manager.install_model_dependencies()
            self._set_model_progress(
                model_id,
                {
                    "stage": "Downloading and converting model",
                    "current": 100,
                    "total": 10_000,
                },
            )
            from app.foreground import run_control

            loop = asyncio.get_running_loop()

            def report_progress(event: object) -> None:
                stage = getattr(event, "stage", "working")
                current = getattr(event, "current", 0)
                total = getattr(event, "total", 10_000)
                snapshot = {
                    "stage": _safe_model_stage(stage),
                    "current": current,
                    "total": total,
                }
                loop.call_soon_threadsafe(self._set_model_progress, model_id, snapshot)

            artifact = await run_control(
                _install_qwen_model,
                loaded.package_module,
                self.data_directory / "models",
                model_id,
                installer_paths,
                report_progress,
            )
            previous_models = dict(self._settings["models"])
            previous_default = self._settings.get("defaultModel")
            self.register_model_artifact(
                model_id,
                artifact.directory,
                artifact.manifest_sha256,
                size_bytes=artifact.size_bytes,
            )
            await self._rebuild_service()
            self._model_errors.pop(model_id, None)
        except Exception as error:
            if previous_models is not None:
                self._settings["models"] = previous_models
                self._settings["defaultModel"] = previous_default
                self._save_settings()
            logger.exception("Lumi model installation failed")
            self._model_errors[model_id] = type(error).__name__[:80]
        finally:
            self._model_install_id = None
            self._model_progress = None

    def _set_model_progress(self, model_id: str, progress: Mapping[str, Any]) -> None:
        if self._model_install_id != model_id:
            return
        current = progress.get("current")
        total = progress.get("total")
        if type(current) is not int or type(total) is not int or total <= 0:
            return
        stage = progress.get("stage")
        self._model_progress = {
            "stage": _safe_model_stage(stage),
            "current": min(total, max(0, current)),
            "total": min(10_000, max(1, total)),
        }

    def _refresh_model_catalog(self) -> None:
        self._settings["modelCatalog"] = self._model_catalog(from_release=True)
        supported = {item["id"] for item in self._settings["modelCatalog"]}
        self._settings["models"] = {
            model_id: record
            for model_id, record in self._settings["models"].items()
            if model_id in supported
        }
        if self._settings.get("defaultModel") not in supported:
            self._settings["defaultModel"] = None
        self._save_settings()

    def _model_catalog(self, *, from_release: bool = False) -> list[dict[str, Any]]:
        loaded = self._loaded_release
        if from_release and loaded is None:
            raise LumiHostError(
                "Enable a Lumi release before reading its model catalog."
            )
        if loaded is not None:
            supported_models = getattr(loaded.package_module, "supported_models", None)
            if not callable(supported_models):
                raise LumiHostError(
                    "The selected Lumi release does not expose a model catalog."
                )
            options = supported_models()
            result = []
            seen: set[str] = set()
            for option in options:
                model_id = getattr(option, "model_id", None)
                label = getattr(option, "label", None)
                thinking = getattr(option, "supports_thinking", True)
                if (
                    not isinstance(model_id, str)
                    or not model_id.startswith("qwen3.5:")
                    or model_id in seen
                    or not isinstance(label, str)
                    or not label.strip()
                    or len(label) > 80
                    or type(thinking) is not bool
                ):
                    raise LumiHostError(
                        "The selected Lumi release exposes an invalid model catalog."
                    )
                seen.add(model_id)
                result.append(
                    {"id": model_id, "label": label, "supportsThinking": thinking}
                )
            if not result:
                raise LumiHostError(
                    "The selected Lumi release has no supported Qwen3.5 models."
                )
            return result
        stored = self._settings.get("modelCatalog")
        if not isinstance(stored, list):
            return []
        return [
            item
            for item in stored
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].startswith("qwen3.5:")
            and isinstance(item.get("label"), str)
            and type(item.get("supportsThinking")) is bool
        ]

    def _is_supported_model(self, model_id: str) -> bool:
        return any(item["id"] == model_id for item in self._model_catalog())

    def register_model_artifact(
        self,
        model_id: str,
        directory: str | Path,
        manifest_sha256: str,
        *,
        size_bytes: int | None = None,
    ) -> None:
        """Register an artifact only after a model installer verifies its manifest."""

        if not self._is_supported_model(model_id):
            raise LumiHostError(
                "That Qwen3.5 model is not supported by the selected release."
            )
        if not isinstance(manifest_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", manifest_sha256
        ):
            raise LumiHostError("The model manifest digest is invalid.")
        resolved = Path(directory).resolve()
        if not resolved.is_dir() or not _is_relative_to(
            resolved, self.data_directory.resolve()
        ):
            raise LumiHostError(
                "The model must be installed in Lumi's managed data directory."
            )
        self._settings["models"][model_id] = {
            "directory": str(resolved),
            "manifestSha256": manifest_sha256,
            "sizeBytes": size_bytes
            if type(size_bytes) is int and size_bytes >= 0
            else None,
            "enabled": True,
        }
        if not self._settings.get("defaultModel"):
            self._settings["defaultModel"] = model_id
        self._save_settings()

    async def remove_model(self, model_id: str) -> bool:
        """Remove a disabled, non-default model through Lumi's verified installer."""

        async with self._lock:
            loaded = self._loaded_release
            if not self._settings["enabled"] or loaded is None:
                raise LumiHostError("Enable Lumi before removing a model.")
            if not self._is_supported_model(model_id):
                raise LumiHostError(
                    "That Qwen3.5 model is not supported by the selected release."
                )
            if self._model_operation is not None and not self._model_operation.done():
                raise LumiHostError(
                    "Wait for the current Lumi model installation to finish."
                )
            record = self._model_artifact(model_id)
            if record is None:
                return False
            if record.get("enabled") or self._settings.get("defaultModel") == model_id:
                raise LumiHostError(
                    "Disable the model and choose another default before removing it."
                )
            from app.foreground import run_control

            removed = await run_control(
                _remove_qwen_model,
                loaded.package_module,
                self.data_directory / "models",
                model_id,
            )
            if removed:
                self._settings["models"].pop(model_id, None)
                self._save_settings()
            return removed

    async def _rebuild_service(self) -> None:
        loaded = self._loaded_release
        if loaded is None:
            self._service = None
            self._runtime = None
            return
        active_models = [
            (model_id, record)
            for model_id, record in self._settings["models"].items()
            if self._is_supported_model(model_id)
            and record.get("enabled")
            and self._model_artifact(model_id) is not None
        ]
        if not active_models:
            self._service = None
            self._runtime = None
            return

        runtime_module = loaded.runtime_module
        artifacts = {
            model_id: runtime_module.VerifiedModelArtifact(
                model_id,
                record["directory"],
                record["manifestSha256"],
            )
            for model_id, record in active_models
        }
        limits = self._settings["limits"]
        runtime_config = runtime_module.OrtGenAIConfig(
            model_artifacts=artifacts,
            idle_unload_seconds=limits["idleUnloadSeconds"],
            max_context_tokens=limits["maxContextTokens"],
            max_output_tokens=limits["maxOutputTokens"],
        )
        runtime = runtime_module.OrtGenAIChatRuntime(runtime_config)

        model_module = importlib.import_module("lumi.model_catalog")
        options_by_id = {item["id"]: item for item in self._model_catalog()}
        model_options = [
            model_module.QwenModelOption(
                id=model_id,
                label=options_by_id[model_id]["label"],
                supports_thinking=options_by_id[model_id]["supportsThinking"],
                enabled=True,
            )
            for model_id, _record in active_models
        ]
        default_model = self._settings.get("defaultModel")
        if default_model not in {item.id for item in model_options}:
            default_model = model_options[0].id
            self._settings["defaultModel"] = default_model
            self._save_settings()
        model_catalog = model_module.ModelCatalog(
            model_options,
            default_model,
            bool(self._settings.get("defaultThinking", False)),
        )
        registry = self._create_tool_registry(loaded)
        service = loaded.create_embedded_service(
            runtime=runtime,
            models=model_catalog,
            tools=registry,
            store_path=self.model_database_path,
            max_concurrent_chats=limits["maxConcurrentChats"],
            max_active_conversations=limits["maxActiveConversations"],
        )
        await service.start()
        previous_service = self._service
        self._service = service
        self._runtime = runtime
        if previous_service is not None:
            await previous_service.close()

    def _create_tool_registry(self, loaded):
        if self.catalog is None:
            raise LumiHostError("The Orchestrator catalog is not available.")
        tools_module = importlib.import_module("lumi.tools")
        catalog_tools = importlib.import_module("lumi.orchestrator_tools")
        client = LumiCatalogAdapter(
            self.catalog,
            tool_error=catalog_tools._OrchestratorReadError,
        )
        local_tools = (
            catalog_tools.CatalogSearchTool(client),
            catalog_tools.CatalogItemDetailTool(client),
            catalog_tools.HomeRecommendationsTool(client),
            catalog_tools.ContinueWatchingTool(client),
            catalog_tools.NextUpTool(client),
            catalog_tools.FavoritesTool(client),
        )
        web_tools = ()
        search_url = self.web_search_url
        if search_url:
            web_module = importlib.import_module("lumi.web_research")
            search_config = web_module.WebResearchConfig(searxng_url=search_url)
            web_tools = web_module.build_web_research_tools(search_config)
        return tools_module.ToolRegistry((*local_tools, *web_tools))

    def _model_artifact(self, model_id: str) -> dict[str, Any] | None:
        record = self._settings["models"].get(model_id)
        if not isinstance(record, dict):
            return None
        directory = record.get("directory")
        digest = record.get("manifestSha256")
        if not isinstance(directory, str) or not re.fullmatch(
            r"[0-9a-f]{64}", str(digest)
        ):
            return None
        resolved = Path(directory).resolve()
        try:
            resolved.relative_to(self.data_directory.resolve())
        except ValueError:
            return None
        if not resolved.is_dir():
            return None
        return record

    def _read_settings(self) -> dict[str, Any]:
        defaults = {
            "schemaVersion": 1,
            "enabled": False,
            "releaseTag": None,
            "webSearchUrl": None,
            "defaultModel": None,
            "defaultThinking": False,
            "modelCatalog": [],
            "limits": dict(_DEFAULT_LIMITS),
            "models": {},
        }
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return defaults
        if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
            return defaults
        result = dict(defaults)
        result["enabled"] = payload.get("enabled") is True
        tag = payload.get("releaseTag")
        result["releaseTag"] = (
            tag if isinstance(tag, str) and _STABLE_RELEASE_TAG.fullmatch(tag) else None
        )
        search_url = payload.get("webSearchUrl")
        result["webSearchUrl"] = search_url if isinstance(search_url, str) else None
        default_model = payload.get("defaultModel")
        result["defaultModel"] = (
            default_model
            if isinstance(default_model, str) and default_model.startswith("qwen3.5:")
            else None
        )
        result["defaultThinking"] = payload.get("defaultThinking") is True
        catalog = payload.get("modelCatalog")
        if isinstance(catalog, list):
            result["modelCatalog"] = [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "supportsThinking": item["supportsThinking"],
                }
                for item in catalog
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and item["id"].startswith("qwen3.5:")
                and isinstance(item.get("label"), str)
                and item["label"].strip()
                and len(item["label"]) <= 80
                and type(item.get("supportsThinking")) is bool
            ]
        limits = payload.get("limits")
        if isinstance(limits, dict):
            for key, default in _DEFAULT_LIMITS.items():
                value = limits.get(key)
                if type(value) is int and 0 <= value <= 32_768:
                    result["limits"][key] = value
        models = payload.get("models")
        if isinstance(models, dict):
            result["models"] = {
                model_id: record
                for model_id, record in models.items()
                if isinstance(model_id, str)
                and model_id.startswith("qwen3.5:")
                and isinstance(record, dict)
            }
        if not result["releaseTag"]:
            result["enabled"] = False
        return result

    def _save_settings(self) -> None:
        self.data_directory.mkdir(parents=True, exist_ok=True)
        handle, temporary_path = tempfile.mkstemp(
            prefix="integration-", suffix=".tmp", dir=self.data_directory
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    self._settings, stream, ensure_ascii=False, separators=(",", ":")
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.settings_path)
        finally:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass

    def status(self) -> dict[str, Any]:
        installing = self._state == "installing"
        return {
            "integration": {
                "enabled": bool(self._settings["enabled"]),
                "installed": self._loaded_release is not None,
                "state": self._state,
                "releaseTag": self._settings.get("releaseTag"),
                "restartRequired": self.restart_required,
                "error": self._error,
                "progress": (
                    {
                        "stage": "Downloading and verifying Lumi release",
                        "current": 0,
                        "total": 1,
                    }
                    if installing
                    else None
                ),
            },
            **self.model_settings(),
        }


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _installer_context(package_module: Any, model_root: str | Path) -> tuple[Any, Path]:
    """Resolve Lumi's installer and keep its model directory outside package files."""

    installer_type = getattr(package_module, "Qwen35ModelInstaller", None)
    package_file = getattr(package_module, "__file__", None)
    if not callable(installer_type) or not isinstance(package_file, str):
        raise LumiHostError(
            "The selected Lumi release has no supported model installer."
        )
    package_directory = Path(package_file).resolve().parents[1]
    root = Path(model_root)
    if root.is_symlink() or os.path.ismount(root):
        raise LumiHostError("The Lumi model storage path is unsafe.")
    resolved_root = root.resolve()
    if _is_relative_to(resolved_root, package_directory):
        raise LumiHostError("Qwen3.5 model files must stay outside the Lumi package.")
    return installer_type, resolved_root


def _installer_dependency_paths(package_module: Any, paths: Any) -> tuple[Path, ...]:
    package_file = getattr(package_module, "__file__", None)
    if not isinstance(package_file, str) or not isinstance(paths, (tuple, list)):
        raise LumiHostError(
            "The selected Lumi release has invalid installer dependencies."
        )
    installer_root = Path(package_file).resolve().parents[2] / "installer-dependencies"
    checked: list[Path] = []
    for value in paths:
        path = Path(value)
        if path.is_symlink() or os.path.ismount(path):
            raise LumiHostError("A Lumi installer dependency path is unsafe.")
        resolved = path.resolve()
        if not resolved.is_dir() or not _is_relative_to(resolved, installer_root):
            raise LumiHostError(
                "A Lumi installer dependency path is outside its release."
            )
        checked.append(resolved)
    if not checked:
        raise LumiHostError(
            "The selected Lumi release has no installer dependencies for this host."
        )
    return tuple(checked)


def _install_qwen_model(
    package_module: Any,
    model_root: str | Path,
    model_id: str,
    dependency_paths: Any,
    progress: Any,
) -> Any:
    """Run Lumi's pinned checkpoint download and conversion in the control lane."""

    installer_type, resolved_root = _installer_context(package_module, model_root)
    dependency_directories = _installer_dependency_paths(
        package_module, dependency_paths
    )
    inserted_paths: list[str] = []
    dll_handles: list[Any] = []
    for path in reversed(tuple(str(value) for value in dependency_directories)):
        if path not in sys.path:
            sys.path.insert(0, path)
            inserted_paths.append(path)
    try:
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if callable(add_dll_directory):
            dll_directories: set[Path] = set()
            for root in dependency_directories:
                for candidate in root.rglob("*"):
                    if candidate.is_file() and candidate.suffix.lower() in {
                        ".dll",
                        ".pyd",
                    }:
                        dll_directories.add(candidate.parent.resolve())
            dll_handles.extend(
                add_dll_directory(str(path)) for path in sorted(dll_directories)
            )
        installer = installer_type(resolved_root)
        install = getattr(installer, "install_model", None)
        if not callable(install):
            raise LumiHostError(
                "The selected Lumi release has no supported model installer."
            )
        return install(model_id, progress=progress)
    finally:
        for path in inserted_paths:
            try:
                sys.path.remove(path)
            except ValueError:
                pass
        for handle in dll_handles:
            close = getattr(handle, "close", None)
            if callable(close):
                close()


def _remove_qwen_model(
    package_module: Any, model_root: str | Path, model_id: str
) -> bool:
    installer_type, resolved_root = _installer_context(package_module, model_root)
    installer = installer_type(resolved_root)
    remove = getattr(installer, "remove_model", None)
    if not callable(remove):
        raise LumiHostError(
            "The selected Lumi release has no supported model removal API."
        )
    return bool(remove(model_id))


def _safe_model_stage(stage: object) -> str:
    labels = {
        "validating": "Validating model selection",
        "downloading": "Downloading pinned model files",
        "verifying-source": "Verifying source files",
        "converting": "Converting model for local inference",
        "verifying-output": "Verifying converted model",
        "activating": "Activating model files",
        "complete": "Model installation complete",
    }
    return (
        labels.get(stage, "Preparing model installation")
        if isinstance(stage, str)
        else "Preparing model installation"
    )


lumi_host = LumiHost()
