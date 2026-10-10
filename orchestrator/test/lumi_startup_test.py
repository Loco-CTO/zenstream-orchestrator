from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from app.lumi_host import LumiHost
from fastapi import FastAPI


class LumiStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_lifespan_waits_until_saved_lumi_is_restored(self):
        class DelayedReleaseManager:
            def __init__(self):
                self.activation_started = threading.Event()
                self.continue_activation = threading.Event()
                self.restart_required = False

            def has_installed_release(self, _tag: str) -> bool:
                return True

            async def enable(self, tag: str):
                self.activation_started.set()
                await asyncio.to_thread(self.continue_activation.wait)
                return SimpleNamespace(
                    tag=tag,
                    package_module=SimpleNamespace(
                        supported_models=lambda: (
                            SimpleNamespace(
                                model_id="qwen3.5:2b",
                                label="Qwen3.5 2B",
                                supports_thinking=True,
                            ),
                        )
                    ),
                    manifest=SimpleNamespace(installer_dependencies=()),
                )

            async def disable(self):
                return None

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
            service = object()

            async def rebuild_service():
                host._service = service
                host._runtime = object()

            host._rebuild_service = AsyncMock(side_effect=rebuild_service)

            import app.app as app_module
            from api.zenstream import lumi_routes

            test_app = FastAPI(lifespan=app_module.lifespan)

            @test_app.get("/lumi-probe")
            def lumi_probe():
                return {"available": lumi_routes._service() is service}

            lifespan_context = app_module.lifespan(test_app)
            startup = None
            response = None
            yielded_before_restore = None
            with ExitStack() as patches:
                patches.enter_context(
                    patch.dict(os.environ, {"SECRET_KEY": "test-secret"})
                )
                patches.enter_context(patch("app.app.load_config"))
                read_model = patches.enter_context(patch("app.app.CatalogReadModel"))
                read_model.return_value.bootstrap.return_value = None
                patches.enter_context(patch("app.app.lumi_host", host))
                patches.enter_context(
                    patch("api.zenstream.lumi_routes.lumi_host", host)
                )
                patches.enter_context(
                    patch(
                        "app.app.Config",
                        return_value=SimpleNamespace(database=object()),
                    )
                )
                patches.enter_context(
                    patch("app.app.run_control", new=AsyncMock(return_value={}))
                )
                patches.enter_context(patch("app.app.library_runtime.start"))
                patches.enter_context(patch("app.app.library_runtime.stop"))
                patches.enter_context(patch("app.app.job_scheduler.start"))
                patches.enter_context(patch("app.app.job_scheduler.stop"))
                patches.enter_context(patch("app.app.stop_artwork_variants"))
                patches.enter_context(patch("app.app.PlaybackManager.stop_all"))
                patches.enter_context(patch("app.app.asset_executor.shutdown"))
                patches.enter_context(
                    patch("app.app.wait_for_shutdown", new=AsyncMock())
                )
                patches.enter_context(patch("app.app.shutdown_foreground"))
                patches.enter_context(patch("app.app.hub.broadcast", new=AsyncMock()))
                patches.enter_context(patch("app.app.hub.shutdown", new=AsyncMock()))
                patches.enter_context(
                    patch("app.app._database_metrics", return_value={})
                )

                startup = asyncio.create_task(lifespan_context.__aenter__())
                for _ in range(500):
                    if manager.activation_started.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(manager.activation_started.is_set())
                yielded_before_restore = startup.done()

                manager.continue_activation.set()
                await asyncio.wait_for(startup, timeout=10)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=test_app),
                    base_url="http://test",
                ) as client:
                    response = await client.get("/lumi-probe")
                await lifespan_context.__aexit__(None, None, None)

            self.assertFalse(
                yielded_before_restore,
                "ASGI startup must not finish before saved Lumi activation completes",
            )
            self.assertIsNotNone(response)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"available": True})


if __name__ == "__main__":
    unittest.main()
