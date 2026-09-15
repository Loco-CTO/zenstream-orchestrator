"""Run a repeatable music-scan and album-page benchmark.

The fixture is deliberately synthetic: Mutagen, provider resolution, and
artwork work are replaced with deterministic stubs while the scanner, file
inventory, SQLite writer, read model, and catalog page path remain real.

Example (from the repository root)::

    .venv\\Scripts\\python.exe scripts/music_scan_benchmark.py \\
        --albums 250 --tracks 2 --repetitions 3 --page-size 18

The release-sized fixtures are selected with ``--scale 1x`` or ``--scale 2x``.
They keep the same deterministic transports while exercising the real scanner,
SQLite persistence, projections, artwork queue, catalog page, and search paths.

Run the same command on two revisions and compare the median values in the
JSON output. Real-provider measurements should be collected separately.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "orchestrator"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.catalog import Catalog, _CatalogDatabase  # noqa: E402
from app.database import DatabaseHandler  # noqa: E402
from app.library import AudioTags, LibraryScanner, LibraryStore  # noqa: E402
from app.models.metadata import MetadataLanguageSettings  # noqa: E402


CAPACITY_TARGETS = {
    "1x": {
        "tracks": 21_658,
        "albums": 4_000,
        "artists": 1_600,
        "episodes": 48_640,
        "series": 2_106,
        "seasons": 3_118,
        "movies": 964,
        "collections": 146,
    },
    "2x": {
        "tracks": 43_316,
        "albums": 8_000,
        "artists": 3_200,
        "episodes": 97_280,
        "series": 4_212,
        "seasons": 6_236,
        "movies": 1_928,
        "collections": 292,
    },
}


@dataclass
class PhaseResult:
    name: str
    elapsed_ms: int
    scan_stats: dict
    request_elapsed_ms: int | None = None
    request_items: int | None = None
    request_hydrated: int | None = None
    request_error: str | None = None
    traffic: dict | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "elapsedMs": self.elapsed_ms,
            "scanStats": self.scan_stats,
            "requestDuringScanMs": self.request_elapsed_ms,
            "requestItems": self.request_items,
            "requestHydrated": self.request_hydrated,
            "requestError": self.request_error,
            "traffic": self.traffic,
        }


class MusicScanBenchmark:
    def __init__(
        self,
        albums: int,
        tracks: int,
        page_size: int,
        *,
        artist_count: int = 1,
        scale: str = "custom",
    ):
        self.albums = max(1, int(albums))
        self.tracks = max(1, int(tracks))
        self.page_size = max(1, int(page_size))
        self.artist_count = max(1, int(artist_count))
        self.scale = scale
        self._temporary = tempfile.TemporaryDirectory(prefix="zenstream-music-bench-")
        self.directory = Path(self._temporary.name)
        self.root = self.directory / "music"
        self.root.mkdir()
        self.database_path = self.directory / "orchestrator.db"
        self._upgrade_database()
        self.db = DatabaseHandler("sqlite", {}, str(self.database_path))
        self.store = LibraryStore.__new__(LibraryStore)
        self.store.db = self.db
        self.store._progress = {}
        self.library = self.store.create("Benchmark Music", "music", str(self.root))
        self.library_id = self.library["id"]
        self.scanner = LibraryScanner(self.store)
        self.catalog = Catalog()
        self.catalog.db = _CatalogDatabase(self.db)
        self._create_corpus()
        self._benchmark_phase = "initialAdmission"
        self.read_workload: dict = {}

    def _upgrade_database(self) -> None:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", (PROJECT_ROOT / "migrations").as_posix())
        config.set_main_option(
            "sqlalchemy.url", f"sqlite:///{self.database_path.as_posix()}"
        )
        command.upgrade(config, "head")

    def _create_corpus(self) -> None:
        for album_index in range(self.albums):
            artist_index = album_index % self.artist_count
            artist = self.root / f"Benchmark Artist {artist_index:04d}"
            album = artist / f"Album {album_index:04d}"
            album.mkdir(parents=True)
            for track_index in range(1, self.tracks + 1):
                path = album / f"{track_index:02d}. Track {track_index:02d}.flac"
                path.write_bytes(
                    f"fixed music fixture {album_index}:{track_index}\n".encode()
                )

    def _parse_tags(self, path: Path, gate: dict | None = None) -> AudioTags:
        album_index = int(path.parent.name.split()[-1])
        track_index = int(path.stem.split(".", 1)[0])
        artist_name = path.parent.parent.name
        tags = {
            "TITLE": f"Track {track_index:02d}",
            "ALBUM": f"Album {album_index:04d}",
            "ALBUMARTIST": artist_name,
            "ARTIST": artist_name,
            "TRACKNUMBER": str(track_index),
            "DATE": str(2000 + album_index % 25),
        }
        probe = {
            "format": {
                "format_name": "flac",
                "duration": str(180 + track_index),
                "bit_rate": "900000",
            },
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "flac",
                    "tags": {"language": "eng"},
                }
            ],
        }
        return AudioTags(tags, probe)

    def _metadata_stub(self, *_args, **_kwargs) -> None:
        # A deterministic provider stub: count one settled enrichment attempt
        # per changed album without making network requests.
        gate = getattr(self, "_benchmark_gate", None)
        if self._benchmark_phase == "oneChangedAlbum" and gate is not None:
            with gate["lock"]:
                should_wait = not gate["armed"]
                if should_wait:
                    gate["armed"] = True
            if should_wait:
                gate["started"].set()
                # Keep the scan alive long enough for the catalog request to
                # overlap it without making the benchmark depend on a second
                # thread reaching a blocking point.
                time.sleep(0.1)
        self.scanner._increment_music_scan_stat("provider_requests")

    def _metadata_needed_stub(self, _artist_id: str, release_id: str, _tracks) -> bool:
        if self._benchmark_phase == "initialAdmission":
            return True
        if self._benchmark_phase == "unchangedRescan":
            return False
        rows = self.db.execute(
            "SELECT relative_path FROM library_entities WHERE id=?", (release_id,)
        )
        return bool(rows and "Album 0000" in str(rows[0][0] or ""))

    def _scan_patches(self, gate: dict | None):
        stack = ExitStack()
        stack.enter_context(
            patch("app.library.parse_audio_tags", side_effect=lambda path: self._parse_tags(path, gate))
        )
        # An instance-level resolver makes the scanner use the synchronous
        # metadata path; the stub then keeps provider behavior repeatable.
        stack.enter_context(
            patch.object(self.scanner, "_resolve_music_group", lambda *_args, **_kwargs: None)
        )
        stack.enter_context(
            patch.object(
                self.scanner,
                "_run_music_group_metadata",
                side_effect=self._metadata_stub,
            )
        )
        stack.enter_context(
            patch.object(
                self.scanner,
                "_music_group_needs_metadata",
                side_effect=self._metadata_needed_stub,
            )
        )
        stack.enter_context(patch.object(self.scanner, "_fetch_seen_locales"))
        stack.enter_context(patch.object(self.scanner, "_refresh_calendar_links"))
        stack.enter_context(patch.object(self.scanner, "_start_heartbeat"))
        stack.enter_context(patch.object(self.scanner, "_stop_heartbeat"))
        stack.enter_context(
            patch.object(MetadataLanguageSettings, "get", return_value=["en", "ja", "zh-TW"])
        )
        stack.enter_context(
            patch.object(MetadataLanguageSettings, "prefer_no_language_for_backdrop", return_value=False)
        )
        stack.enter_context(patch.object(self.db, "schedule_maintenance", return_value=False))
        stack.enter_context(
            patch("app.metadata_services.repair_music_track_contexts")
        )
        artwork = MagicMock()
        artwork.path.return_value = None
        stack.enter_context(patch("app.library.LocalArtworkCache", return_value=artwork))
        trickplay = MagicMock()
        trickplay.queue_pending.return_value = False
        stack.enter_context(
            patch("app.trickplay.TrickplayStore", return_value=trickplay)
        )
        intro_outro = MagicMock()
        intro_outro.settings.return_value = {"scanOnAdded": False}
        intro_outro.queue_pending.return_value = False
        stack.enter_context(
            patch("app.intro_outro.IntroOutroStore", return_value=intro_outro)
        )
        stack.enter_context(patch("app.notifications.NotificationService"))
        return stack

    def _read_scan_stats(self, job_id: str) -> dict:
        rows = self.db.execute(
            "SELECT scan_stats FROM library_jobs WHERE id=?", (job_id,)
        )
        if not rows or not rows[0][0]:
            return {}
        try:
            value = json.loads(rows[0][0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _request_page(
        self,
        *,
        page: int = 1,
        sort_by: str = "title",
        sort_order: str = "ascending",
        catalog: Catalog | None = None,
    ) -> tuple[int, int, int, str | None]:
        active_catalog = catalog or self.catalog
        hydrated = []

        def hydrate(_user_id, row, _language, _dates=None, children=None):
            hydrated.append(row[0])
            return {"id": row[0], "childIds": list(children or [])}

        with patch.object(
            active_catalog, "allowed_libraries", return_value={self.library_id}
        ), patch.object(
            active_catalog,
            "require_library",
            return_value={"type": "music"},
        ), patch.object(
            active_catalog, "_configured_languages", return_value=["en"]
        ), patch.object(
            active_catalog, "_music_album_value", side_effect=hydrate
        ):
            started = time.monotonic()
            try:
                result = active_catalog.music_albums(
                    "benchmark-user",
                    "en",
                    self.library_id,
                    page=page,
                    page_size=self.page_size,
                    sort_by=sort_by,
                    sort_order=sort_order,
                )
            except Exception as error:  # pragma: no cover - diagnostic output
                return (
                    int(round((time.monotonic() - started) * 1000)),
                    0,
                    0,
                    f"{type(error).__name__}: {error}",
                )
        return (
            int(round((time.monotonic() - started) * 1000)),
            len(result.get("items", [])),
            len(hydrated),
            None,
        )

    def _request_search(
        self, query: str, *, catalog: Catalog | None = None
    ) -> tuple[int, int, str | None]:
        active_catalog = catalog or self.catalog

        def hydrate(_user_id, rows, _language, _dates=None):
            return [{"id": row[0]} for row in rows]

        with (
            patch.object(
                active_catalog,
                "allowed_libraries",
                return_value={self.library_id},
            ),
            patch.object(active_catalog, "_hydrate_rows", side_effect=hydrate),
            patch.object(MetadataLanguageSettings, "get", return_value=["en", "ja", "zh-TW"]),
        ):
            started = time.monotonic()
            try:
                result = active_catalog.search(
                    "benchmark-user",
                    query,
                    "en",
                    page=1,
                    page_size=self.page_size,
                )
            except Exception as error:  # pragma: no cover - diagnostic output
                return (
                    int(round((time.monotonic() - started) * 1000)),
                    0,
                    f"{type(error).__name__}: {error}",
                )
        return (
            int(round((time.monotonic() - started) * 1000)),
            len(result.get("items", [])),
            None,
        )

    def _measure_read_workload(self) -> dict:
        count_rows = self.db.execute(
            "SELECT COUNT(*) FROM catalog_music_album_page WHERE locale='en' AND library_id=?",
            (self.library_id,),
        )
        total = int(count_rows[0][0] or 0) if count_rows else 0
        last_page = max(1, (total + self.page_size - 1) // self.page_size)
        middle_page = max(1, (last_page + 1) // 2)
        catalog_cases = (
            ("first", 1, "title", "ascending"),
            ("middle", middle_page, "title", "ascending"),
            ("final", last_page, "release", "descending"),
            ("deep", last_page + 100, "added", "descending"),
        )
        catalog_results = {}
        for name, page, sort_by, sort_order in catalog_cases:
            elapsed, items, hydrated, error = self._request_page(
                page=page,
                sort_by=sort_by,
                sort_order=sort_order,
            )
            catalog_results[name] = {
                "elapsedMs": elapsed,
                "items": items,
                "hydrated": hydrated,
                "error": error,
            }
        search_results = {}
        for name, query in (
            ("common", "Album"),
            ("broad", "Benchmark"),
            ("short", "a"),
            ("multilingual", "アルバム"),
            ("noResult", "no-such-benchmark-result"),
        ):
            elapsed, items, error = self._request_search(query)
            search_results[name] = {
                "elapsedMs": elapsed,
                "items": items,
                "error": error,
            }
        return {
            "catalog": {"total": total, "pages": catalog_results},
            "search": search_results,
        }

    def _run_scan(
        self,
        name: str,
        *,
        gate: dict | None = None,
        request_during_scan: bool = False,
        traffic_users: int = 0,
    ) -> PhaseResult:
        self._benchmark_phase = name
        self._benchmark_gate = gate
        job_id = self.store.create_job(self.library_id, "scan")["id"]
        errors: list[BaseException] = []
        started = time.monotonic()

        def scan_target() -> None:
            try:
                with self._scan_patches(gate):
                    self.scanner.scan(self.library_id, job_id, lambda: False)
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)

        if not request_during_scan:
            scan_target()
            if errors:
                raise errors[0]
            return PhaseResult(name, int(round((time.monotonic() - started) * 1000)), self._read_scan_stats(job_id))

        thread = threading.Thread(target=scan_target, name="music-benchmark-scan")
        thread.start()
        if gate is not None:
            gate["started"].wait(2)
        traffic_results: list[dict] = []
        traffic_threads: list[threading.Thread] = []

        def traffic_worker(user_index: int) -> None:
            traffic_catalog = Catalog()
            traffic_catalog.db = _CatalogDatabase(self.db)
            page_result = self._request_page(
                page=(user_index % 3) + 1,
                sort_by="title" if user_index % 2 else "release",
                sort_order="ascending" if user_index % 2 else "descending",
                catalog=traffic_catalog,
            )
            search_result = self._request_search(
                "Benchmark" if user_index % 2 else "Album",
                catalog=traffic_catalog,
            )
            traffic_results.append(
                {
                    "user": user_index,
                    "catalogMs": page_result[0],
                    "catalogItems": page_result[1],
                    "searchMs": search_result[0],
                    "searchItems": search_result[1],
                    "error": page_result[3] or search_result[2],
                }
            )

        if traffic_users > 0:
            traffic_threads = [
                threading.Thread(
                    target=traffic_worker,
                    args=(index,),
                    name=f"music-benchmark-traffic-{index}",
                )
                for index in range(traffic_users)
            ]
            for traffic_thread in traffic_threads:
                traffic_thread.start()
            for traffic_thread in traffic_threads:
                traffic_thread.join(120)
            request_result = (
                traffic_results[0]["catalogMs"],
                traffic_results[0]["catalogItems"],
                0,
                traffic_results[0]["error"],
            )
        else:
            request_result = self._request_page()
        if gate is not None:
            gate["release"].set()
        thread.join(120)
        if thread.is_alive():
            raise TimeoutError("benchmark scan did not finish")
        if errors:
            raise errors[0]
        request_timing, request_items, hydrated, request_error = request_result
        traffic = None
        if traffic_results:
            latencies = [
                value
                for result in traffic_results
                for value in (result["catalogMs"], result["searchMs"])
            ]
            traffic = {
                "users": len(traffic_results),
                "requests": len(latencies),
                "p95Ms": _percentile(latencies, 0.95),
                "p99Ms": _percentile(latencies, 0.99),
                "results": sorted(traffic_results, key=lambda value: value["user"]),
            }
        return PhaseResult(
            name,
            int(round((time.monotonic() - started) * 1000)),
            self._read_scan_stats(job_id),
            request_elapsed_ms=request_timing,
            request_items=request_items,
            request_hydrated=hydrated,
            request_error=request_error,
            traffic=traffic,
        )

    def run(self) -> list[PhaseResult]:
        initial = self._run_scan("initialAdmission")
        # A full scan normally maintains this projection through publication.
        # Rebuild it cache-only if a minimal fixture or an older schema did not
        # create the page status row; this is outside all measured phases.
        from app.catalog_read_model import CatalogReadModel

        if not self.catalog._music_album_page_ready({self.library_id}, "en"):
            CatalogReadModel(self.db).rebuild_music_album_pages([self.library_id])

        self.read_workload = self._measure_read_workload()

        unchanged = self._run_scan("unchangedRescan")
        changed_track = next(self.root.rglob("*.flac"))
        changed_track.write_bytes(changed_track.read_bytes() + b" changed")
        os.utime(changed_track, None)
        gate = {
            "enabled": True,
            "armed": False,
            "lock": threading.Lock(),
            "started": threading.Event(),
            "release": threading.Event(),
        }
        changed = self._run_scan(
            "oneChangedAlbum",
            gate=gate,
            request_during_scan=True,
            traffic_users=4,
        )
        return [initial, unchanged, changed]

    def close(self) -> None:
        self.db.close()
        self._temporary.cleanup()


def _median(values: list[int | float]) -> int | float:
    return round(statistics.median(values), 2) if values else 0


def _percentile(values: list[int | float], quantile: float) -> int | float:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * quantile)))
    return round(ordered[index], 2)


def _resolve_fixture(scale: str, albums: int | None, tracks: int | None):
    if scale == "custom":
        return {
            "albums": max(1, albums or 250),
            "tracks": max(1, tracks or 2),
            "artists": 1,
            "targets": None,
        }
    target = CAPACITY_TARGETS[scale]
    resolved_albums = max(target["albums"], albums or 0)
    resolved_tracks = max(
        1,
        tracks or 0,
        (target["tracks"] + resolved_albums - 1) // resolved_albums,
    )
    return {
        "albums": resolved_albums,
        "tracks": resolved_tracks,
        "artists": target["artists"],
        "targets": target,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scale",
        choices=("custom", "1x", "2x"),
        default="custom",
        help="use the release capacity fixture (1x) or double it (2x)",
    )
    parser.add_argument("--albums", type=int)
    parser.add_argument("--tracks", type=int)
    parser.add_argument("--page-size", type=int, default=18)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved fixture without creating files or a database",
    )
    parser.add_argument(
        "--assert-targets",
        action="store_true",
        help="fail when scan duration exceeds the published capacity limits",
    )
    args = parser.parse_args()

    fixture = _resolve_fixture(args.scale, args.albums, args.tracks)
    fixture_description = {
        "scale": args.scale,
        "albums": fixture["albums"],
        "tracksPerAlbum": fixture["tracks"],
        "tracks": fixture["albums"] * fixture["tracks"],
        "artists": fixture["artists"],
        "targets": fixture["targets"],
        "locales": ["en", "ja", "zh-TW"],
        "pageSize": args.page_size,
        "providerMode": "deterministic-stub",
        "database": "file-backed-sqlite",
    }
    if args.dry_run:
        print(
            json.dumps(
                {"fixture": fixture_description},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    runs = []
    read_workloads = []
    for _ in range(max(1, args.repetitions)):
        benchmark = MusicScanBenchmark(
            fixture["albums"],
            fixture["tracks"],
            args.page_size,
            artist_count=fixture["artists"],
            scale=args.scale,
        )
        try:
            runs.append([result.as_dict() for result in benchmark.run()])
            read_workloads.append(benchmark.read_workload)
        finally:
            benchmark.close()

    phases = {phase["name"]: phase for phase in runs[0]}
    median = {}
    for name in phases:
        phase_values = [run[next(i for i, item in enumerate(run) if item["name"] == name)] for run in runs]
        median[name] = {
            "elapsedMs": _median([item["elapsedMs"] for item in phase_values]),
            "scanElapsedMs": _median(
                [item["scanStats"].get("elapsedMs", 0) for item in phase_values]
            ),
            "writerWaitMs": _median(
                [item["scanStats"].get("writerWaitMs", 0) for item in phase_values]
            ),
            "writerHoldMs": _median(
                [item["scanStats"].get("writerHoldMs", 0) for item in phase_values]
            ),
            "providerRequests": _median(
                [item["scanStats"].get("providerRequests", 0) for item in phase_values]
            ),
            "publications": _median(
                [item["scanStats"].get("publications", 0) for item in phase_values]
            ),
            "requestDuringScanMs": _median(
                [item["requestDuringScanMs"] or 0 for item in phase_values]
            ),
            "requestHydrated": _median(
                [item["requestHydrated"] or 0 for item in phase_values]
            ),
            "trafficP95Ms": _median(
                [
                    (item.get("traffic") or {}).get("p95Ms", 0)
                    for item in phase_values
                ]
            ),
            "trafficP99Ms": _median(
                [
                    (item.get("traffic") or {}).get("p99Ms", 0)
                    for item in phase_values
                ]
            ),
        }

    print(
        json.dumps(
            {
                "fixture": {
                    **fixture_description,
                },
                "runs": runs,
                "readWorkloads": read_workloads,
                "median": median,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.assert_targets:
        limits_ms = {
            "initialAdmission": 15 * 60 * 1000,
            "unchangedRescan": 2 * 60 * 1000,
            "oneChangedAlbum": 2 * 60 * 1000,
        }
        failures = [
            f"{name}={values['elapsedMs']}ms>{limits_ms[name]}ms"
            for name, values in median.items()
            if values["elapsedMs"] > limits_ms[name]
        ]
        if failures:
            print(
                "capacity targets failed: " + ", ".join(failures),
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
