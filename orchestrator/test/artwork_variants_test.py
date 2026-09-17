import asyncio
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import AsyncMock, patch

from api.zenstream import library_routes
from app.artwork_variants import (
    ArtworkVariantCache,
    ArtworkVariantPrewarmer,
    ArtworkVariantSource,
    artwork_variant_status,
    selected_sources,
)
from app.database import DatabaseHandler


class PendingExecutor:
    def __init__(self):
        self.submissions: list[tuple[tuple[str, str], Future, object]] = []

    def try_submit_future(self, key, work):
        future = Future()
        self.submissions.append((key, future, work))
        return future


class ArtworkVariantCacheTest(unittest.TestCase):
    def test_cache_is_source_versioned_and_prunes_only_invalid_or_obsolete_files(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = ArtworkVariantCache(str(root / "orchestrator.db"))
            source_path = root / "poster.webp"
            source_path.write_bytes(b"source")
            current = ArtworkVariantSource(source_path, "revision-1")
            obsolete = ArtworkVariantSource(source_path, "revision-0")

            current_target = cache.path_for(current, 160)
            obsolete_target = cache.path_for(obsolete, 160)
            incomplete_target = cache.path_for(current, 320)
            assert current_target is not None
            assert obsolete_target is not None
            assert incomplete_target is not None
            current_target.parent.mkdir(parents=True, exist_ok=True)
            obsolete_target.parent.mkdir(parents=True, exist_ok=True)
            incomplete_target.parent.mkdir(parents=True, exist_ok=True)
            current_target.write_bytes(b"current")
            obsolete_target.write_bytes(b"obsolete")
            incomplete_target.write_bytes(b"")

            removed = cache.prune_stale([current])

            self.assertEqual(removed, 2)
            self.assertTrue(current_target.is_file())
            self.assertFalse(obsolete_target.exists())
            self.assertFalse(incomplete_target.exists())
            self.assertNotEqual(
                cache.path_for(current, 160), cache.path_for(obsolete, 160)
            )

    def test_selection_snapshot_reports_incomplete_when_the_canonical_table_is_missing(
        self,
    ):
        database = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            sources, complete = selected_sources(database)
        finally:
            database.close()

        self.assertEqual(sources, [])
        self.assertFalse(complete)

    def test_status_is_starting_before_the_first_sweep(self):
        cache = ArtworkVariantCache(":memory:")
        prewarmer = ArtworkVariantPrewarmer(cache)

        status = prewarmer.status()

        self.assertEqual(status["state"], "starting")
        self.assertEqual(status["expectedVariants"], 0)
        self.assertIsNone(status["lastSweepAt"])
        self.assertEqual(status["pendingConversions"], 0)

    def test_status_reports_expected_ready_and_remaining_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = ArtworkVariantCache(str(root / "orchestrator.db"))
            source = root / "poster.webp"
            source.write_bytes(b"source")
            current = ArtworkVariantSource(source, "revision-1")
            for width, value in ((160, b"small"), (320, b"large")):
                target = cache.path_for(current, width)
                assert target is not None
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(value)
            cache.diagnostics()
            prewarmer = ArtworkVariantPrewarmer(cache)
            prewarmer.mark_sweep_started()
            prewarmer.update_selection([current], True)
            prewarmer.complete_sweep()

            status = prewarmer.status()

        self.assertEqual(status["sourceCount"], 1)
        self.assertEqual(status["expectedVariants"], 2)
        self.assertEqual(status["readyVariants"], 2)
        self.assertEqual(status["remainingVariants"], 0)
        self.assertEqual(status["cacheFileCount"], 2)
        self.assertEqual(status["cacheBytes"], len(b"small") + len(b"large"))
        self.assertEqual(status["state"], "ready")
        self.assertIsNotNone(status["lastSuccessfulSweepAt"])

    def test_status_reports_queued_active_and_pending_conversions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = ArtworkVariantCache(str(root / "orchestrator.db"))
            sources = []
            for index in range(2):
                source = root / f"poster-{index}.webp"
                source.write_bytes(b"source")
                sources.append(ArtworkVariantSource(source, f"revision-{index}"))
            prewarmer = ArtworkVariantPrewarmer(cache)
            executor = PendingExecutor()
            prewarmer.mark_sweep_started()
            prewarmer.update_selection(sources, True)
            prewarmer.enqueue(sources, executor)
            prewarmer.complete_sweep()

            status = prewarmer.status()

        self.assertEqual(status["expectedVariants"], 4)
        self.assertEqual(status["readyVariants"], 0)
        self.assertEqual(status["remainingVariants"], 4)
        self.assertEqual(status["queuedConversions"], 2)
        self.assertEqual(status["activeConversions"], 2)
        self.assertEqual(status["pendingConversions"], 2)
        self.assertEqual(status["state"], "warming")

    def test_successful_and_failed_conversions_update_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = ArtworkVariantCache(str(root / "orchestrator.db"))
            source = root / "poster.webp"
            source.write_bytes(b"source")
            current = ArtworkVariantSource(source, "revision-1")
            prewarmer = ArtworkVariantPrewarmer(cache)
            executor = PendingExecutor()
            prewarmer.mark_sweep_started()
            prewarmer.update_selection([current], True)
            prewarmer.enqueue([current], executor)
            prewarmer.complete_sweep()

            executor.submissions[0][1].set_result(Path("variant-160.webp"))
            executor.submissions[1][1].set_exception(RuntimeError("encoder failed"))
            status = prewarmer.status()

        self.assertEqual(status["readyVariants"], 1)
        self.assertEqual(status["remainingVariants"], 1)
        self.assertEqual(status["activeConversions"], 0)
        self.assertEqual(status["pendingConversions"], 0)
        self.assertEqual(status["failedConversions"], 1)
        self.assertEqual(status["state"], "degraded")
        self.assertTrue(status["lastError"])

    def test_repeated_sweeps_preserve_existing_progress_and_source_versions_refresh_work(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = ArtworkVariantCache(str(root / "orchestrator.db"))
            source = root / "poster.webp"
            source.write_bytes(b"source")
            current = ArtworkVariantSource(source, "revision-1")
            prewarmer = ArtworkVariantPrewarmer(cache)
            executor = PendingExecutor()
            prewarmer.mark_sweep_started()
            prewarmer.update_selection([current], True)
            prewarmer.enqueue([current], executor)
            prewarmer.complete_sweep()
            for width in (160, 320):
                target = cache.path_for(current, width)
                assert target is not None
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"variant")
                executor.submissions[0 if width == 160 else 1][1].set_result(target)

            prewarmer.update_selection([current], True)
            prewarmer.enqueue([current], executor)
            self.assertEqual(prewarmer.status()["readyVariants"], 2)
            self.assertEqual(len(executor.submissions), 2)

            changed = ArtworkVariantSource(source, "revision-2")
            prewarmer.update_selection([changed], True)
            prewarmer.enqueue([changed], executor)
            status = prewarmer.status()

        self.assertEqual(status["expectedVariants"], 2)
        self.assertEqual(status["readyVariants"], 0)
        self.assertEqual(status["remainingVariants"], 2)
        self.assertEqual(len(executor.submissions), 4)

    def test_status_reads_do_not_scan_the_cache_or_enqueue_work(self):
        cache = ArtworkVariantCache(":memory:")
        prewarmer = ArtworkVariantPrewarmer(cache)
        prewarmer.mark_sweep_started()
        prewarmer.complete_sweep()
        with (
            patch.object(cache, "diagnostics", side_effect=AssertionError("scan")),
            patch.object(cache, "submit", side_effect=AssertionError("enqueue")),
        ):
            status = prewarmer.status()

        self.assertEqual(status["state"], "unavailable")
        self.assertEqual(status["pendingConversions"], 0)

    def test_status_route_authenticates_and_uses_the_control_lane(self):
        payload = {"state": "starting"}
        with (
            patch.object(library_routes, "require_admin") as authenticate,
            patch.object(
                library_routes, "run_control", new=AsyncMock(return_value=payload)
            ) as control,
        ):
            response = asyncio.run(library_routes.get_artwork_variant_status())

        self.assertEqual(response, payload)
        authenticate.assert_called_once()
        control.assert_awaited_once_with(
            library_routes.artwork_variant_status,
            library_routes.scheduler.store.db,
        )

    def test_status_function_only_reads_the_in_memory_prewarmer_snapshot(self):
        database = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            prewarmer = ArtworkVariantPrewarmer(ArtworkVariantCache(":memory:"))
            with (
                patch(
                    "app.artwork_variants.prewarmer_for",
                    return_value=prewarmer,
                ) as get_prewarmer,
                patch(
                    "app.artwork_variants.selected_sources",
                    side_effect=AssertionError("selection scan"),
                ),
            ):
                status = artwork_variant_status(database)
        finally:
            database.close()

        self.assertEqual(status["state"], "starting")
        get_prewarmer.assert_called_once_with(":memory:")


if __name__ == "__main__":
    unittest.main()
