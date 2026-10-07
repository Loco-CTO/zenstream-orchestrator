"""Install and import Lumi from an explicitly selected official GitHub release.

This module deliberately does not run pip, spawn a process, contact a separate
Lumi service, or make an installation during import or construction. The
application should call LumiReleaseManager.enable(tag) only after an
administrator has enabled Lumi and selected a pinned stable release tag.

Proposed release contract (schema version 1): the release contains one asset
named lumi-runtime.zip. Its root has lumi-release.json and the package files
listed in manifest.files. Each file entry is {"sha256": <64 lowercase hex>,
"size": <integer>}. The manifest also declares tag, runtimeApiVersion,
minimumOrchestratorVersion, maximumOrchestratorVersionExclusive, and
runtimeDependencies. Each runtime dependency is a separately published GitHub
release wheel asset with distribution, version, pythonTag, abiTag, platformTag,
asset, and sha256 fields. Every selected wheel must appear in the GitHub Release
API asset list with the same SHA-256 digest. Lumi must publish the compatible
onnxruntime-genai wheel for each supported Python ABI and host platform; the
wheel is extracted below the managed Lumi release directory and added to
sys.path only while that release is active. Optional installerDependencies
follow the same pinned wheel contract but are fetched only after an administrator
explicitly requests a model download. Neither dependency group is installed
with an unpinned pip command.

The package contract exports lumi.LUMI_PLUGIN_API_VERSION,
lumi.runtime.LUMI_RUNTIME_API_VERSION, the OrtGenAIChatRuntime,
OrtGenAIConfig, and VerifiedModelArtifact symbols, plus
lumi.service_factory.create_embedded_service. The service object returned by
the factory exposes close(), which may be synchronous or asynchronous.
Disable closes services and removes Python import aliases. Python cannot
reliably unload an imported native extension, so an Orchestrator restart may
still be required before re-enabling Lumi or removing its package and wheel
files. This factory composes an in-process package service, not a separate
Lumi process.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.machinery
import inspect
import io
import json
import os
import platform
import re
import shutil
import stat
import sys
import sysconfig
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

LUMI_GITHUB_REPOSITORY = "Loco-CTO/zenstream-lumi"
LUMI_GITHUB_API = f"https://api.github.com/repos/{LUMI_GITHUB_REPOSITORY}"
LUMI_RELEASE_ASSET_NAME = "lumi-runtime.zip"
LUMI_RELEASE_MANIFEST_NAME = "lumi-release.json"
LUMI_PLUGIN_API_VERSION = 1
LUMI_RUNTIME_API_VERSION = 1

MAX_RELEASE_METADATA_BYTES = 1024 * 1024
MAX_RELEASE_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_WHEEL_BYTES = 256 * 1024 * 1024
MAX_TOTAL_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5000
MAX_PACKAGE_FILES = 1000
MAX_PACKAGE_FILE_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_WHEEL_FILE_BYTES = 128 * 1024 * 1024
MAX_WHEEL_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_TOTAL_WHEEL_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_WHEELS = 16
MAX_INSTALLER_WHEELS = 128
MAX_INSTALLER_WHEEL_BYTES = 512 * 1024 * 1024
MAX_INSTALLER_TOTAL_DOWNLOAD_BYTES = 1024 * 1024 * 1024
MAX_INSTALLER_WHEEL_FILE_BYTES = 512 * 1024 * 1024
MAX_INSTALLER_WHEEL_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_INSTALLER_TOTAL_WHEEL_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
_REQUIRED_INSTALLER_DISTRIBUTIONS = frozenset(
    {"huggingface-hub", "onnx-ir", "torch", "transformers"}
)

_TAG_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]+\.whl$")
_IMPORT_LOCK = threading.RLock()
_DLL_DIRECTORY_HANDLES: dict[str, tuple[object, ...]] = {}
_ALLOWED_GITHUB_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)


class LumiReleaseError(RuntimeError):
    """Base error for release verification, installation, or activation."""


class LumiReleaseUnavailable(LumiReleaseError):
    """The requested official release or one of its required assets is absent."""


class LumiReleaseCompatibilityError(LumiReleaseError):
    """The release does not support this Orchestrator or host runtime."""


@dataclass(frozen=True)
class HttpResponse:
    """A bounded HTTP response returned by the injected fetcher."""

    body: bytes
    final_url: str
    headers: Mapping[str, str]


@dataclass(frozen=True)
class LumiReleaseCandidate:
    """A stable published tag with the expected runtime package asset."""

    tag: str
    name: str
    published_at: str
    package_sha256: str


@dataclass(frozen=True)
class ImportedLumiModules:
    package: ModuleType
    runtime: ModuleType
    service_factory: ModuleType


HttpFetcher = Callable[[str, Mapping[str, str], int], HttpResponse]
RuntimeImporter = Callable[[Path, Sequence[Path]], ImportedLumiModules]
RuntimeUnloader = Callable[[Path, Sequence[Path]], None]


@dataclass(frozen=True)
class RuntimeHost:
    """Wheel tags supported by the current interpreter and operating system."""

    python_tag: str
    abi_tag: str
    platform_tags: tuple[str, ...]

    @classmethod
    def current(cls) -> RuntimeHost:
        implementation = sys.implementation.name
        if implementation != "cpython":
            raise LumiReleaseCompatibilityError(
                "Lumi releases currently support CPython only"
            )
        major, minor = sys.version_info[:2]
        python_tag = f"cp{major}{minor}"
        soabi = str(sysconfig.get_config_var("SOABI") or "")
        if soabi.startswith("cpython-"):
            abi_version = soabi.removeprefix("cpython-").split("-", 1)[0]
        elif soabi.startswith("cp"):
            abi_version = soabi.split("-", 1)[0].removeprefix("cp")
        else:
            abi_version = ""
        expected_abi_version = re.compile(rf"{major}{minor}(?:d|t|dt|td)?")
        if not expected_abi_version.fullmatch(abi_version):
            raise LumiReleaseCompatibilityError(
                "the current CPython ABI cannot be identified"
            )
        abi_tag = f"cp{abi_version}"
        machine = platform.machine().lower()
        if machine in {"amd64", "x64"}:
            machine = "x86_64"
        elif machine in {"arm64"}:
            machine = "aarch64"

        if sys.platform == "win32" and machine == "x86_64":
            platform_tags = ("win_amd64",)
        elif sys.platform == "linux" and machine in {"x86_64", "aarch64"}:
            platform_tags = _linux_wheel_platform_tags(machine)
        else:
            raise LumiReleaseCompatibilityError(
                "this operating system and architecture have no supported Lumi wheel"
            )
        return cls(python_tag, abi_tag, platform_tags)


def _linux_wheel_platform_tags(machine: str) -> tuple[str, ...]:
    libc_name, libc_version = platform.libc_ver()
    tags: list[str] = []
    if libc_name.lower() == "glibc":
        try:
            major, minor = (int(part) for part in libc_version.split(".", 1))
        except (TypeError, ValueError):
            major, minor = 0, 0
        if major >= 2 and minor >= 17:
            for supported_minor in range(minor, 16, -1):
                tags.append(f"manylinux_2_{supported_minor}_{machine}")
            if machine == "x86_64":
                tags.append("manylinux2014_x86_64")
        tags.append(f"linux_{machine}")
    elif libc_name.lower() == "musl":
        try:
            major, minor = (int(part) for part in libc_version.split(".", 1))
        except (TypeError, ValueError):
            major, minor = 0, 0
        if major >= 1:
            for supported_minor in range(minor, -1, -1):
                tags.append(f"musllinux_{major}_{supported_minor}_{machine}")
    if not tags:
        raise LumiReleaseCompatibilityError(
            "the current Linux C library has no supported Lumi wheel"
        )
    return tuple(dict.fromkeys(tags))


@dataclass(frozen=True)
class RuntimeDependency:
    distribution: str
    version: str
    python_tag: str
    abi_tag: str
    platform_tag: str
    asset: str
    sha256: str


@dataclass(frozen=True)
class LumiReleaseManifest:
    tag: str
    runtime_api_version: int
    minimum_orchestrator_version: tuple[int, int, int]
    maximum_orchestrator_version_exclusive: tuple[int, int, int]
    package_files: Mapping[str, tuple[int, str]]
    runtime_dependencies: tuple[RuntimeDependency, ...]
    installer_dependencies: tuple[RuntimeDependency, ...]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class _Asset:
    name: str
    size: int
    sha256: str | None
    download_url: str


@dataclass(frozen=True)
class _PreparedRelease:
    tag: str
    asset_digest: str
    manifest: LumiReleaseManifest
    package_archive: bytes
    wheels: tuple[tuple[RuntimeDependency, bytes], ...]


@dataclass(frozen=True)
class _InstallTransaction:
    release_directory: Path
    previous_directory: Path | None
    had_previous: bool


class LoadedLumiRelease:
    """An imported release, active only while its manager keeps it enabled."""

    def __init__(
        self,
        tag: str,
        directory: Path,
        manifest: LumiReleaseManifest,
        modules: ImportedLumiModules,
    ) -> None:
        self.tag = tag
        self.version = tag[1:]
        self.directory = directory
        self.manifest = manifest
        self.module = modules.package
        self.package_module = modules.package
        self.runtime_module = modules.runtime
        self.service_factory = modules.service_factory
        self.create_embedded_service = self._create_embedded_service
        self.runtime_api_version = manifest.runtime_api_version
        self._enabled = True
        self._services: list[object] = []
        self._pending_service_tasks: set[asyncio.Task[object]] = set()

    def _create_embedded_service(self, *args: object, **kwargs: object) -> object:
        """Compose the package's in-process service and retain it for close."""
        if not self._enabled:
            raise LumiReleaseError("the Lumi release is disabled")
        result = self.service_factory.create_embedded_service(*args, **kwargs)
        if inspect.isawaitable(result):

            async def track_service() -> object:
                if not self._enabled:
                    close_awaitable = getattr(result, "close", None)
                    if callable(close_awaitable):
                        close_awaitable()
                    raise LumiReleaseError("the Lumi release is disabled")
                service = await result
                if service is None or not callable(getattr(service, "close", None)):
                    raise LumiReleaseError(
                        "the Lumi service factory must return a service with close()"
                    )
                if not self._enabled:
                    self._services.append(service)
                    raise LumiReleaseError("the Lumi release is disabled")
                self._services.append(service)
                return service

            try:
                task = asyncio.get_running_loop().create_task(track_service())
            except RuntimeError as error:
                close_awaitable = getattr(result, "close", None)
                if callable(close_awaitable):
                    close_awaitable()
                raise LumiReleaseError(
                    "an asynchronous Lumi service must be composed in a running event loop"
                ) from error
            self._pending_service_tasks.add(task)
            task.add_done_callback(self._pending_service_tasks.discard)
            return task
        if result is None or not callable(getattr(result, "close", None)):
            raise LumiReleaseError(
                "the Lumi service factory must return a service with close()"
            )
        self._services.append(result)
        return result

    async def close_services(self) -> tuple[Exception, ...]:
        """Attempt to close every composed service before unloading modules."""
        self._enabled = False
        pending_tasks = tuple(self._pending_service_tasks)
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        failures: list[Exception] = []
        while self._services:
            service = self._services.pop()
            close = getattr(service, "close", None)
            if not callable(close):
                failures.append(
                    LumiReleaseError(
                        "a Lumi service does not expose the required close() method"
                    )
                )
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                failures.append(error)
        return tuple(failures)


class LumiReleaseManager:
    """Explicitly download, verify, install, import, and disable Lumi releases.

    The supplied managed_data_path must be the Orchestrator metadata/data root.
    All downloaded release bytes, extracted package files, wheels, and staging
    directories are children of managed_data_path/lumi. Constructing a manager
    performs no I/O; enable(tag) is the only method that downloads or imports.
    """

    def __init__(
        self,
        managed_data_path: str | Path,
        orchestrator_version: str,
        *,
        runtime_host: RuntimeHost | None = None,
        fetcher: HttpFetcher | None = None,
        importer: RuntimeImporter | None = None,
        unloader: RuntimeUnloader | None = None,
    ) -> None:
        self._managed_data_path = Path(managed_data_path).expanduser().resolve()
        self._release_root = self._managed_data_path / "lumi" / "releases"
        self._orchestrator_version = _parse_version(
            orchestrator_version, "Orchestrator version"
        )
        self._runtime_host = runtime_host
        self._fetcher = fetcher or _fetch_github_bytes
        self._importer = importer or _import_managed_lumi
        self._unloader = unloader or _unload_managed_lumi
        self._active: LoadedLumiRelease | None = None
        self._closing_release: LoadedLumiRelease | None = None
        self._restart_required = False
        self._disable_error: str | None = None
        self._lock = asyncio.Lock()

    @property
    def active_release(self) -> LoadedLumiRelease | None:
        """The current in-process release, or None when Lumi is disabled."""
        return self._active

    @property
    def restart_required(self) -> bool:
        """Whether cleanup requires an Orchestrator restart before reactivation."""
        return self._restart_required

    @property
    def model_install_available(self) -> bool:
        """Whether the active release publishes a complete installer set for this host."""
        active = self._active
        return bool(
            active
            and _matching_installer_dependencies(
                active.manifest.installer_dependencies,
                self._host(),
            )
        )

    @property
    def disable_error(self) -> str | None:
        """Safe recovery detail when disabling Lumi could not fully clean up."""
        return self._disable_error

    def _host(self) -> RuntimeHost:
        return self._runtime_host or RuntimeHost.current()

    async def list_published_releases(
        self, limit: int = 20
    ) -> tuple[LumiReleaseCandidate, ...]:
        """List stable package candidates without downloading or importing code.

        This is discovery for an administrator's version selector. The
        selected manifest is still checked for Orchestrator, ABI, platform,
        and runtime API compatibility by enable(tag).
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise LumiReleaseCompatibilityError(
                "release listing limit must be from 1 to 100"
            )
        return await asyncio.to_thread(self._list_published_releases, limit)

    async def enable(self, tag: str) -> LoadedLumiRelease:
        """Enable one explicitly selected, stable release tag.

        This method never consults the latest-release endpoint or invents a
        fallback version. A missing release, missing asset, or unsupported host
        fails closed before any package is made active.
        """
        if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
            raise LumiReleaseCompatibilityError(
                "Lumi must be enabled with a stable vMAJOR.MINOR.PATCH tag"
            )
        async with self._lock:
            if self._closing_release is not None:
                raise LumiReleaseError(
                    "Lumi disable is still closing the active service"
                )
            if self._restart_required:
                reason = self._disable_error or "a native Lumi extension remains loaded"
                raise LumiReleaseError(
                    f"restart Orchestrator before re-enabling Lumi: {reason}"
                )
            if self._active is not None:
                if self._active.tag == tag:
                    return self._active
                raise LumiReleaseError(
                    "disable the active Lumi release before enabling another tag"
                )
            prepared = await asyncio.to_thread(self._prepare_release, tag)
            transaction = await asyncio.to_thread(self._install_prepared, prepared)
            try:
                modules = await asyncio.to_thread(
                    self._importer,
                    transaction.release_directory / "package",
                    _dependency_directories(transaction.release_directory),
                )
                self._validate_imported_modules(modules, prepared.manifest)
            except Exception as error:
                dependency_directories = _dependency_directories(
                    transaction.release_directory
                )
                native_loaded = _has_loaded_native_extension(dependency_directories)
                self._restart_required = self._restart_required or native_loaded
                try:
                    await asyncio.to_thread(
                        self._unloader,
                        transaction.release_directory / "package",
                        dependency_directories,
                    )
                finally:
                    if not native_loaded:
                        await asyncio.to_thread(self._rollback_install, transaction)
                if isinstance(error, LumiReleaseError):
                    raise
                if native_loaded:
                    raise LumiReleaseError(
                        f"Lumi release {tag} failed during activation after loading native code; "
                        "restart Orchestrator before cleanup or retry"
                    ) from error
                raise LumiReleaseError(
                    f"Lumi release {tag} could not be imported; activation was rolled back"
                ) from error

            await asyncio.to_thread(self._commit_install, transaction)
            self._active = LoadedLumiRelease(
                prepared.tag,
                transaction.release_directory,
                prepared.manifest,
                modules,
            )
            return self._active

    async def install_model_dependencies(self) -> tuple[Path, ...]:
        """Fetch and extract conversion wheels after an explicit model-install request.

        These potentially large wheels are a separate release-manifest group. They are
        never downloaded by enable(tag), and their import directories are not added to
        the active Lumi runtime paths.
        """

        async with self._lock:
            active = self._active
            if active is None or self._closing_release is not None:
                raise LumiReleaseError("enable Lumi before installing a model")
            matching = _matching_installer_dependencies(
                active.manifest.installer_dependencies,
                self._host(),
            )
            if not matching:
                raise LumiReleaseCompatibilityError(
                    "the selected Lumi release has no complete model installer wheel set "
                    "for this host"
                )
            from app.foreground import run_control

            directories = await run_control(
                self._install_installer_dependencies,
                active.tag,
                active.directory,
                matching,
            )
            if not directories:
                raise LumiReleaseCompatibilityError(
                    "the selected Lumi release has no installer wheels for this host"
                )
            return directories

    async def disable(self) -> None:
        """Close services and block Lumi immediately before unloading imports."""
        async with self._lock:
            active = self._active
            if active is None:
                active = self._closing_release
            if active is None:
                return
            self._active = None
            self._closing_release = active
            self._disable_error = None
            active._enabled = False
            close_failures = await active.close_services()
            dependency_directories = (
                *_dependency_directories(active.directory),
                *_installer_dependency_directories(active.directory),
            )
            self._restart_required = self._restart_required or _has_loaded_native_extension(
                dependency_directories
            )
            unload_error: Exception | None = None
            try:
                await asyncio.to_thread(
                    self._unloader,
                    active.directory / "package",
                    dependency_directories,
                )
            except Exception as error:
                unload_error = error
            finally:
                self._closing_release = None

            if close_failures or unload_error is not None:
                self._restart_required = True
                recovery_reasons = []
                if close_failures:
                    recovery_reasons.append(
                        f"{len(close_failures)} Lumi service close operation(s) failed"
                    )
                if unload_error is not None:
                    recovery_reasons.append("Lumi module cleanup failed")
                self._disable_error = "; ".join(recovery_reasons)
                error = close_failures[0] if close_failures else unload_error
                raise LumiReleaseError(
                    "Lumi is disabled, but cleanup failed; restart Orchestrator "
                    f"before enabling it again ({self._disable_error})"
                ) from error

    def _prepare_release(self, tag: str) -> _PreparedRelease:
        runtime_host = self._host()
        metadata_url = (
            f"{LUMI_GITHUB_API}/releases/tags/{urllib.parse.quote(tag, safe='')}"
        )
        response = self._fetcher(
            metadata_url,
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ZenStream-Orchestrator",
            },
            MAX_RELEASE_METADATA_BYTES,
        )
        if _canonical_api_url(response.final_url) != _canonical_api_url(metadata_url):
            raise LumiReleaseError(
                "GitHub release metadata came from an unexpected URL"
            )
        try:
            release = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LumiReleaseError(
                "GitHub returned invalid release metadata"
            ) from error
        if not isinstance(release, dict):
            raise LumiReleaseError("GitHub returned invalid release metadata")
        self._validate_release_metadata(release, tag)
        assets = _parse_release_assets(release.get("assets"))
        package_asset = assets.get(LUMI_RELEASE_ASSET_NAME)
        if package_asset is None:
            raise LumiReleaseUnavailable(
                f"release {tag} has no {LUMI_RELEASE_ASSET_NAME} asset"
            )
        package_bytes = self._download_asset(
            tag, package_asset, MAX_RELEASE_ARCHIVE_BYTES
        )
        manifest = _read_and_validate_manifest(
            package_bytes,
            tag=tag,
            orchestrator_version=self._orchestrator_version,
            runtime_host=runtime_host,
        )
        matching_dependencies = _matching_dependencies(
            manifest.runtime_dependencies, runtime_host
        )
        if not matching_dependencies:
            raise LumiReleaseCompatibilityError(
                "the release has no runtime dependency wheels for this Python ABI and host"
            )
        if not any(
            _normalize_distribution(item.distribution) == "onnxruntime-genai"
            for item in matching_dependencies
        ):
            raise LumiReleaseCompatibilityError(
                "the release does not provide a pinned onnxruntime-genai wheel for this host"
            )
        if len(matching_dependencies) > MAX_WHEELS:
            raise LumiReleaseCompatibilityError(
                "the release declares too many runtime wheels"
            )

        wheel_payloads: list[tuple[RuntimeDependency, bytes]] = []
        downloaded_bytes = len(package_bytes)
        expanded_wheel_bytes = 0
        for dependency in matching_dependencies:
            asset = assets.get(dependency.asset)
            if asset is None:
                raise LumiReleaseUnavailable(
                    f"release {tag} is missing required wheel asset {dependency.asset}"
                )
            if asset.sha256 != dependency.sha256:
                raise LumiReleaseError(
                    f"release metadata digest does not match the manifest for {dependency.asset}"
                )
            if downloaded_bytes + asset.size > MAX_TOTAL_DOWNLOAD_BYTES:
                raise LumiReleaseError(
                    "the release exceeds the total download size limit"
                )
            wheel_bytes = self._download_asset(tag, asset, MAX_WHEEL_BYTES)
            downloaded_bytes += len(wheel_bytes)
            expanded_wheel_bytes += _validate_wheel_archive(wheel_bytes, dependency)
            if expanded_wheel_bytes > MAX_TOTAL_WHEEL_EXPANDED_BYTES:
                raise LumiReleaseError(
                    "runtime wheels exceed the total expanded size limit"
                )
            wheel_payloads.append((dependency, wheel_bytes))
        return _PreparedRelease(
            tag,
            package_asset.sha256 or "",
            manifest,
            package_bytes,
            tuple(wheel_payloads),
        )

    def _install_installer_dependencies(
        self,
        tag: str,
        release_directory: Path,
        dependencies: Sequence[RuntimeDependency],
    ) -> tuple[Path, ...]:
        runtime_host = self._host()
        matching = _matching_installer_dependencies(dependencies, runtime_host)
        if not matching:
            raise LumiReleaseCompatibilityError(
                "the release has no complete model installer wheel set for this host"
            )
        if len(matching) > MAX_INSTALLER_WHEELS:
            raise LumiReleaseCompatibilityError(
                "the release declares too many model installer wheels"
            )
        target = release_directory / "installer-dependencies"
        if target.is_symlink() or os.path.ismount(target):
            raise LumiReleaseError("managed Lumi installer dependencies use an unsafe path")
        if _installer_dependencies_are_valid(target, tag, matching):
            return _installer_dependency_directories(release_directory)

        metadata_url = f"{LUMI_GITHUB_API}/releases/tags/{urllib.parse.quote(tag, safe='')}"
        response = self._fetcher(
            metadata_url,
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ZenStream-Orchestrator",
            },
            MAX_RELEASE_METADATA_BYTES,
        )
        if _canonical_api_url(response.final_url) != _canonical_api_url(metadata_url):
            raise LumiReleaseError("GitHub release metadata came from an unexpected URL")
        try:
            release = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LumiReleaseError("GitHub returned invalid release metadata") from error
        if not isinstance(release, dict):
            raise LumiReleaseError("GitHub returned invalid release metadata")
        self._validate_release_metadata(release, tag)
        assets = _parse_release_assets(release.get("assets"))
        selected_assets: list[tuple[RuntimeDependency, _Asset]] = []
        total_download_bytes = 0
        for dependency in matching:
            asset = assets.get(dependency.asset)
            if asset is None:
                raise LumiReleaseUnavailable(
                    f"release {tag} is missing installer wheel asset {dependency.asset}"
                )
            if asset.sha256 != dependency.sha256:
                raise LumiReleaseError(
                    f"release metadata digest does not match the manifest for {dependency.asset}"
                )
            if asset.size > MAX_INSTALLER_WHEEL_BYTES:
                raise LumiReleaseError("a model installer wheel exceeds the download size limit")
            total_download_bytes += asset.size
            if total_download_bytes > MAX_INSTALLER_TOTAL_DOWNLOAD_BYTES:
                raise LumiReleaseError(
                    "model installer wheels exceed the total download size limit"
                )
            selected_assets.append((dependency, asset))

        staging = Path(
            tempfile.mkdtemp(prefix=".installer-staging-", dir=release_directory)
        )
        backup = release_directory / f".installer-rollback-{os.getpid()}"
        wheelhouse = staging / "wheelhouse"
        dependency_root = staging / "dependencies"
        wheelhouse.mkdir()
        expanded_bytes = 0
        try:
            for dependency, asset in selected_assets:
                wheel_bytes = self._download_asset(
                    tag,
                    asset,
                    MAX_INSTALLER_WHEEL_BYTES,
                )
                expanded_bytes += _validate_wheel_archive(
                    wheel_bytes,
                    dependency,
                    installer=True,
                )
                if expanded_bytes > MAX_INSTALLER_TOTAL_WHEEL_EXPANDED_BYTES:
                    raise LumiReleaseError(
                        "model installer wheels exceed the total expanded size limit"
                    )
                (wheelhouse / dependency.asset).write_bytes(wheel_bytes)
                destination = (
                    dependency_root
                    / f"{_normalize_distribution(dependency.distribution)}-{dependency.version}"
                    / "site-packages"
                )
                destination.mkdir(parents=True, exist_ok=True)
                _extract_wheel(
                    wheel_bytes,
                    destination,
                    dependency,
                    installer=True,
                )
            marker = _installer_dependency_manifest(tag, matching)
            (staging / "lumi-installer-wheels.json").write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
                newline="\n",
            )
            if backup.exists() or backup.is_symlink():
                _remove_managed_path(backup)
            if target.exists():
                os.replace(target, backup)
            try:
                os.replace(staging, target)
            except Exception:
                if backup.exists():
                    os.replace(backup, target)
                raise
            if backup.exists():
                _remove_managed_path(backup, ignore_errors=True)
        except Exception:
            if staging.exists():
                _remove_managed_path(staging, ignore_errors=True)
            raise
        return _installer_dependency_directories(release_directory)

    def _list_published_releases(self, limit: int) -> tuple[LumiReleaseCandidate, ...]:
        url = f"{LUMI_GITHUB_API}/releases?per_page={limit}"
        response = self._fetcher(
            url,
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ZenStream-Orchestrator",
            },
            MAX_RELEASE_METADATA_BYTES,
        )
        if _canonical_api_url(response.final_url) != _canonical_api_url(url):
            raise LumiReleaseError("GitHub release listing came from an unexpected URL")
        try:
            releases = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LumiReleaseError(
                "GitHub returned an invalid release listing"
            ) from error
        if not isinstance(releases, list):
            raise LumiReleaseError("GitHub returned an invalid release listing")
        candidates: list[LumiReleaseCandidate] = []
        for release in releases[:limit]:
            if not isinstance(release, dict):
                continue
            tag = release.get("tag_name")
            if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
                continue
            try:
                self._validate_release_metadata(release, tag)
                assets = _parse_release_assets(release.get("assets"))
            except LumiReleaseError:
                continue
            package_asset = assets.get(LUMI_RELEASE_ASSET_NAME)
            if (
                package_asset is None
                or package_asset.sha256 is None
                or package_asset.size <= 0
                or package_asset.size > MAX_RELEASE_ARCHIVE_BYTES
            ):
                continue
            expected_url = (
                f"https://github.com/{LUMI_GITHUB_REPOSITORY}/releases/download/"
                f"{urllib.parse.quote(tag, safe='')}/"
                f"{urllib.parse.quote(LUMI_RELEASE_ASSET_NAME, safe='')}"
            )
            try:
                if _canonical_https_url(package_asset.download_url) != expected_url:
                    continue
            except LumiReleaseError:
                continue
            name = release.get("name")
            published_at = release.get("published_at")
            candidates.append(
                LumiReleaseCandidate(
                    tag,
                    name[:256] if isinstance(name, str) and name else tag,
                    published_at,
                    package_asset.sha256,
                )
            )
        return tuple(candidates)

    def _validate_release_metadata(self, release: Mapping[str, Any], tag: str) -> None:
        if release.get("tag_name") != tag:
            raise LumiReleaseError("GitHub returned a different release tag")
        if release.get("draft") is not False:
            raise LumiReleaseUnavailable("draft Lumi releases cannot be installed")
        if release.get("prerelease") is not False:
            raise LumiReleaseUnavailable("Lumi prereleases cannot be installed")
        if (
            not isinstance(release.get("published_at"), str)
            or not release["published_at"].strip()
        ):
            raise LumiReleaseUnavailable("the Lumi release is not published")
        if release.get("html_url") != (
            f"https://github.com/{LUMI_GITHUB_REPOSITORY}/releases/tag/{tag}"
        ):
            raise LumiReleaseError(
                "GitHub release metadata has an unexpected repository origin"
            )
        if release.get("full_name") not in (None, LUMI_GITHUB_REPOSITORY):
            raise LumiReleaseError(
                "GitHub release metadata has an unexpected repository"
            )

    def _download_asset(self, tag: str, asset: _Asset, max_bytes: int) -> bytes:
        expected_url = (
            f"https://github.com/{LUMI_GITHUB_REPOSITORY}/releases/download/"
            f"{urllib.parse.quote(tag, safe='')}/{urllib.parse.quote(asset.name, safe='')}"
        )
        if _canonical_https_url(asset.download_url) != expected_url:
            raise LumiReleaseError(f"release asset {asset.name} has an unexpected URL")
        if asset.sha256 is None:
            raise LumiReleaseError(f"release asset {asset.name} has no SHA-256 digest")
        if asset.size <= 0 or asset.size > max_bytes:
            raise LumiReleaseError(f"release asset {asset.name} exceeds the size limit")
        response = self._fetcher(
            expected_url,
            {
                "Accept": "application/octet-stream",
                "User-Agent": "ZenStream-Orchestrator",
            },
            max_bytes,
        )
        _validate_download_url(response.final_url)
        if len(response.body) != asset.size:
            raise LumiReleaseError(f"release asset {asset.name} has an unexpected size")
        actual_digest = hashlib.sha256(response.body).hexdigest()
        if actual_digest != asset.sha256:
            raise LumiReleaseError(
                f"release asset {asset.name} failed its SHA-256 check"
            )
        return response.body

    def _install_prepared(self, prepared: _PreparedRelease) -> _InstallTransaction:
        digest_parts = [prepared.asset_digest]
        digest_parts.extend(
            hashlib.sha256(wheel_bytes).hexdigest()
            for _, wheel_bytes in prepared.wheels
        )
        release_digest = hashlib.sha256(
            "".join(digest_parts).encode("ascii")
        ).hexdigest()
        release_root = _managed_release_root(self._managed_data_path)
        final_directory = release_root / f"{prepared.tag}-{release_digest[:16]}"
        staging = Path(
            tempfile.mkdtemp(prefix=f".staging-{prepared.tag}-", dir=release_root)
        )
        previous: Path | None = None
        had_previous = final_directory.exists()
        try:
            package_root = staging / "package"
            package_root.mkdir()
            _extract_package_archive(
                prepared.package_archive,
                package_root,
                prepared.manifest,
            )
            wheelhouse = staging / "wheelhouse"
            wheelhouse.mkdir()
            for dependency, wheel_bytes in prepared.wheels:
                wheel_target = wheelhouse / dependency.asset
                wheel_target.write_bytes(wheel_bytes)
                dependency_root = (
                    staging
                    / "dependencies"
                    / f"{_normalize_distribution(dependency.distribution)}-{dependency.version}"
                    / "site-packages"
                )
                dependency_root.mkdir(parents=True, exist_ok=True)
                _extract_wheel(wheel_bytes, dependency_root, dependency)
            if final_directory.exists():
                previous = (
                    release_root / f".rollback-{final_directory.name}-{os.getpid()}"
                )
                if previous.exists():
                    _remove_managed_path(previous)
                os.replace(final_directory, previous)
            try:
                os.replace(staging, final_directory)
            except Exception:
                if previous is not None and previous.exists():
                    os.replace(previous, final_directory)
                    previous = None
                raise
            return _InstallTransaction(final_directory, previous, had_previous)
        except Exception:
            if staging.exists():
                _remove_managed_path(staging, ignore_errors=True)
            raise

    @staticmethod
    def _validate_imported_modules(
        modules: ImportedLumiModules, manifest: LumiReleaseManifest
    ) -> None:
        plugin_api_version = getattr(modules.package, "LUMI_PLUGIN_API_VERSION", None)
        if (
            type(plugin_api_version) is not int
            or plugin_api_version != LUMI_PLUGIN_API_VERSION
        ):
            raise LumiReleaseCompatibilityError(
                "the Lumi package plugin API version is unsupported"
            )
        runtime_api_version = getattr(modules.runtime, "LUMI_RUNTIME_API_VERSION", None)
        if (
            type(runtime_api_version) is not int
            or runtime_api_version != manifest.runtime_api_version
        ):
            raise LumiReleaseCompatibilityError(
                "the Lumi package runtime API does not match its release manifest"
            )
        required_runtime_symbols = (
            "OrtGenAIChatRuntime",
            "OrtGenAIConfig",
            "VerifiedModelArtifact",
        )
        if any(
            not callable(getattr(modules.runtime, name, None))
            for name in required_runtime_symbols
        ):
            raise LumiReleaseCompatibilityError(
                "the Lumi package does not export the required runtime adapter symbols"
            )
        if not callable(
            getattr(modules.service_factory, "create_embedded_service", None)
        ):
            raise LumiReleaseCompatibilityError(
                "the Lumi package does not export create_embedded_service"
            )

    @staticmethod
    def _rollback_install(transaction: _InstallTransaction) -> None:
        if transaction.release_directory.exists():
            _remove_managed_path(transaction.release_directory, ignore_errors=True)
        if (
            transaction.previous_directory is not None
            and transaction.previous_directory.exists()
        ):
            os.replace(transaction.previous_directory, transaction.release_directory)

    @staticmethod
    def _commit_install(transaction: _InstallTransaction) -> None:
        if transaction.previous_directory is not None:
            _remove_managed_path(transaction.previous_directory, ignore_errors=True)


def _parse_version(value: str, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise LumiReleaseCompatibilityError(f"{label} is invalid")
    match = _TAG_RE.fullmatch(value if value.startswith("v") else f"v{value}")
    if match is None:
        raise LumiReleaseCompatibilityError(f"{label} must use MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _normalize_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_release_assets(raw_assets: object) -> dict[str, _Asset]:
    if not isinstance(raw_assets, list):
        raise LumiReleaseError("GitHub release metadata has no asset list")
    assets: dict[str, _Asset] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            raise LumiReleaseError("GitHub release metadata contains an invalid asset")
        name = raw_asset.get("name")
        if not isinstance(name, str) or not name or name in assets:
            raise LumiReleaseError(
                "GitHub release metadata contains duplicate or invalid asset names"
            )
        size = raw_asset.get("size")
        digest = raw_asset.get("digest")
        download_url = raw_asset.get("browser_download_url")
        if type(size) is not int or size < 0:
            raise LumiReleaseError(
                f"GitHub release asset {name} has invalid size metadata"
            )
        sha256: str | None = None
        if isinstance(digest, str) and digest.startswith("sha256:"):
            candidate = digest.removeprefix("sha256:")
            if _SHA256_RE.fullmatch(candidate):
                sha256 = candidate
        if not isinstance(download_url, str):
            raise LumiReleaseError(f"GitHub release asset {name} has no download URL")
        assets[name] = _Asset(name, size, sha256, download_url)
    return assets


def _canonical_https_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise LumiReleaseError("a release URL is invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() not in _ALLOWED_GITHUB_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise LumiReleaseError("a release URL is outside the pinned GitHub origin")
    return urllib.parse.urlunsplit(
        ("https", parsed.netloc.lower(), parsed.path, "", "")
    )


def _canonical_api_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise LumiReleaseError("a GitHub API URL is invalid") from error
    collection_path = f"/repos/{LUMI_GITHUB_REPOSITORY}/releases"
    tag_path = f"{collection_path}/tags/"
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != "api.github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise LumiReleaseError("a GitHub API URL is outside the pinned origin")
    if parsed.path == collection_path:
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if (
            len(query) != 1
            or query[0][0] != "per_page"
            or not query[0][1].isdigit()
            or not 1 <= int(query[0][1]) <= 100
        ):
            raise LumiReleaseError(
                "GitHub release listing URL has invalid query parameters"
            )
    elif parsed.path.startswith(tag_path):
        tag = parsed.path[len(tag_path) :]
        if not _TAG_RE.fullmatch(tag) or parsed.query:
            raise LumiReleaseError(
                "GitHub release metadata URL has invalid path parameters"
            )
    else:
        raise LumiReleaseError("GitHub API URL is outside the pinned release endpoints")
    return urllib.parse.urlunsplit(
        ("https", "api.github.com", parsed.path, parsed.query, "")
    )


def _validate_download_url(value: str) -> None:
    canonical = _canonical_https_url(value)
    parsed = urllib.parse.urlsplit(canonical)
    if parsed.hostname == "api.github.com":
        raise LumiReleaseError("a release asset redirected to GitHub API metadata")
    if parsed.hostname == "github.com" and "/releases/download/" not in parsed.path:
        raise LumiReleaseError("a release asset redirected outside GitHub Releases")


def _fetch_github_bytes(
    url: str, headers: Mapping[str, str], max_bytes: int
) -> HttpResponse:
    if urllib.parse.urlsplit(url).hostname == "api.github.com":
        _canonical_api_url(url)
    else:
        _canonical_https_url(url)

    class RestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, new_url):
            _validate_download_url(new_url)
            return super().redirect_request(
                req, fp, code, msg, response_headers, new_url
            )

    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    opener = urllib.request.build_opener(RestrictedRedirectHandler())
    try:
        with opener.open(request, timeout=45) as response:
            final_url = response.geturl()
            if urllib.parse.urlsplit(url).hostname == "api.github.com":
                _canonical_api_url(final_url)
            else:
                _validate_download_url(final_url)
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > max_bytes:
                        raise LumiReleaseError("GitHub response exceeds the size limit")
                except ValueError as error:
                    raise LumiReleaseError(
                        "GitHub returned invalid response size metadata"
                    ) from error
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise LumiReleaseError("GitHub response exceeds the size limit")
            return HttpResponse(body, final_url, dict(response.headers.items()))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise LumiReleaseUnavailable(
                "the selected Lumi release or asset does not exist"
            ) from error
        raise LumiReleaseError(
            f"GitHub release request failed with HTTP {error.code}"
        ) from error
    except (OSError, urllib.error.URLError) as error:
        raise LumiReleaseError("GitHub release request failed") from error


def _read_and_validate_manifest(
    archive_bytes: bytes,
    *,
    tag: str,
    orchestrator_version: tuple[int, int, int],
    runtime_host: RuntimeHost,
) -> LumiReleaseManifest:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            infos = _validate_zip_entries(archive.infolist(), MAX_ARCHIVE_ENTRIES)
            if LUMI_RELEASE_MANIFEST_NAME not in infos:
                raise LumiReleaseError("Lumi package asset has no release manifest")
            if infos[LUMI_RELEASE_MANIFEST_NAME].file_size > 64 * 1024:
                raise LumiReleaseError("Lumi release manifest exceeds the size limit")
            manifest_bytes = archive.read(LUMI_RELEASE_MANIFEST_NAME)
    except (zipfile.BadZipFile, OSError, RuntimeError) as error:
        if isinstance(error, LumiReleaseError):
            raise
        raise LumiReleaseError(
            "Lumi package asset is not a valid zip archive"
        ) from error
    try:
        raw = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LumiReleaseError("Lumi release manifest is invalid JSON") from error
    if (
        not isinstance(raw, dict)
        or type(raw.get("schemaVersion")) is not int
        or raw.get("schemaVersion") != 1
    ):
        raise LumiReleaseCompatibilityError(
            "Lumi release manifest schema is unsupported"
        )
    if raw.get("tag") != tag:
        raise LumiReleaseError(
            "Lumi release manifest tag does not match the selected release"
        )
    api_version = raw.get("runtimeApiVersion")
    if type(api_version) is not int or api_version != LUMI_RUNTIME_API_VERSION:
        raise LumiReleaseCompatibilityError(
            "Lumi release runtime API version is unsupported"
        )
    minimum = _parse_version(
        raw.get("minimumOrchestratorVersion"), "minimum Orchestrator version"
    )
    maximum = _parse_version(
        raw.get("maximumOrchestratorVersionExclusive"),
        "maximum Orchestrator version",
    )
    if not minimum < maximum:
        raise LumiReleaseCompatibilityError(
            "Lumi release has an invalid Orchestrator version range"
        )
    if not minimum <= orchestrator_version < maximum:
        raise LumiReleaseCompatibilityError(
            "Lumi release is incompatible with this Orchestrator version"
        )

    package_files = _parse_package_file_manifest(raw.get("files"))
    dependencies = _parse_runtime_dependencies(raw.get("runtimeDependencies"))
    installer_dependencies = _parse_installer_dependencies(
        raw.get("installerDependencies", [])
    )
    if len(dependencies) > MAX_WHEELS:
        raise LumiReleaseCompatibilityError(
            "Lumi release declares too many runtime dependencies"
        )
    if not any(
        _normalize_distribution(item.distribution) == "onnxruntime-genai"
        for item in dependencies
    ):
        raise LumiReleaseCompatibilityError(
            "Lumi release must declare its pinned onnxruntime-genai wheel"
        )
    _validate_package_archive_members(infos, package_files)
    _validate_dependency_wheel_names(dependencies)
    _validate_dependency_wheel_names(installer_dependencies)
    if {item.asset for item in dependencies} & {
        item.asset for item in installer_dependencies
    }:
        raise LumiReleaseError("runtime and installer wheel asset names overlap")
    if installer_dependencies:
        installer_distributions = {
            _normalize_distribution(item.distribution) for item in installer_dependencies
        }
        if not _REQUIRED_INSTALLER_DISTRIBUTIONS.issubset(installer_distributions):
            raise LumiReleaseCompatibilityError("Lumi model installer wheel set is incomplete")
    if not _matching_dependencies(dependencies, runtime_host):
        raise LumiReleaseCompatibilityError(
            "Lumi release does not publish wheels for this Python ABI and host"
        )
    return LumiReleaseManifest(
        tag,
        api_version,
        minimum,
        maximum,
        package_files,
        dependencies,
        installer_dependencies,
        raw,
    )


def _parse_package_file_manifest(raw_files: object) -> dict[str, tuple[int, str]]:
    if (
        not isinstance(raw_files, dict)
        or not raw_files
        or len(raw_files) > MAX_PACKAGE_FILES
    ):
        raise LumiReleaseError("Lumi release manifest has an invalid package file list")
    result: dict[str, tuple[int, str]] = {}
    for relative, raw_entry in raw_files.items():
        _validate_relative_member(relative, package_only=True)
        if not isinstance(raw_entry, dict):
            raise LumiReleaseError(
                f"Lumi file manifest entry for {relative} is invalid"
            )
        size = raw_entry.get("size")
        digest = raw_entry.get("sha256")
        if type(size) is not int or size < 0 or size > MAX_PACKAGE_FILE_BYTES:
            raise LumiReleaseError(
                f"Lumi package file {relative} exceeds the size limit"
            )
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise LumiReleaseError(
                f"Lumi package file {relative} has an invalid SHA-256"
            )
        result[relative] = (size, digest)
    if "lumi/__init__.py" not in result or "lumi/runtime/__init__.py" not in result:
        raise LumiReleaseError("Lumi package does not contain its runtime entrypoint")
    return result


def _parse_runtime_dependencies(
    raw_dependencies: object,
) -> tuple[RuntimeDependency, ...]:
    if not isinstance(raw_dependencies, list) or not raw_dependencies:
        raise LumiReleaseCompatibilityError(
            "Lumi release has no pinned runtime wheel set"
        )
    if len(raw_dependencies) > MAX_WHEELS:
        raise LumiReleaseCompatibilityError(
            "Lumi release declares too many runtime wheels"
        )
    return _parse_wheel_dependencies(raw_dependencies, kind="runtime")


def _parse_installer_dependencies(
    raw_dependencies: object,
) -> tuple[RuntimeDependency, ...]:
    if raw_dependencies == []:
        return ()
    if not isinstance(raw_dependencies, list) or len(raw_dependencies) > MAX_INSTALLER_WHEELS:
        raise LumiReleaseCompatibilityError("Lumi release declares too many installer wheels")
    return _parse_wheel_dependencies(raw_dependencies, kind="installer")


def _parse_wheel_dependencies(
    raw_dependencies: list[object],
    *,
    kind: str,
) -> tuple[RuntimeDependency, ...]:
    dependencies: list[RuntimeDependency] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in raw_dependencies:
        if not isinstance(raw, dict):
            raise LumiReleaseError("Lumi runtime dependency entry is invalid")
        values = (
            raw.get("distribution"),
            raw.get("version"),
            raw.get("pythonTag"),
            raw.get("abiTag"),
            raw.get("platformTag"),
            raw.get("asset"),
            raw.get("sha256"),
        )
        if not all(isinstance(value, str) and value for value in values):
            raise LumiReleaseError("Lumi runtime dependency entry has missing fields")
        distribution, version, python_tag, abi_tag, platform_tag, asset, digest = values
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", distribution):
            raise LumiReleaseError("Lumi runtime wheel distribution name is invalid")
        if kind == "runtime" and _normalize_distribution(distribution) == "onnxruntime-genai" and (
            python_tag == "py3" or abi_tag == "none" or platform_tag == "any"
        ):
            raise LumiReleaseCompatibilityError(
                "onnxruntime-genai must use a pinned host-specific binary wheel"
            )
        if not _ASSET_NAME_RE.fullmatch(asset):
            raise LumiReleaseError("Lumi runtime wheel asset name is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", version):
            raise LumiReleaseError("Lumi runtime wheel version is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_]+", python_tag) or not re.fullmatch(
            r"[A-Za-z0-9_]+", abi_tag
        ):
            raise LumiReleaseError("Lumi runtime wheel Python or ABI tag is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.]+", platform_tag):
            raise LumiReleaseError("Lumi runtime wheel platform tag is invalid")
        if not _SHA256_RE.fullmatch(digest):
            raise LumiReleaseError("Lumi runtime wheel has an invalid SHA-256")
        key = (
            _normalize_distribution(distribution),
            python_tag,
            abi_tag,
            platform_tag,
        )
        if key in seen:
            raise LumiReleaseError("Lumi release declares duplicate runtime wheels")
        seen.add(key)
        dependencies.append(
            RuntimeDependency(
                distribution,
                version,
                python_tag,
                abi_tag,
                platform_tag,
                asset,
                digest,
            )
        )
    return tuple(dependencies)


def _validate_dependency_wheel_names(dependencies: Sequence[RuntimeDependency]) -> None:
    names: set[str] = set()
    for dependency in dependencies:
        if dependency.asset in names:
            raise LumiReleaseError("Lumi release declares duplicate wheel asset names")
        names.add(dependency.asset)
        parts = dependency.asset[:-4].split("-")
        if len(parts) < 5:
            raise LumiReleaseError(f"wheel filename {dependency.asset} is invalid")
        distribution, version, python_tag, abi_tag, platform_tag = (
            parts[-5],
            parts[-4],
            parts[-3],
            parts[-2],
            parts[-1],
        )
        if (
            _normalize_distribution(distribution)
            != _normalize_distribution(dependency.distribution)
            or version != dependency.version
            or python_tag != dependency.python_tag
            or abi_tag != dependency.abi_tag
            or platform_tag != dependency.platform_tag
        ):
            raise LumiReleaseError(
                f"wheel filename {dependency.asset} does not match its manifest metadata"
            )


def _matching_dependencies(
    dependencies: Sequence[RuntimeDependency], host: RuntimeHost
) -> tuple[RuntimeDependency, ...]:
    return tuple(
        dependency
        for dependency in dependencies
        if (
            dependency.python_tag == "py3"
            and dependency.abi_tag == "none"
            and dependency.platform_tag == "any"
        )
        or (
            dependency.python_tag == host.python_tag
            and dependency.abi_tag == host.abi_tag
            and dependency.platform_tag in host.platform_tags
        )
    )


def _matching_installer_dependencies(
    dependencies: Sequence[RuntimeDependency], host: RuntimeHost
) -> tuple[RuntimeDependency, ...]:
    matching = _matching_dependencies(dependencies, host)
    distributions = {
        _normalize_distribution(dependency.distribution) for dependency in matching
    }
    if not _REQUIRED_INSTALLER_DISTRIBUTIONS.issubset(distributions):
        return ()
    return matching


def _validate_package_archive_members(
    infos: Mapping[str, zipfile.ZipInfo], package_files: Mapping[str, tuple[int, str]]
) -> None:
    expected = set(package_files) | {LUMI_RELEASE_MANIFEST_NAME}
    if set(infos) != expected:
        extra = sorted(set(infos) - expected)
        missing = sorted(expected - set(infos))
        details = "unexpected package files" if extra else "missing package files"
        raise LumiReleaseError(
            f"Lumi release has {details}: {', '.join((extra or missing)[:5])}"
        )


def _validate_relative_member(
    relative: object, *, package_only: bool = False
) -> PurePosixPath:
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or "\x00" in relative
    ):
        raise LumiReleaseError("release archive contains an invalid file path")
    if relative.startswith("/") or ":" in relative:
        raise LumiReleaseError("release archive contains an absolute file path")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise LumiReleaseError("release archive contains a traversing file path")
    for part in parts:
        if part.endswith((".", " ")) or any(ord(character) < 32 for character in part):
            raise LumiReleaseError("release archive contains a non-portable file path")
        if any(character in '<>"|?*' for character in part):
            raise LumiReleaseError("release archive contains a non-portable file path")
        device_stem = part.split(".", 1)[0].upper()
        if device_stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
            r"(?:COM|LPT)[1-9]", device_stem
        ):
            raise LumiReleaseError(
                "release archive contains a reserved Windows file name"
            )
    path = PurePosixPath(relative)
    if package_only and (len(parts) < 2 or parts[0] != "lumi"):
        raise LumiReleaseError(
            "Lumi package files must be below the lumi/ package directory"
        )
    return path


def _validate_zip_entries(
    infos: Sequence[zipfile.ZipInfo], maximum_entries: int
) -> dict[str, zipfile.ZipInfo]:
    if len(infos) > maximum_entries:
        raise LumiReleaseError("release archive contains too many entries")
    result: dict[str, zipfile.ZipInfo] = {}
    normalized_names: set[str] = set()
    for info in infos:
        name = info.filename
        is_directory = info.is_dir()
        checked_name = name[:-1] if is_directory and name.endswith("/") else name
        normalized_name = checked_name.casefold()
        if normalized_name in normalized_names:
            raise LumiReleaseError("release archive contains duplicate paths")
        normalized_names.add(normalized_name)
        _validate_relative_member(checked_name)
        if info.flag_bits & 0x1:
            raise LumiReleaseError("encrypted release archive entries are unsupported")
        file_type = stat.S_IFMT(info.external_attr >> 16)
        allowed_types = (0, stat.S_IFDIR) if is_directory else (0, stat.S_IFREG)
        if file_type not in allowed_types:
            raise LumiReleaseError(
                "release archive contains a symbolic link or special file"
            )
        if is_directory:
            if info.file_size != 0:
                raise LumiReleaseError(
                    "release archive contains an invalid directory entry"
                )
            continue
        if info.file_size < 0 or info.compress_size < 0:
            raise LumiReleaseError("release archive has an invalid entry size")
        if info.file_size and info.compress_size == 0:
            raise LumiReleaseError(
                "release archive contains a suspicious compressed entry"
            )
        if info.compress_size and info.file_size / info.compress_size > 200:
            raise LumiReleaseError(
                "release archive compression ratio exceeds the limit"
            )
        result[name] = info
    return result


def _extract_package_archive(
    archive_bytes: bytes,
    package_root: Path,
    manifest: LumiReleaseManifest,
) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            infos = _validate_zip_entries(archive.infolist(), MAX_ARCHIVE_ENTRIES)
            _validate_package_archive_members(infos, manifest.package_files)
            total = 0
            for relative, (
                expected_size,
                expected_digest,
            ) in manifest.package_files.items():
                info = infos[relative]
                if info.file_size != expected_size:
                    raise LumiReleaseError(
                        f"Lumi package file {relative} has an unexpected size"
                    )
                total += expected_size
                if total > MAX_PACKAGE_BYTES:
                    raise LumiReleaseError(
                        "Lumi package exceeds the expanded size limit"
                    )
                payload = archive.read(info)
                if (
                    len(payload) != expected_size
                    or hashlib.sha256(payload).hexdigest() != expected_digest
                ):
                    raise LumiReleaseError(
                        f"Lumi package file {relative} failed its SHA-256 check"
                    )
                destination = package_root.joinpath(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
            manifest_bytes = archive.read(LUMI_RELEASE_MANIFEST_NAME)
            (package_root / LUMI_RELEASE_MANIFEST_NAME).write_bytes(manifest_bytes)
    except (zipfile.BadZipFile, OSError, RuntimeError) as error:
        if isinstance(error, LumiReleaseError):
            raise
        raise LumiReleaseError("Lumi package could not be safely extracted") from error


def _validate_wheel_archive(
    wheel_bytes: bytes,
    dependency: RuntimeDependency,
    *,
    installer: bool = False,
) -> int:
    kind = "model installer" if installer else "runtime"
    expanded_limit = (
        MAX_INSTALLER_WHEEL_EXPANDED_BYTES if installer else MAX_WHEEL_EXPANDED_BYTES
    )
    try:
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as wheel:
            infos = _validate_zip_entries(wheel.infolist(), MAX_ARCHIVE_ENTRIES)
            metadata_names = [
                name for name in infos if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise LumiReleaseError(f"{kind} wheel has no unique distribution METADATA")
            if infos[metadata_names[0]].file_size > 64 * 1024:
                raise LumiReleaseError(f"{kind} wheel METADATA exceeds the size limit")
            metadata = wheel.read(metadata_names[0]).decode("utf-8", errors="strict")
            name_value, version_value = _wheel_metadata_identity(metadata)
            if (
                _normalize_distribution(name_value)
                != _normalize_distribution(dependency.distribution)
                or version_value != dependency.version
            ):
                raise LumiReleaseError(f"{kind} wheel METADATA does not match its pinned identity")
            if _normalize_distribution(dependency.distribution) == "onnxruntime-genai":
                native_suffix = (
                    ".pyd" if dependency.platform_tag.startswith("win_") else ".so"
                )
                if not any(
                    name.startswith("onnxruntime_genai/")
                    and name.lower().endswith(native_suffix)
                    for name in infos
                ):
                    raise LumiReleaseError(
                        "pinned onnxruntime-genai wheel has no host-native extension binary"
                    )
            expanded = sum(info.file_size for info in infos.values())
            if expanded > expanded_limit:
                raise LumiReleaseError(f"{kind} wheel exceeds the expanded size limit")
            return expanded
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, RuntimeError) as error:
        if isinstance(error, LumiReleaseError):
            raise
        raise LumiReleaseError(f"{kind} dependency is not a valid wheel archive") from error


def _wheel_metadata_identity(metadata: str) -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in metadata.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.lower() in {"name", "version"}:
            values[key.lower()] = value.strip()
    if not values.get("name") or not values.get("version"):
        raise LumiReleaseError("runtime wheel METADATA omits Name or Version")
    return values["name"], values["version"]


def _extract_wheel(
    wheel_bytes: bytes,
    destination_root: Path,
    dependency: RuntimeDependency,
    *,
    installer: bool = False,
) -> None:
    kind = "model installer" if installer else "runtime"
    max_file_bytes = (
        MAX_INSTALLER_WHEEL_FILE_BYTES if installer else MAX_WHEEL_FILE_BYTES
    )
    max_expanded_bytes = (
        MAX_INSTALLER_WHEEL_EXPANDED_BYTES if installer else MAX_WHEEL_EXPANDED_BYTES
    )
    try:
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as wheel:
            infos = _validate_zip_entries(wheel.infolist(), MAX_ARCHIVE_ENTRIES)
            expanded = 0
            for name, info in infos.items():
                if info.file_size > max_file_bytes:
                    raise LumiReleaseError(f"{kind} wheel contains an oversized file")
                expanded += info.file_size
                if expanded > max_expanded_bytes:
                    raise LumiReleaseError(f"{kind} wheel exceeds the expanded size limit")
                relative = _wheel_install_path(name, dependency)
                if relative is None:
                    continue
                payload = wheel.read(info)
                target = destination_root.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
    except (zipfile.BadZipFile, OSError, RuntimeError) as error:
        if isinstance(error, LumiReleaseError):
            raise
        raise LumiReleaseError(f"{kind} dependency could not be safely extracted") from error


def _wheel_install_path(
    name: str, dependency: RuntimeDependency
) -> PurePosixPath | None:
    relative = _validate_relative_member(name)
    parts = relative.parts
    data_index = next(
        (
            index
            for index, part in enumerate(parts)
            if part.endswith(".data") and index == 0
        ),
        None,
    )
    if data_index is not None:
        if len(parts) < 3 or parts[1] not in {"purelib", "platlib"}:
            raise LumiReleaseError(
                "runtime wheel contains unsupported data or script files"
            )
        parts = parts[2:]
        if not parts:
            raise LumiReleaseError("runtime wheel contains an invalid library path")
        relative = PurePosixPath(*parts)
    return relative


def _dependency_directories(release_directory: Path) -> tuple[Path, ...]:
    return _dependency_directories_from_root(release_directory / "dependencies")


def _installer_dependency_directories(release_directory: Path) -> tuple[Path, ...]:
    return _dependency_directories_from_root(
        release_directory / "installer-dependencies" / "dependencies"
    )


def _dependency_directories_from_root(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    return tuple(
        sorted(path / "site-packages" for path in root.iterdir() if path.is_dir())
    )


def _installer_dependency_manifest(
    tag: str,
    dependencies: Sequence[RuntimeDependency],
) -> dict[str, Any]:
    return {
        "tag": tag,
        "wheels": [
            {"asset": dependency.asset, "sha256": dependency.sha256}
            for dependency in dependencies
        ],
    }


def _installer_dependencies_are_valid(
    root: Path,
    tag: str,
    dependencies: Sequence[RuntimeDependency],
) -> bool:
    if root.is_symlink() or not root.is_dir():
        return False
    try:
        marker = json.loads((root / "lumi-installer-wheels.json").read_text(encoding="utf-8"))
        expected = _installer_dependency_manifest(tag, dependencies)
        if marker != expected:
            return False
        wheelhouse = root / "wheelhouse"
        dependency_root = root / "dependencies"
        if (
            wheelhouse.is_symlink()
            or not wheelhouse.is_dir()
            or dependency_root.is_symlink()
            or not dependency_root.is_dir()
        ):
            return False
        for dependency in dependencies:
            wheel = wheelhouse / dependency.asset
            if wheel.is_symlink() or not wheel.is_file():
                return False
            if _sha256_file(wheel) != dependency.sha256:
                return False
            package_directory = (
                dependency_root
                / f"{_normalize_distribution(dependency.distribution)}-{dependency.version}"
                / "site-packages"
            )
            if (
                package_directory.parent.is_symlink()
                or package_directory.is_symlink()
                or not package_directory.is_dir()
            ):
                return False
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return True


def _managed_release_root(managed_data_path: Path) -> Path:
    base = managed_data_path.resolve()
    base.mkdir(parents=True, exist_ok=True)
    lumi_root = base / "lumi"
    if lumi_root.is_symlink() and not _is_relative_to(lumi_root.resolve(), base):
        raise LumiReleaseError(
            "managed Lumi path resolves outside the Orchestrator data root"
        )
    lumi_root.mkdir(exist_ok=True)
    lumi_root = lumi_root.resolve()
    if not _is_relative_to(lumi_root, base):
        raise LumiReleaseError(
            "managed Lumi path resolves outside the Orchestrator data root"
        )
    release_root = lumi_root / "releases"
    if release_root.is_symlink() and not _is_relative_to(release_root.resolve(), base):
        raise LumiReleaseError(
            "managed Lumi release path resolves outside the Orchestrator data root"
        )
    release_root.mkdir(exist_ok=True)
    release_root = release_root.resolve()
    if not _is_relative_to(release_root, base):
        raise LumiReleaseError(
            "managed Lumi release path resolves outside the Orchestrator data root"
        )
    return release_root


def _remove_managed_path(path: Path, *, ignore_errors: bool = False) -> None:
    try:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError:
        if not ignore_errors:
            raise


def _import_managed_lumi(
    package_directory: Path, dependency_directories: Sequence[Path]
) -> ImportedLumiModules:
    package_directory = package_directory.resolve()
    with _IMPORT_LOCK:
        existing = sys.modules.get("lumi")
        if existing is not None:
            existing_file = getattr(existing, "__file__", None)
            if not existing_file or not _is_relative_to(
                Path(existing_file).resolve(), package_directory
            ):
                raise LumiReleaseError(
                    "another lumi package is already loaded in this process"
                )
        paths = [
            str(path.resolve()) for path in (*dependency_directories, package_directory)
        ]
        for path in reversed(paths):
            if path not in sys.path:
                sys.path.insert(0, path)
        before = set(sys.modules)
        try:
            _register_dll_directories(package_directory, dependency_directories)
            modules = ImportedLumiModules(
                package=importlib.import_module("lumi"),
                runtime=importlib.import_module("lumi.runtime"),
                service_factory=importlib.import_module("lumi.service_factory"),
            )
            for module in (modules.package, modules.runtime, modules.service_factory):
                module_file = getattr(module, "__file__", None)
                if not module_file or not _is_relative_to(
                    Path(module_file).resolve(), package_directory
                ):
                    raise LumiReleaseError(
                        "Lumi runtime import resolved outside its managed release"
                    )
            return modules
        except Exception:
            for name in tuple(sys.modules):
                if name not in before and (name == "lumi" or name.startswith("lumi.")):
                    sys.modules.pop(name, None)
            for path in paths:
                while path in sys.path:
                    sys.path.remove(path)
            if not _has_loaded_native_extension(dependency_directories):
                _close_dll_directory_handles(package_directory)
            raise


def _unload_managed_lumi(
    package_directory: Path, dependency_directories: Sequence[Path]
) -> None:
    package_directory = package_directory.resolve()
    module_roots = tuple(
        path.resolve() for path in (*dependency_directories, package_directory)
    )
    paths = {str(path) for path in module_roots}
    native_suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    with _IMPORT_LOCK:
        for name, module in tuple(sys.modules.items()):
            module_file = getattr(module, "__file__", None)
            module_paths = [module_file] if isinstance(module_file, str) else []
            namespace_paths = getattr(module, "__path__", ())
            if not isinstance(namespace_paths, (str, bytes)):
                try:
                    module_paths.extend(
                        path for path in namespace_paths if isinstance(path, str)
                    )
                except TypeError:
                    pass
            if module_file and module_file.endswith(native_suffixes):
                continue
            if any(
                _is_relative_to(Path(module_path).resolve(), root)
                for module_path in module_paths
                for root in module_roots
            ):
                sys.modules.pop(name, None)
        for path in paths:
            while path in sys.path:
                sys.path.remove(path)
        if not _has_loaded_native_extension(dependency_directories):
            _close_dll_directory_handles(package_directory)


def _register_dll_directories(
    package_directory: Path, dependency_directories: Sequence[Path]
) -> None:
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if not callable(add_dll_directory):
        return
    key = str(package_directory.resolve())
    if key in _DLL_DIRECTORY_HANDLES:
        return
    dll_directories: set[Path] = set()
    for root in dependency_directories:
        for candidate in root.rglob("*"):
            if candidate.is_file() and candidate.suffix.lower() in {".dll", ".pyd"}:
                dll_directories.add(candidate.parent.resolve())
    handles = tuple(add_dll_directory(str(path)) for path in sorted(dll_directories))
    _DLL_DIRECTORY_HANDLES[key] = handles


def _close_dll_directory_handles(package_directory: Path) -> None:
    handles = _DLL_DIRECTORY_HANDLES.pop(str(package_directory.resolve()), ())
    for handle in handles:
        close = getattr(handle, "close", None)
        if callable(close):
            close()


def _has_loaded_native_extension(dependency_directories: Sequence[Path]) -> bool:
    roots = tuple(path.resolve() for path in dependency_directories)
    if not roots:
        return False
    suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    for module in tuple(sys.modules.values()):
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str) or not module_file.endswith(suffixes):
            continue
        path = Path(module_file).resolve()
        if any(_is_relative_to(path, root) for root in roots):
            return True
    return False


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
