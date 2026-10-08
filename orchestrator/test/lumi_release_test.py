import asyncio
import hashlib
import importlib
import importlib.machinery
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from app.lumi_release import (
    LUMI_GITHUB_API,
    LUMI_GITHUB_REPOSITORY,
    RELEASE_LIST_CACHE_TTL_SECONDS,
    HttpResponse,
    ImportedLumiModules,
    LumiReleaseCompatibilityError,
    LumiReleaseError,
    LumiReleaseManager,
    LumiReleaseRequestError,
    LumiReleaseUnavailable,
    RuntimeDependency,
    RuntimeHost,
    _extract_wheel,
    _InstallTransaction,
    _import_managed_lumi,
    _matching_dependencies,
    _parse_runtime_dependencies,
    _replace_managed_directory,
    _unload_managed_lumi,
    _validate_download_url,
    _validate_wheel_archive,
)

TAG = "v1.2.3"
ORCHESTRATOR_VERSION = "1.7.3"
HOST = RuntimeHost("cp312", "cp312", ("win_amd64",))
WHEEL_NAME = "onnxruntime_genai-0.17.1-cp312-cp312-win_amd64.whl"
WHEEL_DISTRIBUTION = "onnxruntime-genai"
WHEEL_VERSION = "0.17.1"


def _sha256(content):
    return hashlib.sha256(content).hexdigest()


def _make_wheel():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("onnxruntime_genai/__init__.py", b"__version__ = '0.17.1'\n")
        archive.writestr(
            "onnxruntime_genai/capi/_native.cp312-win_amd64.pyd",
            b"native wheel fixture",
        )
        archive.writestr(
            "onnxruntime_genai-0.17.1.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: onnxruntime-genai\nVersion: 0.17.1\n",
        )
    return buffer.getvalue()


def _make_llama_cpp_wheel():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("llama_cpp/__init__.py", b"__version__ = '0.3.35'\n")
        archive.writestr(
            "llama_cpp/_llama_cpp.cp312-win_amd64.pyd",
            b"native wheel fixture",
        )
        archive.writestr(
            "llama_cpp_python-0.3.35.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: llama-cpp-python\nVersion: 0.3.35\n",
        )
    return buffer.getvalue()


def _make_installer_wheel(
    distribution,
    version,
    package,
    *,
    compressible_bytes=0,
    nested_metadata=False,
):
    buffer = io.BytesIO()
    wheel_distribution = distribution.replace("-", "_")
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{package}/__init__.py", b"__version__ = 'test'\n")
        archive.writestr(
            f"{wheel_distribution}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n",
        )
        if nested_metadata:
            archive.writestr(
                f"{package}/vendor-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: vendor\nVersion: 1.0\n",
            )
        if compressible_bytes:
            archive.writestr(
                f"{package}/compressible-resource.bin",
                b"0" * compressible_bytes,
            )
    return buffer.getvalue()


def _make_installer_dependencies(*, torch_platform_tag="win_amd64"):
    specifications = (
        ("huggingface-hub", "0.30.0", "huggingface_hub", "py3", "none", "any"),
        ("onnx-ir", "0.1.0", "onnx_ir", "py3", "none", "any"),
        ("torch", "2.5.1", "torch", "cp312", "cp312", torch_platform_tag),
        ("transformers", "4.48.0", "transformers", "py3", "none", "any"),
    )
    dependencies = []
    payloads = {}
    for (
        distribution,
        version,
        package,
        python_tag,
        abi_tag,
        platform_tag,
    ) in specifications:
        asset_distribution = distribution.replace("-", "_")
        asset = (
            f"{asset_distribution}-{version}-{python_tag}-{abi_tag}-{platform_tag}.whl"
        )
        content = _make_installer_wheel(distribution, version, package)
        dependencies.append(
            {
                "distribution": distribution,
                "version": version,
                "pythonTag": python_tag,
                "abiTag": abi_tag,
                "platformTag": platform_tag,
                "asset": asset,
                "sha256": _sha256(content),
            }
        )
        payloads[asset] = content
    return dependencies, payloads


def _make_package_archive(
    *, runtime_api_version=1, extra_member=None, installer_dependencies=()
):
    package_sources = {
        "lumi/__init__.py": (
            b'"""Lumi runtime package."""\nLUMI_PLUGIN_API_VERSION = 1\n'
        ),
        "lumi/runtime/__init__.py": (
            b"LUMI_RUNTIME_API_VERSION = "
            + str(runtime_api_version).encode("ascii")
            + b"\n"
            + b"class OrtGenAIConfig:\n    pass\n"
            + b"class VerifiedModelArtifact:\n    pass\n"
            + b"class OrtGenAIChatRuntime:\n"
            + b"    def __init__(self, configuration):\n"
            + b"        self.configuration = configuration\n"
            + b"    async def close(self):\n        pass\n"
        ),
        "lumi/service_factory.py": (
            b"class EmbeddedService:\n"
            b"    def __init__(self, configuration):\n"
            b"        self.configuration = configuration\n"
            b"        self.closed = False\n"
            b"    async def close(self):\n"
            b"        self.closed = True\n"
            b"def create_embedded_service(configuration):\n"
            b"    return EmbeddedService(configuration)\n"
        ),
    }
    wheel_bytes = _make_wheel()
    manifest = {
        "schemaVersion": 1,
        "tag": TAG,
        "runtimeApiVersion": 1,
        "minimumOrchestratorVersion": "1.7.0",
        "maximumOrchestratorVersionExclusive": "2.0.0",
        "files": {
            name: {"size": len(content), "sha256": _sha256(content)}
            for name, content in package_sources.items()
        },
        "runtimeDependencies": [
            {
                "distribution": WHEEL_DISTRIBUTION,
                "version": WHEEL_VERSION,
                "pythonTag": "cp312",
                "abiTag": "cp312",
                "platformTag": "win_amd64",
                "asset": WHEEL_NAME,
                "sha256": _sha256(wheel_bytes),
            }
        ],
    }
    if installer_dependencies:
        manifest["installerDependencies"] = installer_dependencies
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("lumi-release.json", json.dumps(manifest))
        for name, content in package_sources.items():
            archive.writestr(name, content)
        if extra_member is not None:
            archive.writestr(extra_member[0], extra_member[1])
    return buffer.getvalue(), wheel_bytes, manifest


class FakeGitHub:
    def __init__(
        self,
        *,
        missing_assets=(),
        draft=False,
        prerelease=False,
        release_tag=TAG,
        runtime_api_version=1,
        extra_member=None,
        metadata_final_url=None,
        with_installer_dependencies=False,
        installer_torch_platform="win_amd64",
    ):
        self.requests = []
        self.metadata_final_url = metadata_final_url
        self.package_bytes, self.wheel_bytes, self.manifest = _make_package_archive(
            runtime_api_version=runtime_api_version,
            extra_member=extra_member,
        )
        package_name = "lumi-runtime.zip"
        wheel_sha256 = _sha256(self.wheel_bytes)
        package_sha256 = _sha256(self.package_bytes)
        self.payloads = {
            package_name: self.package_bytes,
            WHEEL_NAME: self.wheel_bytes,
        }
        if with_installer_dependencies:
            installer_dependencies, installer_payloads = _make_installer_dependencies(
                torch_platform_tag=installer_torch_platform
            )
            self.package_bytes, self.wheel_bytes, self.manifest = _make_package_archive(
                runtime_api_version=runtime_api_version,
                extra_member=extra_member,
                installer_dependencies=installer_dependencies,
            )
            self.installer_payloads = installer_payloads
            self.payloads = {
                package_name: self.package_bytes,
                WHEEL_NAME: self.wheel_bytes,
                **installer_payloads,
            }
        assets = [
            self._asset(package_name, self.package_bytes, release_tag),
            self._asset(WHEEL_NAME, self.wheel_bytes, release_tag),
        ]
        assets.extend(
            self._asset(name, content, release_tag)
            for name, content in self.payloads.items()
            if name not in {package_name, WHEEL_NAME}
        )
        assets = [asset for asset in assets if asset["name"] not in missing_assets]
        self.metadata = {
            "tag_name": release_tag,
            "html_url": (
                f"https://github.com/{LUMI_GITHUB_REPOSITORY}/releases/tag/{release_tag}"
            ),
            "full_name": LUMI_GITHUB_REPOSITORY,
            "published_at": "2026-01-01T00:00:00Z",
            "draft": draft,
            "prerelease": prerelease,
            "assets": assets,
        }
        self.releases = [self.metadata]
        self.metadata_bytes = json.dumps(self.metadata).encode("utf-8")
        self.digest_values = (package_sha256, wheel_sha256)

    @staticmethod
    def _asset(name, content, tag):
        return {
            "name": name,
            "size": len(content),
            "digest": f"sha256:{_sha256(content)}",
            "browser_download_url": (
                f"https://github.com/{LUMI_GITHUB_REPOSITORY}/releases/download/{tag}/{name}"
            ),
        }

    def fetch(self, url, headers, max_bytes):
        self.requests.append(url)
        metadata_url = f"{LUMI_GITHUB_API}/releases/tags/{TAG}"
        if url == metadata_url:
            body = self.metadata_bytes
            final_url = self.metadata_final_url or url
        elif url.startswith(f"{LUMI_GITHUB_API}/releases?per_page="):
            body = json.dumps(self.releases).encode("utf-8")
            final_url = url
        else:
            name = url.rsplit("/", 1)[-1]
            body = self.payloads.get(name)
            if body is None:
                raise LumiReleaseUnavailable("asset not found")
            final_url = url
        if len(body) > max_bytes:
            raise AssertionError("test payload exceeded requested byte limit")
        return HttpResponse(body, final_url, {"Content-Length": str(len(body))})


class FakeLlamaCppGitHub(FakeGitHub):
    def __init__(self):
        self.requests = []
        self.metadata_final_url = None
        self.wheel_bytes = _make_llama_cpp_wheel()
        installer_bytes = _make_installer_wheel(
            "huggingface-hub", "1.10.0", "huggingface_hub"
        )
        wheel_name = "llama_cpp_python-0.3.35-py3-none-win_amd64.whl"
        installer_name = "huggingface_hub-1.10.0-py3-none-any.whl"
        installer_dependency = {
            "distribution": "huggingface-hub",
            "version": "1.10.0",
            "pythonTag": "py3",
            "abiTag": "none",
            "platformTag": "any",
            "asset": installer_name,
            "sha256": _sha256(installer_bytes),
        }
        package_sources = {
            "lumi/__init__.py": (
                b'"""Lumi runtime package."""\nLUMI_PLUGIN_API_VERSION = 1\n'
            ),
            "lumi/runtime/__init__.py": (
                b"LUMI_RUNTIME_API_VERSION = 1\n"
                b"class VerifiedModelArtifact:\n    pass\n"
                b"class LlamaCppConfig:\n    pass\n"
                b"class LlamaCppChatRuntime:\n"
                b"    def __init__(self, configuration):\n"
                b"        self.configuration = configuration\n"
                b"    async def close(self):\n        pass\n"
            ),
            "lumi/service_factory.py": (
                b"class EmbeddedService:\n"
                b"    async def close(self):\n        pass\n"
                b"def create_embedded_service(**_kwargs):\n"
                b"    return EmbeddedService()\n"
            ),
        }
        self.manifest = {
            "schemaVersion": 1,
            "tag": TAG,
            "runtimeApiVersion": 1,
            "minimumOrchestratorVersion": "1.7.3",
            "maximumOrchestratorVersionExclusive": "2.0.0",
            "files": {
                name: {"size": len(content), "sha256": _sha256(content)}
                for name, content in package_sources.items()
            },
            "runtimeDependencies": [
                {
                    "distribution": "llama-cpp-python",
                    "version": "0.3.35",
                    "pythonTag": "py3",
                    "abiTag": "none",
                    "platformTag": "win_amd64",
                    "asset": wheel_name,
                    "sha256": _sha256(self.wheel_bytes),
                }
            ],
            "installerDependencies": [installer_dependency],
        }
        package_buffer = io.BytesIO()
        with zipfile.ZipFile(
            package_buffer, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("lumi-release.json", json.dumps(self.manifest))
            for name, content in package_sources.items():
                archive.writestr(name, content)
        self.package_bytes = package_buffer.getvalue()
        self.payloads = {
            "lumi-runtime.zip": self.package_bytes,
            wheel_name: self.wheel_bytes,
            installer_name: installer_bytes,
        }
        self.installer_payloads = {installer_name: installer_bytes}
        self.metadata = {
            "tag_name": TAG,
            "html_url": f"https://github.com/{LUMI_GITHUB_REPOSITORY}/releases/tag/{TAG}",
            "full_name": LUMI_GITHUB_REPOSITORY,
            "published_at": "2026-01-01T00:00:00Z",
            "draft": False,
            "prerelease": False,
            "assets": [
                self._asset(name, content, TAG)
                for name, content in self.payloads.items()
            ],
        }
        self.releases = [self.metadata]
        self.metadata_bytes = json.dumps(self.metadata).encode("utf-8")


class ManagedReleaseFilesystemTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary_directory.name) / "managed-data"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_commit_install_removes_previous_release_directory(self):
        release_directory = self.data_root / "lumi" / "releases" / TAG
        previous_directory = release_directory.with_name(f"{TAG}.previous")
        release_directory.mkdir(parents=True)
        previous_directory.mkdir()
        (previous_directory / "old.txt").write_text("old", encoding="utf-8")

        LumiReleaseManager._commit_install(
            _InstallTransaction(release_directory, previous_directory)
        )

        self.assertTrue(release_directory.is_dir())
        self.assertFalse(previous_directory.exists())

    def test_managed_directory_replace_retries_transient_permission_error(self):
        source = self.data_root / "staged"
        destination = self.data_root / "active"
        source.mkdir(parents=True)
        (source / "marker.txt").write_text("ready", encoding="utf-8")
        original_replace = os.replace
        attempts = 0

        def replace_after_transient_error(source_path, destination_path):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError(5, "directory is temporarily locked")
            return original_replace(source_path, destination_path)

        with (
            patch(
                "app.lumi_release.os.replace",
                side_effect=replace_after_transient_error,
            ),
            patch("app.lumi_release.time.sleep") as sleep_mock,
            patch("app.lumi_release._DIRECTORY_REPLACE_RETRY_DELAYS_SECONDS", (0.0,)),
        ):
            _replace_managed_directory(source, destination)

        self.assertEqual(attempts, 2)
        sleep_mock.assert_called_once_with(0.0)
        self.assertTrue((destination / "marker.txt").is_file())

    def test_managed_directory_replace_stops_after_retry_limit(self):
        source = self.data_root / "staged"
        destination = self.data_root / "active"
        source.mkdir(parents=True)
        with (
            patch(
                "app.lumi_release.os.replace",
                side_effect=PermissionError(5, "directory remains locked"),
            ) as replace_mock,
            patch("app.lumi_release.time.sleep") as sleep_mock,
            patch(
                "app.lumi_release._DIRECTORY_REPLACE_RETRY_DELAYS_SECONDS",
                (0.0,),
            ),
        ):
            with self.assertRaises(PermissionError):
                _replace_managed_directory(source, destination)

        self.assertEqual(replace_mock.call_count, 2)
        sleep_mock.assert_called_once_with(0.0)


class LumiReleaseManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary_directory.name) / "managed-data"
        self.managers = []

    async def asyncTearDown(self):
        for manager in reversed(self.managers):
            if manager.active_release is not None:
                await manager.disable()
        self.temporary_directory.cleanup()

    def test_signed_github_asset_redirects_accept_query_on_asset_cdn_hosts(self):
        """Accept signed URLs only on the pinned GitHub asset CDN hosts."""
        for host in (
            "objects.githubusercontent.com",
            "release-assets.githubusercontent.com",
        ):
            with self.subTest(host=host):
                _validate_download_url(
                    f"https://{host}/release/lumi-runtime.zip?token=signed-value"
                )

    def test_release_asset_redirect_queries_remain_restricted(self):
        """Reject signed queries on GitHub metadata and untrusted origins."""
        for url in (
            "https://github.com/Loco-CTO/zenstream-lumi/releases/download/"
            "v0.1.2/lumi-runtime.zip?token=unexpected",
            "https://api.github.com/repos/Loco-CTO/zenstream-lumi/releases?token=x",
            "https://attacker.invalid/lumi-runtime.zip?token=x",
            "https://release-assets.githubusercontent.com/release/file?token=x#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(LumiReleaseError):
                _validate_download_url(url)

    def _manager(self, github, *, importer=None, unloader=None, host=HOST):
        manager = LumiReleaseManager(
            self.data_root,
            ORCHESTRATOR_VERSION,
            runtime_host=host,
            fetcher=github.fetch,
            importer=importer,
            unloader=unloader,
        )
        self.managers.append(manager)
        return manager

    def test_runtime_host_current_detects_linux_cpython_abi(self):
        runtime_sys = SimpleNamespace(
            implementation=SimpleNamespace(name="cpython"),
            version_info=(3, 13, 0),
            platform="linux",
        )
        with (
            patch("app.lumi_release.sys", runtime_sys),
            patch(
                "app.lumi_release.sysconfig.get_config_var",
                return_value="cpython-313-x86_64-linux-gnu",
            ),
            patch("app.lumi_release.platform.machine", return_value="x86_64"),
            patch("app.lumi_release.platform.libc_ver", return_value=("glibc", "2.36")),
        ):
            host = RuntimeHost.current()

        self.assertEqual(host.python_tag, "cp313")
        self.assertEqual(host.abi_tag, "cp313")
        self.assertIn("manylinux_2_17_x86_64", host.platform_tags)
        self.assertIn("manylinux2014_x86_64", host.platform_tags)
        dependency = RuntimeDependency(
            "onnxruntime-genai",
            "0.17.1",
            "cp313",
            "cp313",
            "manylinux_2_17_x86_64",
            "onnxruntime_genai-0.17.1-cp313-cp313-manylinux_2_17_x86_64.whl",
            "0" * 64,
        )
        self.assertEqual(_matching_dependencies((dependency,), host), (dependency,))

    def test_runtime_host_current_uses_cache_tag_when_soabi_is_missing(self):
        runtime_sys = SimpleNamespace(
            implementation=SimpleNamespace(name="cpython", cache_tag="cpython-312"),
            version_info=(3, 12, 14),
            platform="win32",
        )
        with (
            patch("app.lumi_release.sys", runtime_sys),
            patch("app.lumi_release.sysconfig.get_config_var", return_value=None),
            patch("app.lumi_release.platform.machine", return_value="AMD64"),
        ):
            host = RuntimeHost.current()

        self.assertEqual(host.python_tag, "cp312")
        self.assertEqual(host.abi_tag, "cp312")
        self.assertEqual(host.platform_tags, ("win_amd64",))

    def test_runtime_manifest_accepts_all_release_target_wheels(self):
        dependencies = []
        target_platforms = (
            "win_amd64",
            "manylinux_2_17_x86_64.manylinux2014_x86_64",
            "manylinux_2_17_aarch64.manylinux2014_aarch64",
        )
        for python_tag in ("cp312", "cp313"):
            for platform_tag in target_platforms:
                for distribution in ("numpy", "onnxruntime_genai", "protobuf"):
                    asset = (
                        f"{distribution}-1.0.0-{python_tag}-{python_tag}-"
                        f"{platform_tag}.whl"
                    )
                    dependencies.append(
                        {
                            "distribution": distribution,
                            "version": "1.0.0",
                            "pythonTag": python_tag,
                            "abiTag": python_tag,
                            "platformTag": platform_tag,
                            "asset": asset,
                            "sha256": "0" * 64,
                        }
                    )
        for index, (distribution, python_tag) in enumerate(
            (
                ("flatbuffers", "py2.py3"),
                ("packaging", "py3"),
                ("idna", "py3"),
                ("protobuf-runtime", "py3"),
                ("typing-extensions", "py3"),
            )
        ):
            asset = f"{distribution}-{index + 1}.0.0-{python_tag}-none-any.whl"
            dependencies.append(
                {
                    "distribution": distribution,
                    "version": f"{index + 1}.0.0",
                    "pythonTag": python_tag,
                    "abiTag": "none",
                    "platformTag": "any",
                    "asset": asset,
                    "sha256": "0" * 64,
                }
            )

        parsed = _parse_runtime_dependencies(dependencies)

        self.assertEqual(len(parsed), 23)

    def test_runtime_dependency_matching_supports_abi3_and_compound_tags(self):
        host = RuntimeHost(
            "cp314",
            "cp314",
            (
                "manylinux_2_28_aarch64",
                "manylinux_2_17_aarch64",
                "manylinux2014_aarch64",
                "linux_aarch64",
            ),
        )
        abi3_dependency = RuntimeDependency(
            "protobuf",
            "6.0.0",
            "cp310",
            "abi3",
            "manylinux_2_17_aarch64.manylinux2014_aarch64",
            "protobuf-6.0.0-cp310-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
            "0" * 64,
        )
        universal_dependency = RuntimeDependency(
            "flatbuffers",
            "25.0.0",
            "py2.py3",
            "none",
            "any",
            "flatbuffers-25.0.0-py2.py3-none-any.whl",
            "1" * 64,
        )
        exact_dependency = RuntimeDependency(
            "numpy",
            "2.5.3",
            "cp314",
            "cp314",
            "manylinux_2_17_aarch64.manylinux2014_aarch64",
            "numpy-2.5.3-cp314-cp314-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
            "2" * 64,
        )
        unsupported_dependency = RuntimeDependency(
            "numpy",
            "2.5.3",
            "cp314",
            "cp314",
            "win_amd64",
            "numpy-2.5.3-cp314-cp314-win_amd64.whl",
            "3" * 64,
        )

        self.assertEqual(
            _matching_dependencies(
                (
                    abi3_dependency,
                    universal_dependency,
                    exact_dependency,
                    unsupported_dependency,
                ),
                host,
            ),
            (abi3_dependency, universal_dependency, exact_dependency),
        )

    async def test_release_listing_returns_stable_candidates_without_downloading_assets(
        self,
    ):
        github = FakeGitHub()
        manager = self._manager(github)

        candidates = await manager.list_published_releases()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].tag, TAG)
        self.assertEqual(candidates[0].package_sha256, github.digest_values[0])
        self.assertEqual(github.requests, [f"{LUMI_GITHUB_API}/releases?per_page=20"])

    async def test_release_listing_returns_empty_when_github_has_no_releases(self):
        github = FakeGitHub()
        github.releases = []
        manager = self._manager(github)

        candidates = await manager.list_published_releases()

        self.assertEqual(candidates, ())
        self.assertEqual(len(github.requests), 1)

    async def test_release_listing_filters_non_stable_and_assetless_releases(self):
        github = FakeGitHub()
        github.releases = [
            {**github.metadata, "tag_name": "v1.2.4-rc.1", "prerelease": True},
            {**github.metadata, "tag_name": "v1.2.4", "assets": []},
            github.metadata,
        ]
        manager = self._manager(github)

        candidates = await manager.list_published_releases(limit=3)

        self.assertEqual([candidate.tag for candidate in candidates], [TAG])

    async def test_release_listing_cache_survives_restart_and_rate_limits(self):
        github = FakeGitHub()
        first_manager = self._manager(github)
        with patch("app.lumi_release.time.time", return_value=1_000_000):
            original_candidates = await first_manager.list_published_releases()

        unavailable_requests = []

        def unavailable_fetcher(url, *_args):
            unavailable_requests.append(url)
            raise LumiReleaseRequestError("GitHub is rate-limiting requests")

        fresh_manager = LumiReleaseManager(
            self.data_root,
            ORCHESTRATOR_VERSION,
            runtime_host=HOST,
            fetcher=unavailable_fetcher,
        )
        self.managers.append(fresh_manager)
        with patch("app.lumi_release.time.time", return_value=1_000_001):
            fresh_candidates = await fresh_manager.list_published_releases()
        self.assertEqual(fresh_candidates, original_candidates)
        self.assertEqual(unavailable_requests, [])

        stale_manager = LumiReleaseManager(
            self.data_root,
            ORCHESTRATOR_VERSION,
            runtime_host=HOST,
            fetcher=unavailable_fetcher,
        )
        self.managers.append(stale_manager)
        with patch(
            "app.lumi_release.time.time",
            return_value=1_000_000 + RELEASE_LIST_CACHE_TTL_SECONDS + 1,
        ):
            stale_candidates = await stale_manager.list_published_releases()
        self.assertEqual(stale_candidates, original_candidates)
        self.assertEqual(len(unavailable_requests), 1)

    async def test_enable_installs_pinned_release_and_disable_closes_and_unloads(self):
        github = FakeGitHub()
        manager = self._manager(github)

        self.assertIsNone(manager.active_release)
        self.assertFalse(self.data_root.exists())
        release = await manager.enable(TAG)

        self.assertEqual(release.tag, TAG)
        self.assertEqual(release.runtime_api_version, 1)
        self.assertTrue(
            release.directory.is_relative_to(self.data_root / "lumi" / "releases")
        )
        self.assertEqual(github.requests[0], f"{LUMI_GITHUB_API}/releases/tags/{TAG}")
        self.assertNotIn("/releases/latest", " ".join(github.requests))
        service = release.create_embedded_service({"model": "test"})
        dependency_root = next(
            (release.directory / "dependencies").glob("*/site-packages")
        )
        self.assertTrue(
            (dependency_root / "onnxruntime_genai" / "__init__.py").is_file()
        )
        self.assertIn(str(dependency_root.resolve()), sys.path)
        runtime_dependency = importlib.import_module("onnxruntime_genai")
        self.assertTrue(
            Path(runtime_dependency.__file__)
            .resolve()
            .is_relative_to(dependency_root.resolve())
        )
        self.assertEqual(service.configuration, {"model": "test"})

        await manager.disable()

        self.assertTrue(service.closed)
        self.assertIsNone(manager.active_release)
        self.assertNotIn(str(dependency_root.resolve()), sys.path)
        self.assertNotIn(str((release.directory / "package").resolve()), sys.path)
        self.assertNotIn("lumi", sys.modules)
        self.assertNotIn("onnxruntime_genai", sys.modules)
        self.assertTrue(release.directory.is_dir())

    async def test_release_activation_does_not_require_directory_rename(self):
        github = FakeGitHub()
        manager = self._manager(github)
        with patch("app.lumi_release.os.replace", wraps=os.replace) as replace_mock:
            release = await manager.enable(TAG)

        self.assertTrue(
            all(
                not Path(call.args[0]).is_dir()
                for call in replace_mock.call_args_list
            )
        )
        self.assertTrue(release.directory.is_dir())
        self.assertTrue(manager.has_installed_release(TAG))
        self.assertFalse(
            any(
                path.name.startswith(f".staging-{TAG}-")
                for path in release.directory.parent.iterdir()
            )
        )

    async def test_disabled_release_is_persistent_and_removal_keeps_user_data(self):
        manager = self._manager(FakeGitHub())
        release = await manager.enable(TAG)
        await manager.disable()

        self.assertTrue(manager.has_installed_release(TAG))
        self.assertTrue(release.directory.is_dir())
        restarted_manager = self._manager(FakeGitHub())
        self.assertTrue(restarted_manager.has_installed_release(TAG))
        model_file = self.data_root / "models" / "qwen3.5-2b" / "weights.bin"
        model_file.parent.mkdir(parents=True)
        model_file.write_bytes(b"local model")
        conversation_file = self.data_root / "conversations.sqlite3"
        conversation_file.write_bytes(b"saved conversations")

        async def run_in_worker(function, *args):
            return await asyncio.to_thread(function, *args)

        with patch("app.foreground.run_control", side_effect=run_in_worker):
            self.assertTrue(await restarted_manager.remove_installed_releases())

        self.assertFalse(restarted_manager.has_installed_release(TAG))
        self.assertFalse(release.directory.exists())
        self.assertEqual(model_file.read_bytes(), b"local model")
        self.assertEqual(conversation_file.read_bytes(), b"saved conversations")

    async def test_model_installer_wheels_download_only_after_explicit_request(self):
        github = FakeGitHub(with_installer_dependencies=True)
        manager = self._manager(github)
        release = await manager.enable(TAG)
        installer_assets = {
            dependency["asset"]
            for dependency in release.manifest.raw["installerDependencies"]
        }

        requested_assets = {
            asset
            for asset in installer_assets
            if any(asset in request for request in github.requests)
        }
        self.assertEqual(requested_assets, set())
        self.assertFalse((release.directory / "installer-dependencies").exists())

        async def run_in_worker(function, *args):
            return await asyncio.to_thread(function, *args)

        with patch("app.foreground.run_control", side_effect=run_in_worker):
            directories = await manager.install_model_dependencies()

        installer_root = release.directory / "installer-dependencies"
        self.assertEqual(len(directories), 4)
        self.assertEqual(
            {path.name for path in (installer_root / "wheelhouse").iterdir()},
            installer_assets,
        )
        self.assertTrue(all(path.is_dir() for path in directories))
        self.assertTrue((installer_root / "lumi-installer-wheels.json").is_file())
        requested_assets = {
            asset
            for asset in installer_assets
            if any(asset in request for request in github.requests)
        }
        self.assertEqual(requested_assets, installer_assets)

    async def test_llama_cpp_release_activates_and_installs_its_model_installer(self):
        github = FakeLlamaCppGitHub()
        manager = self._manager(github)

        release = await manager.enable(TAG)

        self.assertEqual(
            {
                dependency.distribution
                for dependency in release.manifest.runtime_dependencies
            },
            {"llama-cpp-python"},
        )
        self.assertTrue(manager.model_install_available)
        self.assertTrue(
            (
                release.directory
                / "dependencies"
                / "llama-cpp-python-0.3.35"
                / "site-packages"
                / "llama_cpp"
                / "__init__.py"
            ).is_file()
        )
        self.assertFalse((release.directory / "installer-dependencies").exists())

        async def run_in_worker(function, *args):
            return await asyncio.to_thread(function, *args)

        with patch("app.foreground.run_control", side_effect=run_in_worker):
            directories = await manager.install_model_dependencies()

        installer_root = release.directory / "installer-dependencies"
        self.assertEqual(len(directories), 1)
        self.assertTrue(
            (
                installer_root
                / "wheelhouse"
                / "huggingface_hub-1.10.0-py3-none-any.whl"
            ).is_file()
        )

    async def test_release_and_installer_wheels_survive_manager_restart(self):
        github = FakeLlamaCppGitHub()
        first_manager = self._manager(github)
        first_release = await first_manager.enable(TAG)

        async def run_in_worker(function, *args):
            return await asyncio.to_thread(function, *args)

        with patch("app.foreground.run_control", side_effect=run_in_worker):
            await first_manager.install_model_dependencies()
        request_count = len(github.requests)
        release_directory = first_release.directory
        installer_marker = (
            release_directory / "installer-dependencies" / "lumi-installer-wheels.json"
        )
        self.assertTrue(installer_marker.is_file())

        await first_manager.disable()
        second_manager = self._manager(github)
        second_release = await second_manager.enable(TAG)

        self.assertEqual(second_release.directory, release_directory)
        self.assertEqual(len(github.requests), request_count)
        self.assertTrue(installer_marker.is_file())
        with patch("app.foreground.run_control", side_effect=run_in_worker):
            directories = await second_manager.install_model_dependencies()
        self.assertEqual(len(directories), 1)
        self.assertEqual(len(github.requests), request_count)

        cached_hub = (
            release_directory
            / "installer-dependencies"
            / "dependencies"
            / "huggingface-hub-1.10.0"
            / "site-packages"
            / "huggingface_hub"
            / "__init__.py"
        )
        cached_hub.write_bytes(b"damaged cached package")
        extra_file = cached_hub.parent / "unexpected.py"
        extra_file.write_bytes(b"unexpected package file")
        with patch("app.foreground.run_control", side_effect=run_in_worker):
            await second_manager.install_model_dependencies()
        self.assertGreater(len(github.requests), request_count)
        self.assertEqual(cached_hub.read_bytes(), b"__version__ = 'test'\n")
        self.assertFalse(extra_file.exists())

    async def test_modified_cached_release_is_downloaded_and_repaired(self):
        github = FakeLlamaCppGitHub()
        first_manager = self._manager(github)
        first_release = await first_manager.enable(TAG)
        runtime_file = (
            first_release.directory / "package" / "lumi" / "runtime" / "__init__.py"
        )
        original_contents = runtime_file.read_bytes()
        await first_manager.disable()
        runtime_file.write_bytes(b"modified cached release\n")
        request_count = len(github.requests)

        second_manager = self._manager(github)
        repaired_release = await second_manager.enable(TAG)

        self.assertGreater(len(github.requests), request_count)
        self.assertEqual(repaired_release.directory, first_release.directory)
        self.assertEqual(runtime_file.read_bytes(), original_contents)

    async def test_llama_cpp_release_rejects_older_orchestrator_version(self):
        github = FakeLlamaCppGitHub()
        manager = LumiReleaseManager(
            self.data_root,
            "1.7.2",
            runtime_host=HOST,
            fetcher=github.fetch,
        )
        self.managers.append(manager)

        with self.assertRaisesRegex(
            LumiReleaseCompatibilityError,
            "incompatible with this Orchestrator version",
        ):
            await manager.enable(TAG)

    def test_model_installer_wheel_accepts_compressible_files_within_size_limits(self):
        wheel_bytes = _make_installer_wheel(
            "torch", "2.5.1", "torch", compressible_bytes=256 * 1024
        )
        dependency = RuntimeDependency(
            "torch",
            "2.5.1",
            "cp312",
            "cp312",
            "win_amd64",
            "torch-2.5.1-cp312-cp312-win_amd64.whl",
            _sha256(wheel_bytes),
        )
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as wheel:
            resource = wheel.getinfo("torch/compressible-resource.bin")
            self.assertGreater(resource.file_size / resource.compress_size, 200)

        expanded = _validate_wheel_archive(wheel_bytes, dependency, installer=True)
        self.assertGreaterEqual(expanded, resource.file_size)
        with self.assertRaisesRegex(LumiReleaseError, "compression ratio"):
            _validate_wheel_archive(wheel_bytes, dependency)

        with tempfile.TemporaryDirectory() as directory:
            _extract_wheel(
                wheel_bytes,
                Path(directory),
                dependency,
                installer=True,
            )
            installed_resource = Path(directory) / "torch" / "compressible-resource.bin"
            self.assertEqual(installed_resource.stat().st_size, resource.file_size)

    def test_model_installer_wheel_ignores_nested_vendored_metadata(self):
        wheel_bytes = _make_installer_wheel(
            "torch",
            "2.5.1",
            "torch",
            nested_metadata=True,
        )
        dependency = RuntimeDependency(
            "torch",
            "2.5.1",
            "cp312",
            "cp312",
            "win_amd64",
            "torch-2.5.1-cp312-cp312-win_amd64.whl",
            _sha256(wheel_bytes),
        )

        expanded = _validate_wheel_archive(wheel_bytes, dependency, installer=True)

        self.assertGreater(expanded, 0)

    async def test_model_installer_requires_a_complete_wheel_set_for_the_current_host(
        self,
    ):
        github = FakeGitHub(
            with_installer_dependencies=True,
            installer_torch_platform="linux_x86_64",
        )
        manager = self._manager(github)
        await manager.enable(TAG)

        self.assertFalse(manager.model_install_available)
        installer_assets = {
            dependency.asset
            for dependency in manager.active_release.manifest.installer_dependencies
        }
        with self.assertRaisesRegex(
            LumiReleaseCompatibilityError,
            "complete model installer wheel set",
        ):
            await manager.install_model_dependencies()
        requested_assets = {
            asset
            for asset in installer_assets
            if any(asset in request for request in github.requests)
        }
        self.assertEqual(requested_assets, set())

    async def test_disable_blocks_new_service_calls_before_awaiting_close(self):
        github = FakeGitHub()
        manager = self._manager(github)
        release = await manager.enable(TAG)
        close_started = asyncio.Event()
        finish_close = asyncio.Event()

        class SlowService:
            async def close(self):
                close_started.set()
                await finish_close.wait()

        release.service_factory.create_embedded_service = lambda *_args, **_kwargs: (
            SlowService()
        )
        service = release.create_embedded_service()
        disable_task = asyncio.create_task(manager.disable())
        await close_started.wait()

        self.assertIsNone(manager.active_release)
        with self.assertRaisesRegex(LumiReleaseError, "release is disabled"):
            release.create_embedded_service()

        finish_close.set()
        await disable_task
        self.assertIsNotNone(service)

    async def test_disable_waits_for_inflight_async_service_creation(self):
        github = FakeGitHub()
        manager = self._manager(github)
        release = await manager.enable(TAG)
        creation_started = asyncio.Event()
        finish_creation = asyncio.Event()
        created_services = []

        class PendingService:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

        async def create_service():
            creation_started.set()
            await finish_creation.wait()
            service = PendingService()
            created_services.append(service)
            return service

        release.service_factory.create_embedded_service = create_service
        creation_task = release.create_embedded_service()
        await creation_started.wait()
        disable_task = asyncio.create_task(manager.disable())
        while manager.active_release is not None:
            await asyncio.sleep(0)
        finish_creation.set()

        with self.assertRaisesRegex(LumiReleaseError, "release is disabled"):
            await creation_task
        await disable_task

        self.assertEqual(len(created_services), 1)
        self.assertTrue(created_services[0].closed)
        self.assertIsNone(manager.active_release)

    async def test_disable_closes_remaining_services_and_requires_restart_after_close_error(
        self,
    ):
        github = FakeGitHub()
        manager = self._manager(github)
        release = await manager.enable(TAG)
        manager._unloader = Mock(wraps=_unload_managed_lumi)

        class CloseService:
            def __init__(self, *, fail):
                self.fail = fail
                self.close_attempted = False

            def close(self):
                self.close_attempted = True
                if self.fail:
                    raise RuntimeError("close failed")

        closes_first = CloseService(fail=False)
        closes_fails = CloseService(fail=True)
        services = iter((closes_first, closes_fails))
        release.service_factory.create_embedded_service = lambda: next(services)
        release.create_embedded_service()
        release.create_embedded_service()

        with self.assertRaisesRegex(LumiReleaseError, "cleanup failed"):
            await manager.disable()

        self.assertTrue(closes_fails.close_attempted)
        self.assertTrue(closes_first.close_attempted)
        manager._unloader.assert_called_once()
        self.assertIsNone(manager.active_release)
        self.assertIsNone(manager._closing_release)
        self.assertTrue(manager.restart_required)
        self.assertEqual(
            manager.disable_error,
            "1 Lumi service close operation(s) failed",
        )
        self.assertNotIn("lumi", sys.modules)
        with self.assertRaisesRegex(LumiReleaseError, "restart Orchestrator"):
            await manager.enable(TAG)

    async def test_same_enabled_tag_is_idempotent_and_other_tag_requires_disable(self):
        github = FakeGitHub()
        manager = self._manager(github)

        first = await manager.enable(TAG)
        self.assertIs(await manager.enable(TAG), first)
        with self.assertRaises(LumiReleaseError):
            await manager.enable("v1.2.4")
        self.assertEqual(
            github.requests.count(f"{LUMI_GITHUB_API}/releases/tags/{TAG}"), 1
        )

    async def test_invalid_or_unpublished_tags_are_not_looked_up(self):
        github = FakeGitHub()
        manager = self._manager(github)

        for tag in ("latest", "v1.2.3-rc.1", "1.2.3", "v1.2"):
            with (
                self.subTest(tag=tag),
                self.assertRaises(LumiReleaseCompatibilityError),
            ):
                await manager.enable(tag)
        self.assertEqual(github.requests, [])

    async def test_missing_package_asset_is_rejected_without_activation(self):
        github = FakeGitHub(missing_assets={"lumi-runtime.zip"})
        manager = self._manager(github)

        with self.assertRaises(LumiReleaseUnavailable):
            await manager.enable(TAG)

        self.assertIsNone(manager.active_release)
        self.assertFalse((self.data_root / "lumi" / "releases").exists())
        self.assertEqual(len(github.requests), 1)

    async def test_missing_required_wheel_asset_is_rejected(self):
        github = FakeGitHub(missing_assets={WHEEL_NAME})
        manager = self._manager(github)

        with self.assertRaises(LumiReleaseUnavailable):
            await manager.enable(TAG)

        self.assertIsNone(manager.active_release)
        self.assertFalse((self.data_root / "lumi" / "releases").exists())

    async def test_aggregate_wheel_expansion_limit_is_enforced_before_install(self):
        github = FakeGitHub()
        manager = self._manager(github)

        with patch("app.lumi_release.MAX_TOTAL_WHEEL_EXPANDED_BYTES", 1):
            with self.assertRaisesRegex(LumiReleaseError, "total expanded size"):
                await manager.enable(TAG)

        self.assertIsNone(manager.active_release)
        self.assertFalse((self.data_root / "lumi" / "releases").exists())

    async def test_unsupported_python_or_platform_is_rejected_before_wheel_download(
        self,
    ):
        github = FakeGitHub()
        manager = self._manager(
            github,
            host=RuntimeHost("cp313", "cp313", ("win_amd64",)),
        )

        with self.assertRaises(LumiReleaseCompatibilityError):
            await manager.enable(TAG)

        self.assertEqual(len(github.requests), 2)
        self.assertIsNone(manager.active_release)
        self.assertFalse((self.data_root / "lumi" / "releases").exists())

    async def test_release_metadata_rejects_draft_and_prerelease(self):
        for github in (FakeGitHub(draft=True), FakeGitHub(prerelease=True)):
            manager = self._manager(github)
            with (
                self.subTest(metadata=github.metadata),
                self.assertRaises(LumiReleaseUnavailable),
            ):
                await manager.enable(TAG)
            self.assertIsNone(manager.active_release)
        self.assertFalse((self.data_root / "lumi" / "releases").exists())

    async def test_incompatible_orchestrator_version_is_rejected_before_wheel_download(
        self,
    ):
        github = FakeGitHub()
        manager = LumiReleaseManager(
            self.data_root,
            "2.1.0",
            runtime_host=HOST,
            fetcher=github.fetch,
        )
        self.managers.append(manager)

        with self.assertRaises(LumiReleaseCompatibilityError):
            await manager.enable(TAG)

        self.assertEqual(len(github.requests), 2)
        self.assertFalse((self.data_root / "lumi" / "releases").exists())

    async def test_missing_asset_sha256_metadata_fails_closed(self):
        github = FakeGitHub()
        github.metadata["assets"][0].pop("digest")
        github.metadata_bytes = json.dumps(github.metadata).encode("utf-8")
        manager = self._manager(github)

        with self.assertRaises(LumiReleaseError):
            await manager.enable(TAG)

        self.assertEqual(len(github.requests), 1)
        self.assertIsNone(manager.active_release)

    async def test_downloaded_asset_digest_must_match_official_metadata(self):
        github = FakeGitHub()
        github.payloads[WHEEL_NAME] = github.wheel_bytes + b"tampered"
        manager = self._manager(github)

        with self.assertRaises(LumiReleaseError):
            await manager.enable(TAG)

        self.assertIsNone(manager.active_release)
        self.assertFalse((self.data_root / "lumi" / "releases").exists())

    async def test_release_asset_origin_is_pinned_to_the_configured_github_repo(self):
        github = FakeGitHub()
        github.metadata["assets"][0]["browser_download_url"] = (
            "https://attacker.invalid/lumi-runtime.zip"
        )
        github.metadata_bytes = json.dumps(github.metadata).encode("utf-8")
        manager = self._manager(github)

        with self.assertRaises(LumiReleaseError):
            await manager.enable(TAG)

        self.assertEqual(len(github.requests), 1)
        self.assertFalse((self.data_root / "lumi" / "releases").exists())

    async def test_release_metadata_must_come_from_the_pinned_api_endpoint(self):
        github = FakeGitHub(
            metadata_final_url="https://attacker.invalid/releases/v1.2.3"
        )
        manager = self._manager(github)

        with self.assertRaises(LumiReleaseError):
            await manager.enable(TAG)

        self.assertEqual(len(github.requests), 1)
        self.assertFalse((self.data_root / "lumi" / "releases").exists())

    async def test_zip_traversal_is_rejected_before_any_extraction(self):
        github = FakeGitHub(extra_member=("../escape.py", b"bad"))
        manager = self._manager(github)

        with self.assertRaises(LumiReleaseError):
            await manager.enable(TAG)

        self.assertFalse((Path(self.temporary_directory.name) / "escape.py").exists())
        self.assertFalse((self.data_root / "lumi" / "releases").exists())

    async def test_zip_symlink_entries_are_rejected(self):
        symlink = zipfile.ZipInfo("lumi/linked.py")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        github = FakeGitHub(extra_member=(symlink, b"../outside.py"))
        manager = self._manager(github)

        with self.assertRaisesRegex(LumiReleaseError, "symbolic link"):
            await manager.enable(TAG)

        self.assertFalse((Path(self.temporary_directory.name) / "outside.py").exists())
        self.assertFalse((self.data_root / "lumi" / "releases").exists())

    async def test_package_file_sha256_is_verified_before_activation(self):
        github = FakeGitHub()
        contents = {}
        with zipfile.ZipFile(io.BytesIO(github.package_bytes)) as archive:
            for info in archive.infolist():
                contents[info.filename] = archive.read(info)
        original = contents["lumi/__init__.py"]
        contents["lumi/__init__.py"] = original.replace(b"runtime", b"Runtime")
        changed_archive = io.BytesIO()
        with zipfile.ZipFile(
            changed_archive, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, content in contents.items():
                archive.writestr(name, content)
        github.package_bytes = changed_archive.getvalue()
        github.payloads["lumi-runtime.zip"] = github.package_bytes
        package_asset = github.metadata["assets"][0]
        package_asset["size"] = len(github.package_bytes)
        package_asset["digest"] = f"sha256:{_sha256(github.package_bytes)}"
        github.metadata_bytes = json.dumps(github.metadata).encode("utf-8")
        manager = self._manager(github)

        with self.assertRaisesRegex(LumiReleaseError, "failed its SHA-256 check"):
            await manager.enable(TAG)

        self.assertIsNone(manager.active_release)
        self.assertEqual(list((self.data_root / "lumi" / "releases").iterdir()), [])

    async def test_import_failure_rolls_back_new_directory_and_staging(self):
        github = FakeGitHub()

        def failing_importer(package_directory, dependency_directories):
            raise ImportError("injected import failure")

        manager = self._manager(
            github,
            importer=failing_importer,
            unloader=lambda package, dependencies: None,
        )

        with self.assertRaisesRegex(LumiReleaseError, "activation was rolled back"):
            await manager.enable(TAG)

        releases_directory = self.data_root / "lumi" / "releases"
        self.assertTrue(releases_directory.is_dir())
        self.assertEqual(list(releases_directory.iterdir()), [])
        self.assertIsNone(manager.active_release)

    async def test_import_api_mismatch_rolls_back_and_calls_unloader(self):
        github = FakeGitHub()
        unloaded = []
        package_module = ModuleType("lumi")
        package_module.LUMI_PLUGIN_API_VERSION = 1
        runtime_module = ModuleType("lumi.runtime")
        runtime_module.LUMI_RUNTIME_API_VERSION = 2
        service_factory = ModuleType("lumi.service_factory")
        service_factory.create_embedded_service = lambda: object()

        class Runtime:
            def close(self):
                pass

        runtime_module.OrtGenAIChatRuntime = Runtime
        runtime_module.OrtGenAIConfig = type("OrtGenAIConfig", (), {})
        runtime_module.VerifiedModelArtifact = type("VerifiedModelArtifact", (), {})
        modules = ImportedLumiModules(package_module, runtime_module, service_factory)
        manager = self._manager(
            github,
            importer=lambda package, dependencies: modules,
            unloader=lambda package, dependencies: unloaded.append(package),
        )

        with self.assertRaises(LumiReleaseCompatibilityError):
            await manager.enable(TAG)

        self.assertEqual(len(unloaded), 1)
        self.assertEqual(list((self.data_root / "lumi" / "releases").iterdir()), [])
        self.assertIsNone(manager.active_release)

    async def test_failed_reactivation_restores_previously_installed_version_directory(
        self,
    ):
        github = FakeGitHub()
        should_fail = False

        def importer(package_directory, dependency_directories):
            if should_fail:
                raise ImportError("failed reactivation")
            return _import_managed_lumi(package_directory, dependency_directories)

        manager = self._manager(github, importer=importer)
        first = await manager.enable(TAG)
        installed_directory = first.directory
        await manager.disable()
        should_fail = True

        with self.assertRaises(LumiReleaseError):
            await manager.enable(TAG)

        self.assertTrue(installed_directory.is_dir())
        self.assertIsNone(manager.active_release)
        self.assertEqual(
            [path.name for path in (self.data_root / "lumi" / "releases").iterdir()],
            [installed_directory.name],
        )

    async def test_z_disable_reports_restart_when_native_extension_remains_loaded(self):
        github = FakeGitHub()
        manager = self._manager(github)
        release = await manager.enable(TAG)
        dependency_root = next(
            (release.directory / "dependencies").glob("*/site-packages")
        )
        native_module = ModuleType("test_managed_native_extension")
        suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
        native_module.__file__ = str(
            dependency_root / "onnxruntime_genai" / f"onnxruntime_genai{suffix}"
        )
        sys.modules[native_module.__name__] = native_module
        self.addCleanup(sys.modules.pop, native_module.__name__, None)

        await manager.disable()

        self.assertIsNone(manager.active_release)
        self.assertTrue(manager.restart_required)
        self.assertTrue(release.directory.is_dir())
        with self.assertRaisesRegex(LumiReleaseError, "restart Orchestrator"):
            await manager.enable(TAG)
        with self.assertRaisesRegex(LumiReleaseError, "restart Orchestrator"):
            await manager.remove_installed_releases()
        self.assertTrue(release.directory.is_dir())


if __name__ == "__main__":
    unittest.main()
