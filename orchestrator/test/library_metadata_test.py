import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from api.zenstream import library_routes
from app.database import DatabaseHandler
from app.library import (
    EPISODE_RE,
    QUICK_FINGERPRINT_SAMPLE_SIZE,
    FairMetadataExecutor,
    LibraryRuntime,
    LibraryScanner,
    LibraryStore,
    _quick_fingerprint,
    _SidecarStatWorker,
    guess_media,
    normalized_path,
    provider_ids,
    sidecar_display_title,
)
from app.library_cleanup import cleanup_entities, cleanup_library, cleanup_orphans
from app.metadata_services import MetadataSearchProjection
from app.models.metadata import (
    IMAGE_LANGUAGE_SCHEMA,
    MetadataCache,
    MetadataLanguageSettings,
)
from app.providers import (
    BANNER,
    PRIMARY,
    MetadataService,
    ProviderError,
    ProviderLanguageCatalog,
    ProviderNotFoundError,
    TMDBClient,
    TVDBClient,
    _select_match,
    _tvdb_children,
    _tvdb_images,
    choose_image,
)


class _JsonRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class LibraryMetadataTest(unittest.TestCase):
    def test_fair_metadata_executor_bounds_head_of_line_work_per_library(self):
        executor = FairMetadataExecutor(max_workers=1)
        started = threading.Event()
        release = threading.Event()
        order = []

        def first():
            order.append("a1")
            started.set()
            release.wait(2)

        first_future = executor.submit("library-a", first)
        self.assertTrue(started.wait(1))
        futures = [
            executor.submit("library-a", lambda: order.append("a2")),
            executor.submit("library-a", lambda: order.append("a3")),
            executor.submit("library-b", lambda: order.append("b1")),
        ]
        release.set()
        first_future.result(2)
        for future in futures:
            future.result(2)

        self.assertLess(order.index("b1"), order.index("a3"))

    @staticmethod
    def test_movie_resolution_publishes_root_after_metadata():
        scanner = LibraryScanner.__new__(LibraryScanner)
        scanner._resolve_movie_row = MagicMock()
        scanner._publish_root = MagicMock()

        scanner._resolve_movie_and_publish(
            "library-1",
            ("movie-1", "movie", "Movie", None, None),
            "job-1",
            lambda: False,
            1,
            1,
        )

        scanner._resolve_movie_row.assert_called_once()
        scanner._publish_root.assert_called_once_with("movie-1")

    @staticmethod
    def test_scanner_locale_ingest_materializes_assets_inline():
        service = MagicMock()
        ingest = MagicMock()
        ingest.locales.return_value = ["en", "ja"]
        with patch(
            "app.metadata_services.MetadataIngestService", return_value=ingest
        ) as ingest_type:
            LibraryScanner._fetch_configured_locales(
                service, "tmdb", "movie", "123", required=True
            )

        ingest_type.assert_called_once_with(service, background_assets=False)
        ingest.ingest_locales.assert_called_once_with(
            "tmdb", "movie", "123", ["en", "ja"], force=False
        )

    def test_explicit_movie_identity_is_materialized_before_scan_advances(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = root / "A [tmdbid-1]"
                second = root / "B [tmdbid-2]"
                first.mkdir()
                second.mkdir()
                (first / "A.mkv").touch()
                (second / "B.mkv").touch()
                self._prepare_incremental_scan(scanner)
                order = []

                def resolve_and_publish(_library, row, *_args):
                    order.append(row[2])

                with (
                    patch.object(
                        scanner,
                        "_resolve_movie_and_publish",
                        side_effect=resolve_and_publish,
                    ) as resolve,
                    patch.object(scanner, "_publish_root") as publish,
                ):
                    scanner._scan_movies("library-1", root, "job-1", lambda: False)

                self.assertEqual(order, ["A [tmdbid-1]", "B [tmdbid-2]"])
                self.assertEqual(resolve.call_count, 2)
                publish.assert_not_called()
        finally:
            db.close()

    def test_child_metadata_failure_does_not_skip_later_siblings(self):
        db, scanner = self._scanner_db()
        try:
            for entity_id, episode_number in (("episode-1", 1), ("episode-2", 2)):
                db.execute(
                    "INSERT INTO library_entities(id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,created_at,updated_at,match_status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        entity_id,
                        "library-1",
                        "season-1",
                        "episode",
                        f"Series/Season 1/Episode {episode_number}.mkv",
                        1,
                        episode_number,
                        "now",
                        "now",
                        "matched",
                    ),
                )
                db.execute(
                    "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
                    (entity_id, "tvdb", "episode", str(100 + episode_number), 1),
                )
            scanner._scan_seen_ids = {"episode-1", "episode-2"}
            scanner._scan_created_ids = ["episode-1", "episode-2"]
            scanner._scan_provider_identity_changed = set()
            scanner._scan_delta = {"content_changed": set()}
            ingest = MagicMock()
            ingest.locales.return_value = ["en"]
            ingest.ingest_locales.side_effect = [
                OSError("disconnect"),
                {"en": {"title": "Episode 2"}},
            ]

            with (
                patch.object(
                    scanner,
                    "_metadata_candidates",
                    return_value={"episode-1", "episode-2"},
                ),
                patch(
                    "app.metadata_services.MetadataIngestService", return_value=ingest
                ),
                patch.object(scanner, "_persist_normalized_ids"),
                patch.object(scanner, "_persist_child_ids"),
            ):
                scanner._seed_all_children(
                    "library-1",
                    MagicMock(),
                    "job-1",
                    lambda: False,
                    parent_id="season-1",
                )

            states = dict(
                db.execute(
                    "SELECT id,match_status FROM library_entities WHERE id IN ('episode-1','episode-2')"
                )
            )
            self.assertEqual(states["episode-1"], "failed")
            self.assertEqual(states["episode-2"], "matched")
            self.assertEqual(ingest.ingest_locales.call_count, 2)
        finally:
            db.close()

    def test_scan_failure_persists_durable_locale_repairs(self):
        db, scanner = self._scanner_db()
        try:
            db.execute(
                "CREATE TABLE enrichment_queue(id TEXT PRIMARY KEY,entity_id TEXT,library_id TEXT,kind TEXT,locale TEXT,priority INTEGER,state TEXT,attempts INTEGER,next_attempt_at TEXT,lease_owner TEXT,lease_expires_at TEXT,source_job_id TEXT,error TEXT,created_at TEXT,updated_at TEXT,UNIQUE(entity_id,kind,locale))"
            )
            db.execute(
                "INSERT INTO library_entities(id,library_id,parent_id,entity_type,relative_path,created_at,updated_at,match_status) VALUES('movie-1','library-1',NULL,'movie','Movie','now','now','failed')"
            )

            scanner._queue_metadata_repair(
                "movie-1",
                "library-1",
                "job-1",
                "transport failure",
                ["en", "ja"],
            )
            scanner._queue_metadata_repair(
                "movie-1",
                "library-1",
                "job-2",
                "retry failure",
                ["en", "ja"],
            )

            rows = db.execute(
                "SELECT locale,state,attempts,source_job_id,error FROM enrichment_queue ORDER BY locale"
            )
            self.assertEqual(
                rows,
                [
                    ("en", "retry", 2, "job-2", "retry failure"),
                    ("ja", "retry", 2, "job-2", "retry failure"),
                ],
            )
        finally:
            db.close()

    def test_metadata_languages_normalize_without_forcing_english(self):
        self.assertEqual(
            MetadataLanguageSettings.normalize(["ja", "zh_tw", "en", "ja"]),
            ["ja", "zh-TW", "en"],
        )
        self.assertEqual(
            MetadataLanguageSettings.normalize(["ja", "zh_tw"]), ["ja", "zh-TW"]
        )
        with self.assertRaises(ValueError):
            MetadataLanguageSettings.normalize([])

    def test_normalized_path_accepts_literal_windows_yen_separators(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Media Library" / "Movie"
            target.mkdir(parents=True)
            literal_yen_path = str(target).replace("\\", "\u00a5")

            self.assertEqual(
                normalized_path(literal_yen_path),
                os.path.normcase(os.path.normpath(str(target))),
            )

    @staticmethod
    def _scanner_db():
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute(
            "CREATE TABLE library_entities (id TEXT PRIMARY KEY, library_id TEXT NOT NULL, parent_id TEXT, entity_type TEXT NOT NULL, relative_path TEXT, season_number INTEGER, episode_number INTEGER, episode_end_number INTEGER, disc_number INTEGER, track_number INTEGER, created_at TEXT, updated_at TEXT, match_status TEXT DEFAULT 'unresolved', match_confidence REAL, match_method TEXT, UNIQUE(library_id, entity_type, relative_path))"
        )
        db.execute(
            "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER, PRIMARY KEY(entity_id, provider, identifier_type))"
        )
        db.execute(
            "CREATE TABLE media_files (id TEXT PRIMARY KEY, entity_id TEXT, relative_path TEXT, role TEXT, language TEXT, flags TEXT, size INTEGER, modified_ns INTEGER, quick_fingerprint TEXT, UNIQUE(entity_id, relative_path, role))"
        )
        db.execute(
            "CREATE TABLE library_jobs (id TEXT PRIMARY KEY, library_id TEXT, kind TEXT, state TEXT, progress_current INTEGER DEFAULT 0, progress_total INTEGER DEFAULT 0, message TEXT)"
        )
        db.execute(
            "CREATE TABLE collection_members (collection_entity_id TEXT, source_entity_id TEXT, position INTEGER)"
        )
        db.execute(
            "CREATE TABLE metadata_hydration_requests (entity_id TEXT, locale TEXT)"
        )
        db.execute(
            "CREATE TABLE metadata_cache (provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, payload TEXT, fetched_at TEXT, expires_at TEXT)"
        )
        db.execute(
            "CREATE TABLE metadata_images (provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, image_type TEXT, image_url TEXT, local_path TEXT)"
        )
        db.execute(
            "INSERT INTO library_jobs(id, library_id, kind, state) VALUES('job-1','library-1','scan','queued')"
        )
        store = LibraryStore.__new__(LibraryStore)
        store.db = db
        return db, LibraryScanner(store)

    @staticmethod
    def _prepare_incremental_scan(scanner):
        scanner._scan_seen_ids = set()
        scanner._scan_created_ids = []
        scanner._scan_delta = {
            "added": set(),
            "changed": set(),
            "content_changed": set(),
            "unchanged": set(),
            "removed": set(),
        }
        scanner._scan_provider_identity_changed = set()
        scanner._scan_rejected_ids = set()
        scanner._scan_reconciled_ids = set()
        scanner._scan_refresh_root_ids = set()
        scanner._scan_complete = False

    @staticmethod
    def _finish_incremental_scan(scanner, library_id, root):
        scanner._reconcile_moved_entities(library_id, root)
        scanner._prune_rejected_entities()
        scanner._prune_missing_entities(library_id, root)

    @staticmethod
    def _finish_targeted_scan(scanner, library_id, root, targets):
        scanner._reconcile_moved_entities(library_id, root, targets)
        scanner._prune_rejected_entities(targets)
        scanner._prune_missing_entities(library_id, root, targets=targets)

    def test_file_scan_stages_never_persist_writer_updates(self):
        db, scanner = self._scanner_db()
        try:
            scanner.store.update_job = MagicMock()
            for index in range(20):
                scanner._set_stage(
                    "job-1",
                    f"Inspecting file-{index}",
                    persist=False,
                    entityId="entity-1",
                    path=f"file-{index}",
                )
            self.assertEqual(scanner.store.update_job.call_count, 0)

            scanner._set_stage(
                "job-1",
                "Indexing series",
            )
            self.assertEqual(scanner.store.update_job.call_count, 1)
        finally:
            db.close()

    def test_incremental_movie_scan_preserves_ids_and_reconciles_files(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                movie = root / "Movie (2020)"
                movie.mkdir()
                video = movie / "Movie.mkv"
                subtitle = movie / "Movie.en.srt"
                video.touch()
                subtitle.touch()

                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False)
                scanner._prune_missing_entities("library-1")
                entity_id = db.execute("SELECT id FROM library_entities")[0][0]
                db.execute(
                    "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
                    (entity_id, "tmdb", "movie", "123", 1),
                )

                subtitle.unlink()
                new_movie = root / "New Movie"
                new_movie.mkdir()
                (new_movie / "New.mkv").touch()
                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False)
                scanner._prune_missing_entities("library-1")

                self.assertEqual(
                    db.execute(
                        "SELECT id FROM library_entities WHERE relative_path='Movie (2020)'"
                    )[0][0],
                    entity_id,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT provider_id FROM entity_provider_ids WHERE entity_id=?",
                        (entity_id,),
                    )[0][0],
                    "123",
                )
                self.assertEqual(
                    db.execute(
                        "SELECT relative_path FROM media_files WHERE entity_id=?",
                        (entity_id,),
                    ),
                    [("Movie (2020)/Movie.mkv",)],
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM library_entities")[0][0], 2
                )
        finally:
            db.close()

    def test_media_file_quick_fingerprint_reuses_row_and_only_content_changes_probe(
        self,
    ):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                movie = root / "Movie"
                movie.mkdir()
                video = movie / "Movie.mkv"
                video.write_bytes(b"same content")

                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False)
                file_id, first_fingerprint, first_mtime = db.execute(
                    "SELECT id,quick_fingerprint,modified_ns FROM media_files"
                )[0]
                first_entity_updated_at = db.execute(
                    "SELECT updated_at FROM library_entities"
                )[0][0]

                with (
                    patch("app.playback.PlaybackManager") as playback,
                    patch(
                        "app.library._quick_fingerprint", wraps=_quick_fingerprint
                    ) as fingerprint,
                ):
                    self._prepare_incremental_scan(scanner)
                    scanner._scan_movies("library-1", root, "job-1", lambda: False)
                    fingerprint.assert_not_called()

                    os.utime(
                        video,
                        ns=(first_mtime + 2_000_000_000, first_mtime + 2_000_000_000),
                    )
                    self._prepare_incremental_scan(scanner)
                    scanner._scan_movies("library-1", root, "job-1", lambda: False)
                    self.assertEqual(fingerprint.call_count, 1)
                    self.assertEqual(
                        db.execute("SELECT id,quick_fingerprint FROM media_files")[0],
                        (file_id, first_fingerprint),
                    )
                    self.assertEqual(
                        db.execute("SELECT updated_at FROM library_entities")[0][0],
                        first_entity_updated_at,
                    )
                    playback.return_value.probe_entity.assert_not_called()

                    video.write_bytes(b"changed content")
                    self._prepare_incremental_scan(scanner)
                    scanner._scan_movies("library-1", root, "job-1", lambda: False)
                    self.assertEqual(fingerprint.call_count, 2)
                    self.assertEqual(
                        db.execute("SELECT id FROM media_files")[0][0], file_id
                    )
                    playback.return_value.probe_entity.assert_called_once()
        finally:
            db.close()

    def test_quick_fingerprint_samples_only_file_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.bin"
            sample = QUICK_FINGERPRINT_SAMPLE_SIZE
            path.write_bytes(b"a" * sample + b"middle" * sample + b"b" * sample)
            fingerprint, bytes_read = _quick_fingerprint(path)
            self.assertEqual(bytes_read, sample * 2)

            data = bytearray(path.read_bytes())
            data[sample + 10] = ord("c")
            path.write_bytes(data)
            middle_fingerprint, middle_bytes_read = _quick_fingerprint(path)
            self.assertEqual(middle_bytes_read, sample * 2)
            self.assertEqual(middle_fingerprint, fingerprint)

            data[10] = ord("z")
            path.write_bytes(data)
            first_fingerprint, _ = _quick_fingerprint(path)
            self.assertNotEqual(first_fingerprint, fingerprint)

            data[-10] = ord("z")
            path.write_bytes(data)
            last_fingerprint, _ = _quick_fingerprint(path)
            self.assertNotEqual(last_fingerprint, first_fingerprint)

    def test_sidecar_display_titles_preserve_descriptors_and_ignore_flags(self):
        media_paths = ["5 Centimeters per Second/5 Centimeters per Second.mkv"]
        self.assertEqual(
            sidecar_display_title(
                "5 Centimeters per Second/5 Centimeters per Second.AI 音声認識.ja.srt",
                "ja",
                "subtitle",
                media_paths,
            ),
            "AI 音声認識 - Japanese (日本語)",
        )
        self.assertEqual(
            sidecar_display_title(
                "5 Centimeters per Second/5 Centimeters per Second.ja.srt",
                "ja",
                "subtitle",
                media_paths,
            ),
            "Japanese (日本語)",
        )
        self.assertEqual(
            sidecar_display_title(
                "5 Centimeters per Second/5 Centimeters per Second.forced.sdh.cc.hi.ja.srt",
                "ja",
                "subtitle",
                media_paths,
            ),
            "Japanese (日本語)",
        )

        self.assertEqual(
            sidecar_display_title(
                "5 Centimeters per Second/5 Centimeters per Second.AI 生成.zh-TW.srt",
                "zh-TW",
                "subtitle",
                media_paths,
            ),
            "AI 生成 - Chinese (Taiwan)",
        )

    def test_sidecar_display_title_does_not_use_unmatched_movie_title_as_descriptor(
        self,
    ):
        self.assertEqual(
            sidecar_display_title(
                "Mr.Robot.en.srt",
                "en",
                "subtitle",
                ["Mr.Robot.mkv"],
            ),
            "English (English)",
        )

    def test_sidecar_scan_is_stat_only_and_retains_inaccessible_existing_rows(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sidecar = root / "Movie.AI 音声認識.ja.srt"
                sidecar.write_text("subtitle", encoding="utf-8")
                first_mtime = sidecar.stat().st_mtime_ns
                db.execute(
                    "INSERT INTO media_files(id,entity_id,relative_path,role,language,flags,size,modified_ns,quick_fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "sidecar-1",
                        "entity-1",
                        sidecar.name,
                        "subtitle",
                        "ja",
                        None,
                        1,
                        first_mtime,
                        "legacy-fingerprint",
                    ),
                )
                with patch("app.library._quick_fingerprint") as fingerprint:
                    result = scanner._files("entity-1", root, [sidecar])
                    fingerprint.assert_not_called()
                    self.assertEqual(result["updated"], 1)
                    self.assertEqual(
                        db.execute(
                            "SELECT id,size,modified_ns,quick_fingerprint FROM media_files WHERE entity_id='entity-1'"
                        )[0][0],
                        "sidecar-1",
                    )

                    with patch("app.library._bounded_sidecar_stat", return_value=None):
                        scanner._files("entity-1", root, [sidecar])
                    self.assertEqual(
                        db.execute(
                            "SELECT id FROM media_files WHERE entity_id='entity-1'"
                        )[0][0],
                        "sidecar-1",
                    )
        finally:
            db.close()

    def test_targeted_movie_scan_only_visits_affected_root(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("One", "Two"):
                    folder = root / name
                    folder.mkdir()
                    (folder / f"{name}.mkv").write_bytes(name.encode())

                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False)
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM library_entities")[0][0], 2
                )

                (root / "One" / "One.en.srt").write_text("subtitle")
                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False, {"One"})

                self.assertEqual(
                    db.execute(
                        "SELECT progress_total FROM library_jobs WHERE id='job-1'"
                    )[0][0],
                    1,
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM library_entities")[0][0], 2
                )
        finally:
            db.close()

    def test_renamed_movie_preserves_entity_and_file_identity(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original = root / "Original"
                original.mkdir()
                video = original / "Movie.mkv"
                video.write_bytes(b"stable identity")
                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False)
                old_entity, old_file = db.execute(
                    "SELECT e.id,f.id FROM library_entities e JOIN media_files f ON f.entity_id=e.id"
                )[0]

                renamed = root / "Renamed"
                original.rename(renamed)
                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False)
                scanner._reconcile_moved_entities("library-1", root)
                scanner._prune_rejected_entities()
                scanner._prune_missing_entities("library-1", root)

                self.assertEqual(
                    db.execute("SELECT id,relative_path FROM library_entities")[0],
                    (old_entity, "Renamed"),
                )
                self.assertEqual(
                    db.execute("SELECT id,relative_path FROM media_files")[0],
                    (old_file, "Renamed/Movie.mkv"),
                )
        finally:
            db.close()

    def test_movie_root_requires_a_playable_video(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                movie = root / "Empty Movie"
                movie.mkdir()
                (movie / "poster.jpg").touch()
                (movie / "Movie.en.srt").touch()

                self._prepare_incremental_scan(scanner)
                with patch.object(scanner, "_resolve_movie_row") as resolve:
                    count = scanner._scan_movies(
                        "library-1", root, "job-1", lambda: False
                    )

                self.assertEqual(count, 0)
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM library_entities")[0][0], 0
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM media_files")[0][0], 0
                )
                resolve.assert_not_called()
        finally:
            db.close()

    def test_movie_removed_when_its_directory_no_longer_has_playable_video(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                movie = root / "Movie"
                movie.mkdir()
                video = movie / "Movie.mkv"
                video.touch()

                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False)
                self._finish_incremental_scan(scanner, "library-1", root)
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM library_entities")[0][0], 1
                )

                video.unlink()
                (movie / "Movie.en.srt").touch()
                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False)
                self._finish_incremental_scan(scanner, "library-1", root)

                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM library_entities")[0][0], 0
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM media_files")[0][0], 0
                )
        finally:
            db.close()

    def test_targeted_empty_movie_cleanup_keeps_unrelated_movie(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("One", "Two"):
                    movie = root / name
                    movie.mkdir()
                    (movie / f"{name}.mkv").touch()

                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False)
                self._finish_incremental_scan(scanner, "library-1", root)

                (root / "One" / "One.mkv").unlink()
                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False, {"One"})
                self._finish_incremental_scan(scanner, "library-1", root)

                self.assertEqual(
                    db.execute(
                        "SELECT relative_path FROM library_entities ORDER BY relative_path"
                    ),
                    [("Two",)],
                )
        finally:
            db.close()

    def test_targeted_series_reconcile_admits_adds_and_removes_roots(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = root / "Example" / "Season 1"
                other = root / "Other" / "Season 1"
                first.mkdir(parents=True)
                other.mkdir(parents=True)
                episode_one = first / "Example - S01E01.mkv"
                episode_one.touch()
                (other / "Other - S01E01.mkv").touch()

                self._prepare_incremental_scan(scanner)
                scanner._scan_series("library-1", root, "job-1", lambda: False)
                self._finish_incremental_scan(scanner, "library-1", root)
                example_id, season_id = db.execute(
                    "SELECT s.id, se.id FROM library_entities s JOIN library_entities se ON se.parent_id=s.id WHERE s.relative_path='Example' AND se.relative_path='Example/Season 1'"
                )[0]

                new_episode = root / "Example" / "Season 1" / "Example - S01E02.mkv"
                new_episode.touch()
                self._prepare_incremental_scan(scanner)
                scanner._scan_series(
                    "library-1", root, "job-1", lambda: False, targets={"Example"}
                )
                scanner._reconcile_moved_entities("library-1", root, {"Example"})
                scanner._prune_rejected_entities({"Example"})
                scanner._prune_missing_entities("library-1", root, targets={"Example"})
                self.assertEqual(
                    db.execute(
                        "SELECT id FROM library_entities WHERE relative_path='Example'"
                    )[0][0],
                    example_id,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT id FROM library_entities WHERE relative_path='Example/Season 1'"
                    )[0][0],
                    season_id,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM library_entities WHERE entity_type='episode' AND parent_id=?",
                        (season_id,),
                    )[0][0],
                    2,
                )

                episode_one.unlink()
                self._prepare_incremental_scan(scanner)
                scanner._scan_series(
                    "library-1", root, "job-1", lambda: False, targets={"Example"}
                )
                scanner._reconcile_moved_entities("library-1", root, {"Example"})
                scanner._prune_rejected_entities({"Example"})
                scanner._prune_missing_entities("library-1", root, targets={"Example"})
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM library_entities WHERE entity_type='episode' AND parent_id=?",
                        (season_id,),
                    )[0][0],
                    1,
                )

                new_episode.unlink()
                self._prepare_incremental_scan(scanner)
                scanner._scan_series(
                    "library-1", root, "job-1", lambda: False, targets={"Example"}
                )
                scanner._reconcile_moved_entities("library-1", root, {"Example"})
                scanner._prune_rejected_entities({"Example"})
                scanner._prune_missing_entities("library-1", root, targets={"Example"})
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM library_entities WHERE relative_path LIKE 'Example%'"
                    )[0][0],
                    0,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM library_entities WHERE relative_path LIKE 'Other%'"
                    )[0][0],
                    3,
                )
        finally:
            db.close()

    def test_inaccessible_targeted_series_root_is_deferred_and_preserved(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                season = root / "Example" / "Season 1"
                season.mkdir(parents=True)
                episode = season / "Example - S01E01.mkv"
                episode.touch()
                self._prepare_incremental_scan(scanner)
                scanner._scan_series("library-1", root, "job-1", lambda: False)
                self._finish_incremental_scan(scanner, "library-1", root)
                before = db.execute(
                    "SELECT entity_type,relative_path FROM library_entities ORDER BY relative_path"
                )
                original_stat = Path.stat

                def inaccessible(path, *args, **kwargs):
                    if path == episode:
                        raise OSError("temporarily unavailable")
                    return original_stat(path, *args, **kwargs)

                with patch.object(Path, "stat", inaccessible):
                    self._prepare_incremental_scan(scanner)
                    scanner._scan_series(
                        "library-1", root, "job-1", lambda: False, targets={"Example"}
                    )
                    scanner._reconcile_moved_entities("library-1", root, {"Example"})
                    scanner._prune_rejected_entities({"Example"})
                    scanner._prune_missing_entities(
                        "library-1", root, targets={"Example"}
                    )
                self.assertEqual(
                    db.execute(
                        "SELECT entity_type,relative_path FROM library_entities ORDER BY relative_path"
                    ),
                    before,
                )
                self.assertIn("example", scanner._scan_deferred_roots)
        finally:
            db.close()

    def test_targeted_movie_reconcile_admits_and_removes_media(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                existing = root / "Existing"
                existing.mkdir()
                (existing / "Existing.mkv").touch()
                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False)
                self._finish_incremental_scan(scanner, "library-1", root)
                existing_id = db.execute(
                    "SELECT id FROM library_entities WHERE relative_path='Existing'"
                )[0][0]

                added = root / "Added"
                added.mkdir()
                added_video = added / "Added.mkv"
                added_video.touch()
                self._prepare_incremental_scan(scanner)
                scanner._scan_movies(
                    "library-1", root, "job-1", lambda: False, {"Added"}
                )
                self._finish_targeted_scan(scanner, "library-1", root, {"Added"})
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM library_entities WHERE relative_path='Added'"
                    )[0][0],
                    1,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT id FROM library_entities WHERE relative_path='Existing'"
                    )[0][0],
                    existing_id,
                )

                added_video.unlink()
                self._prepare_incremental_scan(scanner)
                scanner._scan_movies(
                    "library-1", root, "job-1", lambda: False, {"Added"}
                )
                self._finish_targeted_scan(scanner, "library-1", root, {"Added"})
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM library_entities WHERE relative_path='Added'"
                    )[0][0],
                    0,
                )
        finally:
            db.close()

    def test_removing_episode_cleans_its_metadata_but_keeps_shared_show_metadata(self):
        db, scanner = self._scanner_db()
        try:
            db.execute(
                "INSERT INTO library_entities(id,library_id,entity_type,relative_path) VALUES('show','library-1','series','Show')"
            )
            db.execute(
                "INSERT INTO library_entities(id,library_id,parent_id,entity_type,relative_path) VALUES('episode','library-1','show','episode','Show/Episode')"
            )
            db.execute(
                "INSERT INTO library_entities(id,library_id,entity_type,relative_path) VALUES('other','library-1','series','Other')"
            )
            db.execute(
                "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
                ("show", "tvdb", "series", "show-id", 1),
            )
            db.execute(
                "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
                ("episode", "tvdb", "episode", "episode-id", 1),
            )
            db.execute(
                "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
                ("other", "tvdb", "series", "show-id", 1),
            )
            for provider_id in ("show-id", "episode-id"):
                db.execute(
                    "INSERT INTO metadata_cache VALUES(?,?,?,?,?,?,?)",
                    (
                        "tvdb",
                        "series" if provider_id == "show-id" else "episode",
                        provider_id,
                        "en",
                        "{}",
                        "now",
                        "later",
                    ),
                )
            db.execute("INSERT INTO metadata_hydration_requests VALUES('episode','en')")

            cleanup_entities(db, ["episode"])

            self.assertEqual(
                db.execute("SELECT id FROM library_entities WHERE id='episode'"), []
            )
            self.assertEqual(
                db.execute("SELECT entity_id FROM metadata_hydration_requests"), []
            )
            self.assertEqual(
                db.execute(
                    "SELECT provider_id FROM metadata_cache ORDER BY provider_id"
                ),
                [("show-id",)],
            )
            self.assertEqual(
                db.execute(
                    "SELECT provider_id FROM entity_provider_ids ORDER BY provider_id"
                ),
                [("show-id",), ("show-id",)],
            )
        finally:
            db.close()

    def test_deleting_library_removes_entities_jobs_and_unreferenced_metadata(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            db.execute("CREATE TABLE libraries (id TEXT PRIMARY KEY)")
            db.execute(
                "CREATE TABLE library_sources (library_id TEXT, source_library_id TEXT)"
            )
            db.execute(
                "CREATE TABLE library_jobs (id TEXT PRIMARY KEY, library_id TEXT)"
            )
            db.execute(
                "CREATE TABLE library_entities (id TEXT PRIMARY KEY, library_id TEXT, parent_id TEXT, entity_type TEXT)"
            )
            db.execute(
                "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT)"
            )
            db.execute("CREATE TABLE media_files (id TEXT, entity_id TEXT)")
            db.execute(
                "CREATE TABLE collection_members (collection_entity_id TEXT, source_entity_id TEXT, position INTEGER)"
            )
            db.execute(
                "CREATE TABLE metadata_hydration_requests (entity_id TEXT, locale TEXT)"
            )
            db.execute(
                "CREATE TABLE metadata_cache (provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, payload TEXT, fetched_at TEXT, expires_at TEXT)"
            )
            db.execute(
                "CREATE TABLE metadata_images (provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, image_type TEXT, image_url TEXT, local_path TEXT)"
            )
            db.execute("INSERT INTO libraries VALUES('library-1')")
            db.execute(
                "INSERT INTO library_entities VALUES('show','library-1',NULL,'series')"
            )
            db.execute(
                "INSERT INTO library_entities VALUES('episode','library-1','show','episode')"
            )
            db.execute(
                "INSERT INTO entity_provider_ids VALUES('show','tvdb','series','show-id')"
            )
            db.execute(
                "INSERT INTO entity_provider_ids VALUES('episode','tvdb','episode','episode-id')"
            )
            db.execute("INSERT INTO media_files VALUES('file','episode')")
            db.execute("INSERT INTO metadata_hydration_requests VALUES('episode','en')")
            db.execute(
                "INSERT INTO metadata_cache VALUES('tvdb','series','show-id','en','{}','now','later')"
            )
            db.execute(
                "INSERT INTO metadata_cache VALUES('tvdb','series','show-id','ja','{}','now','later')"
            )
            db.execute(
                "INSERT INTO metadata_cache VALUES('tvdb','episode','episode-id','en','{}','now','later')"
            )
            db.execute(
                "INSERT INTO metadata_cache VALUES('tvdb','episode','episode-id','ja','{}','now','later')"
            )
            db.execute(
                "INSERT INTO metadata_cache VALUES('tvdb','movie','old-orphan','ja','{}','now','later')"
            )
            db.execute("INSERT INTO library_jobs VALUES('job','library-1')")

            self.assertTrue(cleanup_library(db, "library-1"))
            for table in (
                "libraries",
                "library_entities",
                "entity_provider_ids",
                "media_files",
                "metadata_hydration_requests",
                "metadata_cache",
                "library_jobs",
            ):
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) FROM {table}")[0][0], 0, table
                )
        finally:
            db.close()

    def test_library_cleanup_rolls_back_when_final_library_delete_fails(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            db.execute("CREATE TABLE libraries (id TEXT PRIMARY KEY)")
            db.execute(
                "CREATE TABLE library_sources (library_id TEXT, source_library_id TEXT)"
            )
            db.execute("CREATE TABLE library_jobs (id TEXT, library_id TEXT)")
            db.execute(
                "CREATE TABLE library_entities (id TEXT PRIMARY KEY, library_id TEXT, parent_id TEXT, entity_type TEXT)"
            )
            db.execute(
                "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT)"
            )
            db.execute("CREATE TABLE media_files (id TEXT, entity_id TEXT)")
            db.execute(
                "CREATE TABLE metadata_cache (provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, payload TEXT, fetched_at TEXT, expires_at TEXT)"
            )
            db.execute(
                "CREATE TRIGGER refuse_library_delete BEFORE DELETE ON libraries BEGIN SELECT RAISE(ABORT, 'delete blocked'); END"
            )
            db.execute("INSERT INTO libraries VALUES('library-1')")
            db.execute(
                "INSERT INTO library_entities VALUES('movie','library-1',NULL,'movie')"
            )
            db.execute(
                "INSERT INTO entity_provider_ids VALUES('movie','tmdb','movie','movie-id')"
            )
            db.execute("INSERT INTO media_files VALUES('file','movie')")

            with self.assertRaises(Exception):
                cleanup_library(db, "library-1")

            self.assertEqual(db.execute("SELECT COUNT(*) FROM libraries")[0][0], 1)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM library_entities")[0][0], 1
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM entity_provider_ids")[0][0], 1
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM media_files")[0][0], 1)
        finally:
            db.close()

    def test_library_delete_endpoint_does_not_continue_after_cleanup_failure(self):
        async def invoke():
            with (
                patch.object(library_routes, "require_admin"),
                patch.object(
                    library_routes.store,
                    "delete",
                    side_effect=RuntimeError("cleanup failed"),
                ),
                patch.object(
                    library_routes.scheduler, "remove_library_definition"
                ) as remove,
            ):
                with self.assertRaises(RuntimeError):
                    await library_routes.delete_library("library-1", "admin", "token")
                remove.assert_not_called()

        asyncio.run(invoke())

    def test_deleting_last_entity_removes_cached_image_file(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseHandler("sqlite", {}, str(Path(directory) / "orchestrator.db"))
            try:
                for sql in (
                    "CREATE TABLE library_entities(id TEXT PRIMARY KEY, library_id TEXT, parent_id TEXT, entity_type TEXT)",
                    "CREATE TABLE entity_provider_ids(entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT)",
                    "CREATE TABLE media_files(id TEXT, entity_id TEXT)",
                    "CREATE TABLE collection_members(collection_entity_id TEXT, source_entity_id TEXT, position INTEGER)",
                    "CREATE TABLE metadata_hydration_requests(entity_id TEXT, locale TEXT)",
                    "CREATE TABLE metadata_cache(provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, payload TEXT, fetched_at TEXT, expires_at TEXT)",
                    "CREATE TABLE metadata_images(provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, image_type TEXT, image_url TEXT, local_path TEXT)",
                ):
                    db.execute(sql)
                image_dir = Path(directory) / "metadata-cache" / "images"
                image_dir.mkdir(parents=True)
                image_path = image_dir / "cached.jpg"
                image_path.touch()
                db.execute(
                    "INSERT INTO library_entities VALUES('movie','library-1',NULL,'movie')"
                )
                db.execute(
                    "INSERT INTO entity_provider_ids VALUES('movie','tmdb','movie','movie-id')"
                )
                db.execute(
                    "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?)",
                    (
                        "tmdb",
                        "movie",
                        "movie-id",
                        "en",
                        "Primary",
                        "https://image",
                        str(image_path),
                    ),
                )

                cleanup_entities(db, ["movie"])

                self.assertFalse(image_path.exists())
            finally:
                db.close()

    def test_enumerated_file_stat_is_reused_during_reconciliation(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                video = root / "Movie.mkv"
                video.write_bytes(b"video")
                entries = list(scanner._walk_file_entries(root))

                with (
                    patch.object(
                        Path,
                        "stat",
                        side_effect=AssertionError("unexpected second stat"),
                    ),
                    patch("app.playback.PlaybackManager.probe_entity"),
                ):
                    result = scanner._files("entity-1", root, entries)

                self.assertEqual(result["added"], 1)
        finally:
            db.close()

    def test_sidecar_stat_timeout_includes_waiting_for_shared_worker(self):
        worker = _SidecarStatWorker()
        worker._lock.acquire()
        started = time.monotonic()
        try:
            self.assertIsNone(worker.stat(Path("blocked.srt"), timeout=0.05))
        finally:
            worker._lock.release()
        self.assertLess(time.monotonic() - started, 0.25)

    def test_deleting_large_library_batches_sqlite_variables(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            db.execute("CREATE TABLE libraries (id TEXT PRIMARY KEY)")
            db.execute(
                "CREATE TABLE library_sources (library_id TEXT, source_library_id TEXT)"
            )
            db.execute("CREATE TABLE library_jobs (id TEXT, library_id TEXT)")
            db.execute(
                "CREATE TABLE library_entities (id TEXT PRIMARY KEY, library_id TEXT, parent_id TEXT, entity_type TEXT)"
            )
            db.execute(
                "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT)"
            )
            db.execute("CREATE TABLE media_files (id TEXT, entity_id TEXT)")
            db.execute(
                "CREATE TABLE collection_members (collection_entity_id TEXT, source_entity_id TEXT, position INTEGER)"
            )
            db.execute(
                "CREATE TABLE metadata_hydration_requests (entity_id TEXT, locale TEXT)"
            )
            db.execute(
                "CREATE TABLE metadata_cache (provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, payload TEXT, fetched_at TEXT, expires_at TEXT)"
            )
            db.execute(
                "CREATE TABLE metadata_images (provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, image_type TEXT, image_url TEXT, local_path TEXT)"
            )
            db.execute("INSERT INTO libraries VALUES('large-library')")
            for index in range(1100):
                entity_id = f"entity-{index}"
                db.execute(
                    "INSERT INTO library_entities VALUES(?,?,NULL,'movie')",
                    (entity_id, "large-library"),
                )
                db.execute(
                    "INSERT INTO entity_provider_ids VALUES(?,?,?,?)",
                    (entity_id, "tmdb", "movie", entity_id),
                )
                db.execute(
                    "INSERT INTO media_files VALUES(?,?)", (f"file-{index}", entity_id)
                )
                db.execute(
                    "INSERT INTO metadata_hydration_requests VALUES(?,?)",
                    (entity_id, "en"),
                )

            self.assertTrue(cleanup_library(db, "large-library"))
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM library_entities")[0][0], 0
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM entity_provider_ids")[0][0], 0
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM media_files")[0][0], 0)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM metadata_hydration_requests")[0][0], 0
            )
        finally:
            db.close()

    def test_orphan_cleanup_removes_leftovers_when_no_libraries_remain(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            db.execute("CREATE TABLE libraries (id TEXT PRIMARY KEY)")
            db.execute(
                "CREATE TABLE library_entities (id TEXT PRIMARY KEY, library_id TEXT, parent_id TEXT, entity_type TEXT)"
            )
            db.execute(
                "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT)"
            )
            db.execute("CREATE TABLE media_files (id TEXT, entity_id TEXT)")
            db.execute(
                "CREATE TABLE collection_members (collection_entity_id TEXT, source_entity_id TEXT, position INTEGER)"
            )
            db.execute(
                "CREATE TABLE metadata_hydration_requests (entity_id TEXT, locale TEXT)"
            )
            db.execute(
                "CREATE TABLE metadata_cache (provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, payload TEXT, fetched_at TEXT, expires_at TEXT)"
            )
            db.execute(
                "CREATE TABLE metadata_images (provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, image_type TEXT, image_url TEXT, local_path TEXT)"
            )
            db.execute(
                "INSERT INTO library_entities VALUES('old','deleted-library',NULL,'movie')"
            )
            db.execute(
                "INSERT INTO entity_provider_ids VALUES('old','tmdb','movie','old-movie')"
            )
            db.execute("INSERT INTO media_files VALUES('old-file','old')")
            db.execute("INSERT INTO metadata_hydration_requests VALUES('old','ja')")
            db.execute(
                "INSERT INTO metadata_cache VALUES('tmdb','movie','old-movie','ja','{}','now','later')"
            )
            db.execute(
                "INSERT INTO metadata_cache VALUES('tmdb','movie','orphan','en','{}','now','later')"
            )

            cleanup_orphans(db)

            for table in (
                "library_entities",
                "entity_provider_ids",
                "media_files",
                "metadata_hydration_requests",
                "metadata_cache",
                "metadata_images",
            ):
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) FROM {table}")[0][0], 0, table
                )
        finally:
            db.close()

    def test_incremental_series_scan_preserves_hierarchy_and_removes_missing_episode(
        self,
    ):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                season = root / "Example" / "Season 1"
                season.mkdir(parents=True)
                first = season / "Example - S01E01.mkv"
                first.touch()

                self._prepare_incremental_scan(scanner)
                scanner._scan_series("library-1", root, "job-1", lambda: False)
                scanner._prune_missing_entities("library-1")
                original = dict(
                    (row[1], row[0])
                    for row in db.execute(
                        "SELECT id,relative_path FROM library_entities"
                    )
                )

                second = season / "Example - S01E02.mkv"
                second.touch()
                self._prepare_incremental_scan(scanner)
                scanner._scan_series("library-1", root, "job-1", lambda: False)
                scanner._prune_missing_entities("library-1")
                self.assertEqual(
                    db.execute(
                        "SELECT id FROM library_entities WHERE relative_path='Example'"
                    )[0][0],
                    original["Example"],
                )
                self.assertEqual(
                    db.execute(
                        "SELECT id FROM library_entities WHERE relative_path='Example/Season 1'"
                    )[0][0],
                    original["Example/Season 1"],
                )

                first.unlink()
                self._prepare_incremental_scan(scanner)
                scanner._scan_series("library-1", root, "job-1", lambda: False)
                scanner._prune_missing_entities("library-1")
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM library_entities WHERE entity_type='episode'"
                    )[0][0],
                    1,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT relative_path FROM library_entities WHERE entity_type='episode'"
                    ),
                    [("Example/Season 1/Example - S01E02.mkv",)],
                )
        finally:
            db.close()

    def test_series_scan_publishes_each_admitted_series_in_order(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("Alpha", "Beta"):
                    season = root / name / "Season 1"
                    season.mkdir(parents=True)
                    (season / f"{name} - S01E01.mkv").touch()
                self._prepare_incremental_scan(scanner)
                scanner._publish_root = MagicMock()

                scanner._scan_series("library-1", root, "job-1", lambda: False)

                published_paths = [
                    db.execute(
                        "SELECT relative_path FROM library_entities WHERE id=?",
                        (call.args[0],),
                    )[0][0]
                    for call in scanner._publish_root.call_args_list
                ]
                self.assertEqual(published_paths, ["Alpha", "Beta"])
        finally:
            db.close()

    def test_tv_scan_admits_only_series_and_seasons_with_playable_episodes(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first_season = root / "Example" / "Season 1"
                empty_season = root / "Example" / "Season 2"
                empty_series = root / "Empty" / "Season 1"
                unmapped_series = root / "Unmapped"
                for path in (first_season, empty_season, empty_series, unmapped_series):
                    path.mkdir(parents=True)
                episode = first_season / "Example - S01E01.mkv"
                episode.touch()
                (first_season / "Example - S01E01.en.srt").touch()
                (empty_season / "poster.jpg").touch()
                (empty_series / "poster.jpg").touch()
                (unmapped_series / "feature.mkv").touch()

                self._prepare_incremental_scan(scanner)
                count = scanner._scan_series("library-1", root, "job-1", lambda: False)
                self._finish_incremental_scan(scanner, "library-1", root)

                self.assertEqual(count, 1)
                self.assertEqual(
                    db.execute(
                        "SELECT entity_type,relative_path FROM library_entities ORDER BY length(relative_path),relative_path"
                    ),
                    [
                        ("series", "Example"),
                        ("season", "Example/Season 1"),
                        ("episode", "Example/Season 1/Example - S01E01.mkv"),
                    ],
                )
                self.assertEqual(
                    db.execute("SELECT role FROM media_files ORDER BY role"),
                    [("media",), ("subtitle",)],
                )
        finally:
            db.close()

    def test_series_removed_when_last_playable_episode_disappears(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                season = root / "Example" / "Season 1"
                season.mkdir(parents=True)
                episode = season / "Example - S01E01.mkv"
                episode.touch()

                self._prepare_incremental_scan(scanner)
                scanner._scan_series("library-1", root, "job-1", lambda: False)
                self._finish_incremental_scan(scanner, "library-1", root)
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM library_entities")[0][0], 3
                )

                episode.unlink()
                self._prepare_incremental_scan(scanner)
                scanner._scan_series("library-1", root, "job-1", lambda: False)
                self._finish_incremental_scan(scanner, "library-1", root)

                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM library_entities")[0][0], 0
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM media_files")[0][0], 0
                )
        finally:
            db.close()

    def test_movie_scan_failure_does_not_prune_existing_entity(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                movie = root / "Movie"
                movie.mkdir()
                (movie / "Movie.mkv").touch()

                self._prepare_incremental_scan(scanner)
                scanner._scan_movies("library-1", root, "job-1", lambda: False)
                self._finish_incremental_scan(scanner, "library-1", root)
                entity_id = db.execute("SELECT id FROM library_entities")[0][0]

                self._prepare_incremental_scan(scanner)
                with patch.object(
                    scanner,
                    "_walk_file_entries",
                    side_effect=PermissionError("denied"),
                ):
                    scanner._scan_movies("library-1", root, "job-1", lambda: False)

                self.assertTrue(scanner._scan_complete)
                self.assertEqual(
                    db.execute("SELECT id FROM library_entities"), [(entity_id,)]
                )
                self.assertTrue(scanner._scan_deferred_roots)
        finally:
            db.close()

    def test_movie_disappearing_after_preflight_leaves_no_provisional_entity(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                movie = root / "Movie"
                movie.mkdir()
                video = movie / "Movie.mkv"
                video.touch()
                original_files = scanner._files

                def remove_before_reconcile(*args, **kwargs):
                    video.unlink()
                    return original_files(*args, **kwargs)

                self._prepare_incremental_scan(scanner)
                with patch.object(
                    scanner, "_files", side_effect=remove_before_reconcile
                ):
                    scanner._scan_movies("library-1", root, "job-1", lambda: False)
                self._finish_incremental_scan(scanner, "library-1", root)

                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM library_entities")[0][0], 0
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM media_files")[0][0], 0
                )
        finally:
            db.close()

    def test_series_root_resolution_does_not_seed_children_before_inventory(self):
        db, scanner = self._scanner_db()
        try:
            db.execute(
                "INSERT INTO library_entities(id,library_id,entity_type,relative_path) VALUES('series-1','library-1','series','Example [tvdbid-12345]')"
            )
            scanner._scan_seen_ids = {"series-1"}
            scanner._scan_created_ids = ["series-1"]
            service = MagicMock()
            with (
                patch.object(scanner, "_aggregate_series_children") as aggregate,
                patch.object(scanner, "_derive_tvdb_episode_ids") as derive,
                patch.object(scanner, "_seed_all_children") as seed,
                patch.object(scanner, "_fetch_configured_locales"),
            ):
                service.resolve_inventory_entity.return_value = {
                    "providerIds": [{"provider": "tvdb", "id": "12345"}],
                    "metadata": {"children": []},
                }
                scanner._resolve_series_root(
                    "library-1",
                    "series-1",
                    "Example [tvdbid-12345]",
                    service,
                    "job-1",
                    lambda: False,
                )

            aggregate.assert_not_called()
            derive.assert_not_called()
            seed.assert_not_called()
        finally:
            db.close()

    def test_series_scan_child_query_binds_series_parent_twice(self):
        db, scanner = self._scanner_db()
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS metadata_cache (provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, payload TEXT, fetched_at TEXT, expires_at TEXT, PRIMARY KEY(provider, entity_type, provider_id, locale))"
            )
            db.execute(
                "INSERT INTO library_entities(id,library_id,parent_id,entity_type,relative_path) VALUES(?,?,?,?,?)",
                ("series-1", "library-1", None, "series", "Example"),
            )
            db.execute(
                "INSERT INTO library_entities(id,library_id,parent_id,entity_type,relative_path) VALUES(?,?,?,?,?)",
                ("season-1", "library-1", "series-1", "season", "Example/Season 1"),
            )
            db.execute(
                "INSERT INTO library_entities(id,library_id,parent_id,entity_type,relative_path) VALUES(?,?,?,?,?)",
                (
                    "episode-1",
                    "library-1",
                    "season-1",
                    "episode",
                    "Example/Season 1/Episode 1",
                ),
            )
            children = scanner.db.execute(
                "SELECT id FROM library_entities WHERE library_id=? AND (parent_id=? OR parent_id IN (SELECT id FROM library_entities WHERE parent_id=? AND entity_type='season'))",
                ("library-1", "series-1", "series-1"),
            )
            self.assertEqual({row[0] for row in children}, {"season-1", "episode-1"})
        finally:
            db.close()

    def test_incremental_music_scan_removes_stale_track_without_resetting_release(self):
        db, scanner = self._scanner_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                album = root / "Artist" / "Album"
                album.mkdir(parents=True)
                first = album / "01 - First.mp3"
                second = album / "02 - Second.mp3"
                first.touch()
                second.touch()

                self._prepare_incremental_scan(scanner)
                scanner._scan_music("library-1", root, "job-1", lambda: False)
                scanner._prune_missing_entities("library-1")
                release_id = db.execute(
                    "SELECT id FROM library_entities WHERE entity_type='release'"
                )[0][0]

                second.unlink()
                self._prepare_incremental_scan(scanner)
                scanner._scan_music("library-1", root, "job-1", lambda: False)
                scanner._prune_missing_entities("library-1")
                self.assertEqual(
                    db.execute(
                        "SELECT id FROM library_entities WHERE entity_type='release'"
                    )[0][0],
                    release_id,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM library_entities WHERE entity_type='track'"
                    )[0][0],
                    1,
                )
        finally:
            db.close()

    def test_jellyfin_style_provider_ids_are_extracted(self):
        self.assertEqual(
            provider_ids("The Matrix (1999) [tmdbid-603] [tvdbid-Movie-123]"),
            [("tmdb", "movie", "603"), ("tvdb", "series", "Movie-123")],
        )

    def test_guessit_fallback_reads_season_and_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Show - S02E03 - Episode.mkv"
            path.touch()
            parsed = guess_media(path)
            self.assertEqual(int(parsed["season"]), 2)
            self.assertEqual(int(parsed["episode"]), 3)

    def test_episode_names_support_arbitrarily_long_season_and_episode_numbers(self):
        match = EPISODE_RE.search("Example - S101E289.mkv")
        self.assertIsNotNone(match)
        self.assertEqual(
            (match.group("season"), match.group("episode")), ("101", "289")
        )
        match = EPISODE_RE.search("Example - S12345E67890-E67891.mkv")
        self.assertIsNotNone(match)
        self.assertEqual(
            (match.group("season"), match.group("episode"), match.group("end")),
            ("12345", "67890", "67891"),
        )

    def test_series_scan_maps_episode_files_to_parent_season_directories(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute(
            "CREATE TABLE library_entities (id TEXT PRIMARY KEY, library_id TEXT NOT NULL, parent_id TEXT, entity_type TEXT NOT NULL, relative_path TEXT, season_number INTEGER, episode_number INTEGER, episode_end_number INTEGER, disc_number INTEGER, track_number INTEGER, created_at TEXT, updated_at TEXT, match_status TEXT DEFAULT 'unresolved', match_confidence REAL, match_method TEXT)"
        )
        db.execute(
            "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)"
        )
        db.execute(
            "CREATE TABLE media_files (id TEXT PRIMARY KEY, entity_id TEXT, relative_path TEXT, role TEXT, language TEXT, flags TEXT, size INTEGER, modified_ns INTEGER)"
        )
        db.execute(
            "CREATE TABLE library_jobs (id TEXT PRIMARY KEY, progress_current INTEGER, progress_total INTEGER, message TEXT)"
        )
        db.execute("INSERT INTO library_jobs(id) VALUES('job-1')")
        store = LibraryStore.__new__(LibraryStore)
        store.db = db
        scanner = LibraryScanner(store)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            season = root / "Example" / "Season 1"
            specials = root / "Example" / "Specials"
            season.mkdir(parents=True)
            specials.mkdir(parents=True)
            (season / "Example - S101E289.mkv").touch()
            (specials / "Example - S00E12345.mkv").touch()
            with (
                patch("app.providers.MetadataService", return_value=MagicMock()),
                patch.object(
                    scanner, "_resolve_series_root", return_value=None
                ) as resolve,
                patch.object(scanner, "_resolve_season_metadata"),
            ):
                scanner._scan_series(
                    "library-1", root, "job-1", lambda: False, resolve_immediately=True
                )

        rows = db.execute(
            "SELECT e.season_number,e.episode_number,s.season_number FROM library_entities e JOIN library_entities s ON s.id=e.parent_id WHERE e.entity_type='episode' ORDER BY e.episode_number"
        )
        self.assertEqual(rows, [(1, 289, 1), (0, 12345, 0)])
        self.assertEqual(resolve.call_count, 1)
        db.close()

    def test_tv_scan_resolves_series_and_each_season_in_numeric_order(self):
        db, scanner = self._scanner_db()
        events = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = root / "Example" / "Season 1"
                second = root / "Example" / "Season 2"
                first.mkdir(parents=True)
                second.mkdir(parents=True)
                (second / "Example - S02E02.mkv").touch()
                (second / "Example - S02E01.mkv").touch()
                (first / "Example - S01E02.mkv").touch()
                (first / "Example - S01E01.mkv").touch()

                def resolve_root(*args, **kwargs):
                    events.append("series")
                    return {"children": []}

                def resolve_season(*args, **kwargs):
                    season_id = args[2]
                    season_number = db.execute(
                        "SELECT season_number FROM library_entities WHERE id=?",
                        (season_id,),
                    )[0][0]
                    children = db.execute(
                        "SELECT episode_number FROM library_entities WHERE parent_id=? ORDER BY episode_number",
                        (season_id,),
                    )
                    self.assertTrue(children)
                    events.append((season_number, [row[0] for row in children]))

                with (
                    patch("app.providers.MetadataService", return_value=MagicMock()),
                    patch.object(
                        scanner, "_resolve_series_root", side_effect=resolve_root
                    ),
                    patch.object(
                        scanner, "_resolve_season_metadata", side_effect=resolve_season
                    ),
                ):
                    scanner._scan_series(
                        "library-1",
                        root,
                        "job-1",
                        lambda: False,
                        resolve_immediately=True,
                    )

            self.assertEqual(events, ["series", (1, [1, 2]), (2, [1, 2])])
        finally:
            db.close()

    def test_image_fallback_order_is_requested_no_language_english_any(self):
        images = [
            {"type": PRIMARY, "language": "fr", "url": "fr"},
            {"type": PRIMARY, "language": "en", "url": "en"},
            {"type": PRIMARY, "language": None, "url": "neutral"},
            {"type": PRIMARY, "language": "ja", "url": "ja"},
        ]
        self.assertEqual(choose_image(images, "ja-JP", PRIMARY)["url"], "ja")
        self.assertEqual(choose_image(images, "en", PRIMARY)["url"], "en")
        self.assertEqual(choose_image(images, "de-DE", PRIMARY)["url"], "neutral")
        with self.assertRaises(ValueError):
            choose_image(images, "en", "Thumb")

    def test_image_selection_preserves_provider_order_over_local_score(self):
        images = [
            {
                "type": PRIMARY,
                "language": "en",
                "url": "first",
                "score": 0,
                "width": 100,
            },
            {
                "type": PRIMARY,
                "language": "en",
                "url": "second",
                "score": 100,
                "width": 4000,
            },
        ]
        self.assertEqual(choose_image(images, "en", PRIMARY)["url"], "first")

    def test_image_fallback_does_not_prefer_an_unrequested_language(self):
        images = [
            {"type": PRIMARY, "language": "fr", "url": "fr"},
            {"type": PRIMARY, "language": "de", "url": "de"},
        ]
        self.assertIsNone(choose_image(images, "es", PRIMARY))

    def test_tvdb_artwork_keeps_raw_code_and_normalizes_catalog_code(self):
        images, _ = _tvdb_images(
            "series",
            {
                "artworks": [
                    {"type": "poster", "language": "provider-jpn", "image": "poster"}
                ]
            },
            lambda value: "ja" if value == "provider-jpn" else value,
        )
        self.assertEqual(images[0]["language"], "ja")
        self.assertEqual(images[0]["providerLanguage"], "provider-jpn")

    def test_metadata_cache_rejects_legacy_image_language_payloads(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute(
            "CREATE TABLE metadata_cache (provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, payload TEXT, fetched_at TEXT, expires_at TEXT, PRIMARY KEY(provider,entity_type,provider_id,locale))"
        )
        cache = MetadataCache.__new__(MetadataCache)
        cache.db = db
        db.execute(
            "INSERT INTO metadata_cache VALUES(?,?,?,?,?,?,?)",
            (
                "tvdb",
                "series",
                "1",
                "en",
                '{"title":"Legacy","images":[{"type":"Primary","language":null,"url":"legacy"}]}',
                "2020-01-01",
                "2999-01-01",
            ),
        )
        self.assertIsNone(cache.get("tvdb", "series", "1", "en"))
        cache.put("tvdb", "series", "1", "en", {"title": "Current", "images": []})
        current = cache.get("tvdb", "series", "1", "en")
        self.assertEqual(current["_imageLanguageSchema"], IMAGE_LANGUAGE_SCHEMA)
        db.close()

    def test_library_search_uses_trigrams_and_requested_locale_titles(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute(
            "CREATE TABLE library_entities (id TEXT PRIMARY KEY, library_id TEXT NOT NULL, parent_id TEXT, entity_type TEXT NOT NULL, relative_path TEXT)"
        )
        db.execute(
            "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)"
        )
        db.execute(
            "CREATE TABLE metadata_cache (provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, payload TEXT, fetched_at TEXT, expires_at TEXT, PRIMARY KEY(provider,entity_type,provider_id,locale))"
        )
        db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?)",
            ("gintama", "library-1", None, "series", "shows/entry-001"),
        )
        db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?)",
            ("ghost", "library-1", None, "series", "shows/entry-002"),
        )
        db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?)",
            ("child", "library-1", "gintama", "season", "shows/entry-001/season-1"),
        )
        db.execute(
            "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
            ("gintama", "tvdb", "series", "1", 1),
        )
        db.execute(
            "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
            ("ghost", "tvdb", "series", "2", 1),
        )
        cache = MetadataCache.__new__(MetadataCache)
        cache.db = db
        cache.put(
            "tvdb",
            "series",
            "1",
            "en",
            {"title": "Gintama - Mr. Ginpachi's Zany Class", "images": []},
        )
        MetadataSearchProjection(db).project(
            "tvdb",
            "series",
            "1",
            "en",
            {"title": "Gintama - Mr. Ginpachi's Zany Class"},
        )
        cache.put("tvdb", "series", "1", "ja", {"title": "銀魂", "images": []})
        MetadataSearchProjection(db).project(
            "tvdb", "series", "1", "ja", {"title": "銀魂"}
        )
        cache.put("tvdb", "series", "2", "en", {"title": "07-Ghost", "images": []})
        MetadataSearchProjection(db).project(
            "tvdb", "series", "2", "en", {"title": "07-Ghost"}
        )

        self.assertEqual(
            library_routes._rank_library_item_ids(
                db, "library-1", None, "en", "gintma"
            ),
            ["gintama", "ghost"],
        )
        self.assertEqual(
            library_routes._rank_library_item_ids(
                db, "library-1", None, "en", "07 ghost"
            ),
            ["ghost", "gintama"],
        )
        self.assertEqual(
            library_routes._rank_library_item_ids(db, "library-1", None, "ja", "銀魂"),
            ["gintama"],
        )
        self.assertEqual(
            library_routes._rank_library_item_ids(db, "library-1", None, "en", "銀魂"),
            [],
        )
        self.assertNotIn(
            "child",
            library_routes._rank_library_item_ids(
                db, "library-1", None, "en", "season"
            ),
        )
        db.close()

    def test_preview_image_cache_miss_does_not_hydrate_provider_metadata(self):
        item = {
            "id": "entity-1",
            "libraryId": "library-1",
            "type": "movie",
            "providerIds": [{"provider": "tmdb", "id": "603"}],
        }
        with (
            patch.object(library_routes, "require_admin"),
            patch.object(library_routes, "_entity", return_value=item),
            patch.object(
                library_routes.store,
                "get",
                return_value={"id": "library-1", "directory": None},
            ),
            patch.object(library_routes.store.db, "execute", return_value=[]),
            patch.object(
                library_routes, "_metadata_for", return_value=None
            ) as metadata,
        ):
            response = asyncio.run(
                library_routes.get_image(
                    "entity-1",
                    imageType=PRIMARY,
                    locale="en",
                    Username="admin",
                    TOKEN="token",
                )
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["retry-after"], "2")
        metadata.assert_called_once_with(item, "en", False, False)

    def test_metadata_language_update_queues_existing_entity_backfill(self):
        settings = MagicMock()
        settings.update.return_value = {
            "locales": ["en", "zh-TW"],
            "preferNoLanguageForBackdrop": True,
        }
        with (
            patch.object(library_routes, "require_admin"),
            patch.object(
                library_routes, "MetadataLanguageSettings", return_value=settings
            ),
            patch.object(
                library_routes.scheduler,
                "enqueue_metadata_refresh",
                return_value={"id": "run-1"},
            ) as refresh,
        ):
            response = asyncio.run(
                library_routes.update_metadata_languages(
                    _JsonRequest(
                        {
                            "locales": ["en", "zh-TW"],
                            "preferNoLanguageForBackdrop": True,
                        }
                    ),
                    Username="admin",
                    TOKEN="token",
                )
            )
        self.assertEqual(response["locales"], ["en", "zh-TW"])
        self.assertTrue(response["preferNoLanguageForBackdrop"])
        settings.update.assert_called_once_with(["en", "zh-TW"], True)
        refresh.assert_called_once_with()

    def test_metadata_language_update_preserves_option_when_omitted(self):
        settings = MagicMock()
        settings.update.return_value = {
            "locales": ["en"],
            "preferNoLanguageForBackdrop": True,
        }
        with (
            patch.object(library_routes, "require_admin"),
            patch.object(
                library_routes, "MetadataLanguageSettings", return_value=settings
            ),
            patch.object(
                library_routes.scheduler,
                "enqueue_metadata_refresh",
                return_value={"id": "run-1"},
            ),
        ):
            response = asyncio.run(
                library_routes.update_metadata_languages(
                    _JsonRequest({"locales": ["en"]}),
                    Username="admin",
                    TOKEN="token",
                )
            )

        self.assertTrue(response["preferNoLanguageForBackdrop"])
        settings.update.assert_called_once_with(["en"])

    def test_explicit_metadata_refresh_queues_existing_entity_backfill(self):
        with (
            patch.object(library_routes, "require_admin"),
            patch.object(
                library_routes.scheduler,
                "enqueue_metadata_refresh",
                return_value={"id": "run-1"},
            ) as refresh,
        ):
            response = asyncio.run(
                library_routes.refresh_metadata(Username="admin", TOKEN="token")
            )
        self.assertEqual(response, {"backfill": {"id": "run-1"}})
        refresh.assert_called_once_with()

    def test_item_metadata_refresh_forces_all_locales_assets_and_publication(self):
        ingest = MagicMock()
        ingest.locales.return_value = ["en", "ja"]
        model = MagicMock()
        item = {
            "id": "series-1",
            "type": "series",
            "providerIds": [
                {"provider": "tvdb", "id": "100"},
                {"provider": "tmdb", "id": "200"},
            ],
        }
        with (
            patch.object(library_routes, "_entity", return_value=item),
            patch.object(library_routes, "MetadataService") as service_type,
            patch.object(
                library_routes, "MetadataIngestService", return_value=ingest
            ) as ingest_type,
            patch("app.catalog_read_model.CatalogReadModel", return_value=model),
        ):
            result = library_routes._refresh_item_metadata_sync("series-1")

        ingest_type.assert_called_once_with(
            service_type.return_value, background_assets=False
        )
        self.assertEqual(ingest.ingest_locales.call_count, 2)
        ingest.ingest_locales.assert_any_call(
            "tvdb", "series", "100", ["en", "ja"], force=True
        )
        ingest.ingest_locales.assert_any_call(
            "tmdb", "series", "200", ["en", "ja"], force=True
        )
        model.refresh_roots.assert_called_once_with(["series-1"])
        self.assertEqual(result["state"], "completed")

    def test_item_metadata_refresh_endpoint_runs_off_event_loop(self):
        expected = {"itemId": "movie-1", "state": "completed"}
        with (
            patch.object(library_routes, "require_admin"),
            patch.object(
                library_routes, "_refresh_item_metadata_sync", return_value=expected
            ) as refresh,
        ):
            result = asyncio.run(
                library_routes.refresh_item_metadata(
                    "movie-1", Username="admin", TOKEN="token"
                )
            )

        self.assertEqual(result, expected)
        refresh.assert_called_once_with("movie-1")

    def test_local_image_names_are_matched_to_canonical_artwork_types(self):
        self.assertTrue(
            library_routes._local_image_for_type("Series/poster.jpg", "Primary")
        )
        self.assertTrue(
            library_routes._local_image_for_type("Series/fanart.jpg", "Backdrop")
        )
        self.assertFalse(
            library_routes._local_image_for_type("Series/fanart.jpg", "Primary")
        )
        self.assertFalse(
            library_routes._local_image_for_type("Series/poster.jpg", "Backdrop")
        )

    def test_primary_image_candidates_include_series_for_seasons_and_episodes(self):
        values = {
            "episode": {
                "id": "episode-1",
                "type": "episode",
                "parentId": "season-1",
            },
            "season-1": {
                "id": "season-1",
                "type": "season",
                "parentId": "series-1",
            },
            "series-1": {"id": "series-1", "type": "series", "parentId": None},
        }
        with patch.object(library_routes, "_entity", side_effect=values.__getitem__):
            self.assertEqual(
                [
                    value["id"]
                    for value in library_routes._image_entities(
                        values["episode"], "Primary"
                    )
                ],
                ["episode-1", "season-1", "series-1"],
            )
            self.assertEqual(
                [
                    value["id"]
                    for value in library_routes._image_entities(
                        values["episode"], "Backdrop"
                    )
                ],
                ["episode-1"],
            )

    def test_provider_match_rejects_ambiguous_candidates(self):
        with self.assertRaises(ProviderError):
            _select_match(
                [
                    {"providerId": "1", "title": "Example Show", "year": "2020"},
                    {"providerId": "2", "title": "Example Show", "year": "2020"},
                ],
                "Example Show",
                "2020",
            )

    def test_tmdb_normalization_keeps_common_fields_and_external_ids(self):
        value = TMDBClient({}, "api_key").normalize(
            "series",
            "10",
            {
                "name": "Example",
                "first_air_date": "2020-01-02",
                "overview": "Overview",
                "genres": [{"name": "Drama"}],
                "original_language": "ja",
                "vote_average": 8.2,
                "external_ids": {"tvdb_id": 42, "imdb_id": "tt1"},
            },
        )
        self.assertEqual(value["year"], "2020")
        self.assertEqual(value["tags"], ["Drama"])
        self.assertEqual(value["communityRating"], 8.2)
        self.assertEqual(value["images"], [])
        self.assertEqual({item["provider"] for item in value["ids"]}, {"tvdb", "imdb"})

    def test_tvdb_normalization_maps_themoviedb_remote_id_for_series(self):
        value = TVDBClient({"apiKey": "test"}).normalize(
            "series",
            "88651",
            {
                "data": {
                    "name": "07-Ghost",
                    "remoteIds": [
                        {"sourceName": "TheMovieDB.com", "id": "21855"},
                        {"sourceName": "IMDB", "id": "tt1424033"},
                    ],
                }
            },
        )
        self.assertEqual(
            value["ids"],
            [
                {"provider": "tmdb", "identifierType": "series", "id": "21855"},
                {"provider": "imdb", "identifierType": "imdb", "id": "tt1424033"},
            ],
        )

    def test_tvdb_details_requests_english_translation_explicitly(self):
        client = TVDBClient({"apiKey": "test"})
        TVDBClient._language_codes_loaded = False
        TVDBClient._language_catalog = ProviderLanguageCatalog()
        with patch.object(
            client,
            "_request",
            return_value={"data": [{"id": "eng", "shortCode": "en"}]},
        ) as request:
            self.assertEqual(client._language_code("en"), "eng")
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": {"name": "Default title"}},
                {"data": {"name": "English title"}},
            ],
        ) as request:
            payload = client.details("series", "436603", "en")
        self.assertEqual(payload["translation"], {"name": "English title"})
        self.assertEqual(
            request.call_args_list[1].args[0], "/series/436603/translations/eng"
        )

    def test_tvdb_missing_translation_keeps_extended_metadata(self):
        client = TVDBClient({"apiKey": "test"})
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": {"name": "Default title"}},
                ProviderError("404 Not Found"),
            ],
        ) as request:
            payload = client.details("season", "489132", "en")
        self.assertNotIn("translation", payload)
        self.assertEqual(payload["data"]["name"], "Default title")
        self.assertEqual(request.call_count, 2)

    def test_tvdb_fetches_extended_metadata_once_for_all_locales(self):
        client = TVDBClient({"apiKey": "test"})

        def request(path, params=None):
            if path == "/episodes/10/extended":
                return {"data": {"name": "Default title"}}
            language = path.rsplit("/", 1)[-1]
            return {"data": {"name": f"Title {language}"}}

        with (
            patch.object(client, "_language_code", side_effect=lambda value: value),
            patch.object(client, "_request", side_effect=request) as provider_request,
        ):
            payloads = client.details_all_locales(
                "episode", "10", ["en", "ja", "zh-TW"]
            )

        paths = [call.args[0] for call in provider_request.call_args_list]
        self.assertEqual(paths.count("/episodes/10/extended"), 1)
        self.assertEqual(
            {payloads[locale]["translation"]["name"] for locale in payloads},
            {"Title en", "Title ja", "Title zh-TW"},
        )

    def test_tvdb_original_language_is_normalized_to_canonical_locale(self):
        client = TVDBClient({"apiKey": "test"})
        value = client.normalize(
            "series",
            "1",
            {"data": {"name": "Example", "originalLanguage": "eng"}},
        )
        self.assertEqual(value["originalLanguage"], "en")

    def test_tvdb_overview_translation_codes_are_not_used_as_overview(self):
        client = TVDBClient({"apiKey": "test"})
        with patch("app.providers._tvdb_images", return_value=([], [])):
            value = client.normalize(
                "episode",
                "1",
                {
                    "data": {
                        "name": "Example episode",
                        "overview": "eng",
                        "overviewTranslations": ["eng", "jpn"],
                    }
                },
            )
        self.assertIsNone(value["overview"])

    def test_tvdb_normalization_preserves_localized_trailer_url(self):
        client = TVDBClient({"apiKey": "test"})
        with (
            patch.object(
                client,
                "_language_code_for_artwork",
                side_effect=lambda value: {"eng": "en", "jpn": "ja"}.get(value, value),
            ),
            patch("app.providers._tvdb_images", return_value=([], [])),
        ):
            value = client.normalize(
                "series",
                "436603",
                {
                    "data": {
                        "name": "Example",
                        "trailers": [
                            {
                                "name": "Japanese trailer",
                                "url": "https://youtu.be/ja",
                                "language": "jpn",
                            }
                        ],
                    }
                },
            )
        self.assertEqual(value["trailers"][0]["url"], "https://youtu.be/ja")
        self.assertEqual(value["trailers"][0]["language"], "ja")

    def test_tmdb_details_maps_short_locale_to_provider_language(self):
        client = TMDBClient({"value": "test"})
        TMDBClient._language_codes_loaded = False
        TMDBClient._language_catalog = ProviderLanguageCatalog()
        with patch.object(
            client,
            "_request",
            side_effect=[[{"iso_639_1": "ja"}], ["ja-JP"], {"name": "Example"}],
        ) as request:
            client.details("series", "10", "ja")
        self.assertEqual(
            request.call_args_list[2].kwargs["params"]["language"], "ja-JP"
        )
        self.assertEqual(
            request.call_args_list[2].kwargs["params"]["include_image_language"],
            "ja-JP,ja,null",
        )

    def test_tmdb_details_includes_english_and_untranslated_artwork(self):
        client = TMDBClient({"value": "test"})
        TMDBClient._language_codes_loaded = False
        TMDBClient._language_catalog = ProviderLanguageCatalog()
        with patch.object(
            client,
            "_request",
            side_effect=[[{"iso_639_1": "en"}], ["en-US"], {"name": "Example"}],
        ) as request:
            client.details("movie", "10", "en")
        params = request.call_args_list[2].kwargs["params"]
        self.assertEqual(params["language"], "en-US")
        self.assertEqual(params["include_image_language"], "en-US,en,null")

    def test_tmdb_fetches_all_locale_translations_in_one_request(self):
        client = TMDBClient({"value": "test"})
        payload = {
            "title": "Base",
            "translations": {
                "translations": [
                    {
                        "iso_639_1": "en",
                        "iso_3166_1": "US",
                        "data": {"title": "English"},
                    },
                    {
                        "iso_639_1": "ja",
                        "iso_3166_1": "JP",
                        "data": {"title": "Japanese"},
                    },
                ]
            },
        }

        def request(path, params=None):
            if path == "/movie/10":
                return payload
            return {
                "results": [
                    {
                        "id": "ja-video",
                        "site": "YouTube",
                        "key": "ja-video",
                        "iso_639_1": "ja",
                        "iso_3166_1": "JP",
                    }
                ]
            }

        with (
            patch.object(client, "_language_code", side_effect=lambda value: value),
            patch.object(client, "_request", side_effect=request) as provider_request,
        ):
            values = client.details_all_locales("movie", "10", ["en-US", "ja-JP"])

        self.assertEqual(provider_request.call_count, 2)
        self.assertEqual(values["en-US"]["title"], "English")
        self.assertEqual(values["ja-JP"]["title"], "Japanese")
        self.assertEqual(values["ja-JP"]["videos"]["results"][0]["iso_639_1"], "ja")
        self.assertIn(
            "translations",
            provider_request.call_args_list[0].kwargs["params"]["append_to_response"],
        )

    def test_tmdb_tv_details_requests_all_configured_video_languages(self):
        client = TMDBClient({"value": "test"})
        with (
            patch.object(client, "_language_code", side_effect=lambda value: value),
            patch.object(client, "_request", return_value={}) as provider_request,
        ):
            client.details_all_locales("series", "10", ["en-US", "ja-JP"])

        params = provider_request.call_args.kwargs["params"]
        self.assertEqual(params["include_video_language"], "en,ja")

    def test_tmdb_missing_season_or_episode_layout_is_an_empty_result(self):
        client = TMDBClient({"value": "test"})

        for entity_type, provider_id in (
            ("season", "10:2"),
            ("episode", "10:2:1"),
        ):
            with (
                self.subTest(entity_type=entity_type),
                patch.object(client, "_language_code", side_effect=lambda value: value),
                patch.object(
                    client,
                    "_request",
                    side_effect=ProviderNotFoundError("provider resource not found"),
                ),
            ):
                self.assertEqual(
                    client.details_all_locales(
                        entity_type, provider_id, ["en-US", "ja-JP"]
                    ),
                    {"en-US": {}, "ja-JP": {}},
                )

    def test_tmdb_missing_series_remains_a_provider_failure(self):
        client = TMDBClient({"value": "test"})

        with (
            patch.object(client, "_language_code", return_value="en-US"),
            patch.object(
                client,
                "_request",
                side_effect=ProviderNotFoundError("provider resource not found"),
            ),
            self.assertRaises(ProviderNotFoundError),
        ):
            client.details_all_locales("series", "10", ["en-US"])

    def test_provider_language_catalogs_pass_unknown_locale_through(self):
        tvdb = TVDBClient({"apiKey": "test"})
        TVDBClient._language_codes_loaded = False
        TVDBClient._language_catalog = ProviderLanguageCatalog()
        with patch.object(tvdb, "_request", return_value={"data": []}):
            self.assertEqual(tvdb._language_code("xx-YY"), "xx-YY")

        tmdb = TMDBClient({"value": "test"})
        TMDBClient._language_codes_loaded = False
        TMDBClient._language_catalog = ProviderLanguageCatalog()
        with patch.object(tmdb, "_request", side_effect=[[], []]):
            self.assertEqual(tmdb._language_code("xx-YY"), "xx-YY")

    def test_provider_language_catalog_failures_pass_requested_locale_through(self):
        tvdb = TVDBClient({"apiKey": "test"})
        TVDBClient._language_codes_loaded = False
        TVDBClient._language_catalog = ProviderLanguageCatalog()
        with patch.object(
            tvdb, "_request", side_effect=ProviderError("catalog unavailable")
        ):
            self.assertEqual(tvdb._language_code("ga-IE"), "ga-IE")

        tmdb = TMDBClient({"value": "test"})
        TMDBClient._language_codes_loaded = False
        TMDBClient._language_catalog = ProviderLanguageCatalog()
        with patch.object(
            tmdb, "_request", side_effect=ProviderError("catalog unavailable")
        ):
            self.assertEqual(tmdb._language_code("ga-IE"), "ga-IE")

    def test_tvdb_null_short_codes_map_iso_and_regional_languages_bidirectionally(self):
        client = TVDBClient({"apiKey": "test"})
        TVDBClient._language_codes_loaded = False
        TVDBClient._language_catalog = ProviderLanguageCatalog()
        values = [
            {"id": "eng", "name": "English", "shortCode": None},
            {"id": "jpn", "name": "Japanese", "shortCode": None},
            {"id": "por", "name": "Portuguese - Portugal", "shortCode": None},
            {"id": "pt", "name": "Portuguese - Brazil", "shortCode": None},
            {"id": "zho", "name": "Chinese - China", "shortCode": None},
            {"id": "zhtw", "name": "Chinese - Taiwan", "shortCode": None},
            {"id": "yue", "name": "Chinese - Cantonese", "shortCode": None},
        ]
        with patch.object(client, "_request", return_value={"data": values}):
            self.assertEqual(client._language_code("en"), "eng")
        self.assertEqual(client._language_code("ja"), "jpn")
        self.assertEqual(client._language_code("pt-PT"), "por")
        self.assertEqual(client._language_code("pt-BR"), "pt")
        self.assertEqual(client._language_code("zh-CN"), "zho")
        self.assertEqual(client._language_code("zh-TW"), "zhtw")
        self.assertEqual(client._language_code_for_artwork("jpn"), "ja")
        self.assertEqual(client._language_code_for_artwork("zhtw"), "zh-TW")
        self.assertEqual(client._language_code_for_artwork("yue"), "yue")

    def test_tvdb_numeric_artwork_catalog_maps_posters_and_languages(self):
        client = TVDBClient({"apiKey": "test"})
        TVDBClient._language_codes_loaded = False
        TVDBClient._language_catalog = ProviderLanguageCatalog()
        TVDBClient._artwork_types_loaded = False
        TVDBClient._artwork_types = {}

        def request(path, params=None):
            if path == "/artwork/types":
                return {
                    "data": [
                        {
                            "id": 2,
                            "name": "Poster",
                            "recordType": "series",
                            "slug": "posters",
                        },
                        {
                            "id": 7,
                            "name": "Poster",
                            "recordType": "season",
                            "slug": "posters",
                        },
                    ]
                }
            if path == "/languages":
                return {
                    "data": [
                        {"id": "eng", "name": "English", "shortCode": None},
                        {"id": "jpn", "name": "Japanese", "shortCode": None},
                    ]
                }
            raise AssertionError(path)

        with patch.object(client, "_request", side_effect=request):
            value = client.normalize(
                "series",
                "1",
                {
                    "data": {
                        "name": "Example",
                        "artworks": [
                            {
                                "type": 2,
                                "language": "eng",
                                "image": "english",
                                "score": 1,
                            },
                            {
                                "type": 2,
                                "language": "jpn",
                                "image": "japanese",
                                "score": 1,
                            },
                            {
                                "type": 7,
                                "language": "jpn",
                                "image": "season",
                                "score": 100,
                            },
                        ],
                    }
                },
            )
        self.assertEqual(
            [
                (image["language"], image["providerLanguage"], image["sourceType"])
                for image in value["images"]
            ],
            [("en", "eng", "2"), ("ja", "jpn", "2")],
        )
        self.assertEqual(value["extraImages"][0]["url"], "season")
        self.assertEqual(choose_image(value["images"], "en", PRIMARY)["url"], "english")
        self.assertEqual(
            choose_image(value["images"], "ja", PRIMARY)["url"], "japanese"
        )

    def test_provider_artwork_uses_canonical_categories(self):
        tmdb = TMDBClient({}, "api_key").normalize(
            "episode",
            "10:1:2",
            {
                "name": "Episode",
                "images": {
                    "stills": [{"file_path": "/still.jpg"}],
                    "backdrops": [{"file_path": "/backdrop.jpg"}],
                    "logos": [{"file_path": "/logo.png"}],
                },
            },
        )
        self.assertEqual(
            {value["type"] for value in tmdb["images"]}, {PRIMARY, "Backdrop", "Logo"}
        )
        tvdb, extras = _tvdb_images(
            "episode",
            {
                "artworks": [
                    {"type": "banner", "image": "banner"},
                    {"type": "episode still", "image": "still"},
                    {"type": "unknown", "image": "other", "width": 100, "height": 100},
                ]
            },
        )
        self.assertEqual({value["type"] for value in tvdb}, {BANNER, PRIMARY})
        self.assertEqual(extras[0]["sourceType"], "unknown")

        season, season_extras = _tvdb_images(
            "season",
            {
                "artworks": [
                    {"type": "episode still", "image": "still"},
                    {"type": "background", "image": "backdrop"},
                ]
            },
        )
        self.assertEqual({value["type"] for value in season}, {"Backdrop"})
        self.assertEqual(season_extras[0]["sourceType"], "episode still")

        season_with_poster, _ = _tvdb_images(
            "season", {"artworks": [{"type": "poster", "image": "poster"}]}
        )
        self.assertEqual({value["type"] for value in season_with_poster}, {PRIMARY})

        tmdb_season = TMDBClient({}, "api_key").normalize(
            "season",
            "10:1",
            {
                "name": "Season",
                "images": {
                    "stills": [{"file_path": "/still.jpg"}],
                    "backdrops": [{"file_path": "/backdrop.jpg"}],
                },
            },
        )
        self.assertEqual(
            {value["type"] for value in tmdb_season["images"]}, {"Backdrop"}
        )
        tmdb_episode = TMDBClient({}, "api_key").normalize(
            "episode", "10:1:2", {"name": "Episode", "still_path": "/still.jpg"}
        )
        self.assertEqual({value["type"] for value in tmdb_episode["images"]}, {PRIMARY})

    def test_primary_provider_flags_follow_entity_type(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute(
            "CREATE TABLE library_entities (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, match_status TEXT DEFAULT 'unresolved', match_confidence REAL, match_method TEXT, updated_at TEXT)"
        )
        db.execute(
            "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)"
        )
        store = LibraryStore.__new__(LibraryStore)
        store.db = db
        scanner = LibraryScanner(store)
        for entity_id, entity_type in (
            ("series-1", "series"),
            ("movie-1", "movie"),
            ("season-1", "season"),
        ):
            db.execute(
                "INSERT INTO library_entities(id,entity_type) VALUES(?,?)",
                (entity_id, entity_type),
            )
        scanner._ids(
            "series-1",
            [("tmdb", "series", "tmdb-series"), ("tvdb", "series", "tvdb-series")],
        )
        scanner._ids(
            "movie-1",
            [("tvdb", "movie", "tvdb-movie"), ("tmdb", "movie", "tmdb-movie")],
        )
        scanner._ids(
            "season-1",
            [("tmdb", "season", "tmdb-season"), ("tvdb", "season", "tvdb-season")],
        )
        self.assertEqual(
            db.execute(
                "SELECT provider,is_primary FROM entity_provider_ids WHERE entity_id='series-1' ORDER BY provider"
            ),
            [("tmdb", 0), ("tvdb", 1)],
        )
        self.assertEqual(
            db.execute(
                "SELECT provider,is_primary FROM entity_provider_ids WHERE entity_id='movie-1' ORDER BY provider"
            ),
            [("tmdb", 1), ("tvdb", 0)],
        )
        self.assertEqual(
            db.execute(
                "SELECT provider,is_primary FROM entity_provider_ids WHERE entity_id='season-1' ORDER BY provider"
            ),
            [("tmdb", 0), ("tvdb", 1)],
        )
        db.close()

    def test_tvdb_season_details_attach_exact_episode_ids(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute(
            "CREATE TABLE library_entities (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, parent_id TEXT, season_number INTEGER, episode_number INTEGER, relative_path TEXT, match_status TEXT DEFAULT 'unresolved', match_confidence REAL, match_method TEXT, updated_at TEXT)"
        )
        db.execute(
            "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)"
        )
        store = LibraryStore.__new__(LibraryStore)
        store.db = db
        scanner = LibraryScanner(store)
        db.execute(
            "INSERT INTO library_entities(id,entity_type,relative_path) VALUES('series-1','series','Example')"
        )
        db.execute(
            "INSERT INTO library_entities(id,entity_type,parent_id,season_number,relative_path) VALUES('season-1','season','series-1',1,'Season 1')"
        )
        db.execute(
            "INSERT INTO library_entities(id,entity_type,parent_id,season_number,episode_number,relative_path) VALUES('episode-1','episode','season-1',1,2,'Episode 2')"
        )
        scanner._ids(
            "season-1",
            [("tvdb", "season", "season-tvdb-1"), ("tmdb", "season", "series-tmdb:1")],
        )

        class FakeService:
            def fetch(self, provider, entity_type, provider_id, locale, force=False):
                self.called = (provider, entity_type, provider_id, locale, force)
                return {
                    "provider": "tvdb",
                    "providerId": provider_id,
                    "children": [
                        {
                            "type": "episode",
                            "season": 1,
                            "episode": 2,
                            "id": "episode-tvdb-2",
                        }
                    ],
                    "ids": [],
                    "images": [],
                }

        scanner._derive_tvdb_episode_ids("series-1", FakeService())
        self.assertEqual(
            db.execute(
                "SELECT provider,provider_id,is_primary FROM entity_provider_ids WHERE entity_id='episode-1'"
            ),
            [("tvdb", "episode-tvdb-2", 1)],
        )
        db.close()

    def test_tvdb_children_preserve_specials_season_zero(self):
        self.assertEqual(
            _tvdb_children(
                {
                    "seasons": [{"seasonNumber": 0, "number": 99, "id": 123}],
                    "episodes": [{"seasonNumber": 0, "number": 1, "id": 456}],
                }
            ),
            [
                {"type": "season", "season": 0, "id": "123"},
                {"type": "episode", "season": 0, "episode": 1, "id": "456"},
            ],
        )

    def test_tvdb_series_hierarchy_follows_pagination(self):
        client = TVDBClient({"apiKey": "test"})
        responses = [
            {
                "data": {
                    "seasons": [{"id": 1, "number": 0, "type": {"type": "official"}}]
                },
                "links": {},
            },
            {
                "data": {"episodes": [{"id": 10, "seasonNumber": 0, "number": 12345}]},
                "links": {"next": 1},
            },
            {
                "data": {"episodes": [{"id": 11, "seasonNumber": 101, "number": 289}]},
                "links": {"next": None},
            },
        ]
        with patch.object(client, "_request", side_effect=responses) as request:
            value = client.series_hierarchy("series-1")
        self.assertEqual([item["id"] for item in value["episodes"]], [10, 11])
        self.assertEqual(request.call_args_list[1].kwargs["params"], {"page": 0})
        self.assertEqual(request.call_args_list[2].kwargs["params"], {"page": 1})

    def test_tvdb_series_aggregation_fetches_series_in_requested_locale(self):
        class FakeClient:
            @staticmethod
            def series_hierarchy(provider_id):
                return {"extended": {"data": {"seasons": []}}, "episodes": []}

        service = MetadataService.__new__(MetadataService)
        service.cache = MagicMock()
        service.cache.get.return_value = {"title": "Legacy default-language title"}
        service.client = MagicMock(return_value=FakeClient())
        service.fetch = MagicMock(return_value={"title": "English title", "images": []})

        records = service.aggregate_series("tvdb", "series-1", "en")

        self.assertEqual(records["series"]["title"], "English title")
        service.fetch.assert_called_once_with(
            "tvdb", "series", "series-1", "en", force=True
        )

    def test_tvdb_series_aggregation_maps_children_without_fetching_metadata(self):
        class FakeClient:
            @staticmethod
            def series_hierarchy(provider_id):
                return {
                    "extended": {
                        "data": {
                            "seasons": [{"id": 2, "number": 1, "type": "official"}]
                        }
                    },
                    "episodes": [{"id": 3, "seasonNumber": 1, "number": 4}],
                }

            @staticmethod
            def normalize(entity_type, provider_id, payload):
                return {"title": "Default hierarchy title", "providerId": provider_id}

        service = MetadataService.__new__(MetadataService)
        service.cache = MagicMock()
        service.cache.get.return_value = {"title": "Legacy default-language title"}
        service.client = MagicMock(return_value=FakeClient())
        service.fetch = MagicMock(return_value={"title": "English title", "images": []})

        records = service.aggregate_series("tvdb", "series-1", "en")

        self.assertEqual(records["seasons"][0]["providerId"], "2")
        self.assertEqual(records["episodes"][0]["providerId"], "3")
        service.fetch.assert_called_once_with(
            "tvdb", "series", "series-1", "en", force=True
        )

    def test_series_aggregation_maps_children_without_name_resolution(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute(
            "CREATE TABLE library_entities (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, parent_id TEXT, season_number INTEGER, episode_number INTEGER, relative_path TEXT, match_status TEXT DEFAULT 'unresolved', match_confidence REAL, match_method TEXT, updated_at TEXT)"
        )
        db.execute(
            "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)"
        )
        store = LibraryStore.__new__(LibraryStore)
        store.db = db
        scanner = LibraryScanner(store)
        db.execute(
            "INSERT INTO library_entities(id,entity_type) VALUES('series-1','series')"
        )
        db.execute(
            "INSERT INTO library_entities(id,entity_type,parent_id,season_number,relative_path) VALUES('season-0','season','series-1',0,'Specials')"
        )
        db.execute(
            "INSERT INTO library_entities(id,entity_type,parent_id,season_number,episode_number,relative_path) VALUES('episode-1','episode','season-0',0,12345,'S00E12345')"
        )
        scanner._ids(
            "series-1",
            [("tvdb", "series", "tvdb-series"), ("tmdb", "series", "tmdb-series")],
        )

        class FakeService:
            def __init__(self):
                self.resolve_called = False

            @staticmethod
            def aggregate_series(provider, provider_id, locale):
                if provider == "tvdb":
                    return {
                        "seasons": [
                            {
                                "providerId": "tvdb-season-0",
                                "seasonNumber": 0,
                                "ids": [],
                                "children": [],
                                "images": [],
                            }
                        ],
                        "episodes": [
                            {
                                "providerId": "tvdb-episode-12345",
                                "seasonNumber": 0,
                                "episodeNumber": 12345,
                                "ids": [],
                                "children": [],
                                "images": [],
                            }
                        ],
                    }
                return {
                    "seasons": [
                        {
                            "providerId": "tmdb-series:0",
                            "seasonNumber": 0,
                            "ids": [],
                            "children": [],
                            "images": [],
                        }
                    ],
                    "episodes": [
                        {
                            "providerId": "tmdb-series:0:12345",
                            "seasonNumber": 0,
                            "episodeNumber": 12345,
                            "ids": [],
                            "children": [],
                            "images": [],
                        }
                    ],
                }

            def resolve_inventory_entity(self, *args, **kwargs):
                self.resolve_called = True
                raise AssertionError("child names must not be resolved")

        service = FakeService()
        scanner._aggregate_series_children("series-1", service)
        self.assertFalse(service.resolve_called)
        self.assertEqual(
            db.execute(
                "SELECT provider,provider_id,is_primary FROM entity_provider_ids WHERE entity_id='season-0' ORDER BY provider"
            ),
            [("tmdb", "tmdb-series:0", 0), ("tvdb", "tvdb-season-0", 1)],
        )
        self.assertEqual(
            db.execute(
                "SELECT provider,provider_id,is_primary FROM entity_provider_ids WHERE entity_id='episode-1' ORDER BY provider"
            ),
            [("tmdb", "tmdb-series:0:12345", 0), ("tvdb", "tvdb-episode-12345", 1)],
        )
        db.close()

    def test_provider_child_ids_attach_specials_season_zero(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute(
            "CREATE TABLE library_entities (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, parent_id TEXT, season_number INTEGER, episode_number INTEGER, relative_path TEXT, match_status TEXT DEFAULT 'unresolved', match_confidence REAL, match_method TEXT, updated_at TEXT)"
        )
        db.execute(
            "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)"
        )
        store = LibraryStore.__new__(LibraryStore)
        store.db = db
        scanner = LibraryScanner(store)
        db.execute(
            "INSERT INTO library_entities(id,entity_type) VALUES('series-1','series')"
        )
        db.execute(
            "INSERT INTO library_entities(id,entity_type,parent_id,season_number,relative_path) VALUES('season-0','season','series-1',0,'Specials')"
        )
        scanner._derive_provider_child_ids(
            "series-1",
            {
                "provider": "tvdb",
                "children": [{"type": "season", "season": 0, "id": "season-tvdb-0"}],
            },
        )
        self.assertEqual(
            db.execute(
                "SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id='season-0'"
            ),
            [("tvdb", "season-tvdb-0")],
        )
        db.close()

    def test_tvdb_episode_resolution_missing_id_leaves_episode_unresolved(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute(
            "CREATE TABLE library_entities (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, parent_id TEXT, season_number INTEGER, episode_number INTEGER, relative_path TEXT, match_status TEXT DEFAULT 'unresolved', match_confidence REAL, match_method TEXT, updated_at TEXT)"
        )
        db.execute(
            "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)"
        )
        store = LibraryStore.__new__(LibraryStore)
        store.db = db
        scanner = LibraryScanner(store)
        db.execute(
            "INSERT INTO library_entities(id,entity_type,relative_path) VALUES('series-1','series','Example')"
        )
        db.execute(
            "INSERT INTO library_entities(id,entity_type,parent_id,season_number,relative_path) VALUES('season-1','season','series-1',1,'Season 1')"
        )
        db.execute(
            "INSERT INTO library_entities(id,entity_type,parent_id,season_number,episode_number,relative_path) VALUES('episode-1','episode','season-1',1,2,'Episode 2')"
        )
        scanner._ids("episode-1", [("tmdb", "episode", "tmdb-series:1:2")])
        scanner._ids("season-1", [("tvdb", "season", "season-tvdb-1")])
        scanner._derive_tvdb_episode_ids(
            "series-1",
            type(
                "FakeService",
                (),
                {
                    "fetch": lambda *_args, **_kwargs: {
                        "children": [],
                        "ids": [],
                        "images": [],
                    }
                },
            )(),
        )
        self.assertEqual(
            db.execute(
                "SELECT provider_id FROM entity_provider_ids WHERE entity_id='episode-1'"
            ),
            [],
        )
        db.close()

    def test_detail_payload_orders_and_labels_tvdb_as_series_primary(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute(
            "CREATE TABLE library_entities (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)"
        )
        db.execute(
            "INSERT INTO library_entities(id,entity_type) VALUES('series-1','series')"
        )
        db.execute(
            "INSERT INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES('series-1','tmdb','series','217512',1)"
        )
        db.execute(
            "INSERT INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES('series-1','tvdb','series','429055',1)"
        )
        original = library_routes.store.db
        library_routes.store.db = db
        try:
            value = library_routes._entity_ids("series-1")
        finally:
            library_routes.store.db = original
            db.close()
        self.assertEqual(
            value[0],
            {
                "provider": "tvdb",
                "type": "series",
                "id": "429055",
                "primary": True,
                "role": "primary",
            },
        )
        self.assertEqual(value[1]["role"], "secondary")

    def test_primary_provider_is_required_but_secondary_provider_is_optional(self):
        service = MetadataService.__new__(MetadataService)
        service.fetch = lambda provider, entity_type, provider_id, locale, force=False: {
            "provider": provider,
            "providerId": provider_id,
            "title": "Example",
            "ids": [{"provider": "tmdb", "id": "secondary-from-tvdb"}],
        }

        class PrimaryOnlyClient:
            def __init__(self, provider):
                self.provider = provider
                self.searches = []

            def search(self, entity_type, query, *args):
                self.searches.append((entity_type, query, args))
                if self.provider != "tvdb":
                    raise ProviderError("primary provider unavailable")
                return [{"providerId": "primary-1", "title": "Example", "year": "2020"}]

        clients = {}

        def client(provider):
            clients[provider] = PrimaryOnlyClient(provider)
            return clients[provider]

        service.client = client
        series = service.resolve_inventory_entity("series", "Example", "2020")
        self.assertEqual(
            series["providerIds"],
            [
                {"provider": "tvdb", "id": "primary-1"},
                {"provider": "tmdb", "id": "secondary-from-tvdb"},
            ],
        )

        with self.assertRaises(ProviderError):
            service.resolve_inventory_entity("movie", "Example", "2020")

    def test_movie_secondary_ids_come_from_tmdb_external_ids_without_tvdb_search(self):
        service = MetadataService.__new__(MetadataService)

        class FakeClient:
            def __init__(self, provider):
                self.provider = provider

            def search(self, entity_type, query, *args):
                if self.provider != "tmdb":
                    raise AssertionError("secondary provider must not be searched")
                return [{"providerId": "movie-1", "title": "Example", "year": "2020"}]

        service.client = FakeClient
        service.fetch = lambda provider, entity_type, provider_id, locale, force=False: {
            "provider": "tmdb",
            "providerId": provider_id,
            "title": "Example",
            "ids": [
                {"provider": "tvdb", "id": "tvdb-from-tmdb"},
                {"provider": "imdb", "id": "tt-from-tmdb"},
            ],
        }

        result = service.resolve_inventory_entity("movie", "Example", "2020")

        self.assertEqual(
            result["providerIds"],
            [
                {"provider": "tmdb", "id": "movie-1"},
                {"provider": "tvdb", "id": "tvdb-from-tmdb"},
                {"provider": "imdb", "id": "tt-from-tmdb"},
            ],
        )


class LibraryJobControlTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute(
            "CREATE TABLE libraries (id TEXT PRIMARY KEY, name TEXT, type TEXT, directory TEXT, watch_enabled INTEGER, scan_interval_minutes INTEGER, scan_state TEXT, scan_error TEXT, last_scan_started_at TEXT, last_scan_finished_at TEXT, created_at TEXT, updated_at TEXT)"
        )
        self.db.execute(
            "CREATE TABLE library_jobs (id TEXT PRIMARY KEY, library_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', progress_current INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0, message TEXT, error TEXT, error_details TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)"
        )
        self.db.execute(
            "INSERT INTO libraries VALUES('library-1','Library','movies','C:/tmp',1,1440,'ready',NULL,NULL,NULL,'before','before')"
        )
        store = LibraryStore.__new__(LibraryStore)
        store.db = self.db
        self.runtime = LibraryRuntime.__new__(LibraryRuntime)
        self.runtime.store = store
        self.runtime.condition = threading.Condition()
        self.runtime._active_lock = threading.RLock()
        self.runtime._cancel_events = {}
        self.runtime._active_jobs = set()
        self.runtime._reconcile_due = {}
        self.runtime._reconcile_targets = {}
        self.runtime._job_targets = {}
        self.runtime._root_locks = {}
        self.runtime._root_locks_guard = threading.RLock()
        self.runtime._job_target_revisions = {}
        self.runtime._reconcile_state_lock = threading.RLock()
        self.runtime._reconcile_target_cache = {}
        self.runtime._reconcile_cache_loaded = set()
        self.runtime._reconcile_pending = {}
        self.runtime._reconcile_table_available = None
        self.runtime._reconcile_last_flush = 0.0

    def tearDown(self):
        self.db.close()

    def test_scan_and_reconcile_use_independent_library_lanes(self):
        scan = self.runtime.enqueue("library-1", "scan")
        reconcile = self.runtime.enqueue("library-1", "reconcile")

        self.assertNotEqual(scan["id"], reconcile["id"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM library_jobs")[0][0], 2)

    def test_queued_library_task_can_be_terminated(self):
        job = self.runtime.enqueue("library-1", "scan")

        terminated = self.runtime.terminate(job["id"])

        self.assertEqual(terminated["state"], "terminated")
        self.assertIsNotNone(terminated["finishedAt"])

    def test_deleted_library_cannot_enqueue_reconcile_job(self):
        self.db.execute("DELETE FROM libraries WHERE id='library-1'")

        self.assertIsNone(self.runtime.enqueue("library-1", "reconcile"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM library_jobs")[0][0], 0)

    def test_restart_requeues_newest_active_job_and_resets_transient_state(self):
        running = self.runtime.enqueue("library-1", "scan")
        queued = self.runtime.enqueue("library-1", "reconcile")
        self.assertNotEqual(running["id"], queued["id"])
        self.db.execute(
            "UPDATE library_jobs SET state='running',progress_current=4,progress_total=10,started_at='before',finished_at='old',message='Working' WHERE id=?",
            (running["id"],),
        )

        self.runtime._recover_active_jobs()

        self.assertEqual(
            self.db.execute(
                "SELECT state FROM library_jobs WHERE id=?", (running["id"],)
            )[0][0],
            "queued",
        )
        self.assertEqual(
            self.db.execute(
                "SELECT progress_current,progress_total,started_at,finished_at,message FROM library_jobs WHERE id=?",
                (running["id"],),
            )[0],
            (0, 0, None, None, "Queued again after Orchestrator restart"),
        )
        self.assertEqual(
            self.db.execute("SELECT scan_state FROM libraries WHERE id='library-1'")[0][
                0
            ],
            "idle",
        )
        self.assertEqual(
            self.db.execute(
                "SELECT state FROM library_jobs WHERE id=?", (queued["id"],)
            )[0][0],
            "queued",
        )

    def test_restart_terminates_explicitly_terminating_jobs(self):
        job = self.runtime.enqueue("library-1", "scan")
        self.db.execute(
            "UPDATE library_jobs SET state='terminating',message='Termination requested' WHERE id=?",
            (job["id"],),
        )

        self.runtime._recover_active_jobs()

        state, message = self.db.execute(
            "SELECT state,message FROM library_jobs WHERE id=?", (job["id"],)
        )[0]
        self.assertEqual(state, "terminated")
        self.assertEqual(message, "Terminated during Orchestrator restart")

    def test_restart_keeps_only_newest_non_terminating_duplicate(self):
        oldest = self.runtime.enqueue("library-1", "scan")
        self.db.execute(
            "UPDATE library_jobs SET created_at='2020-01-01' WHERE id=?",
            (oldest["id"],),
        )
        newest = self.runtime.store.create_job("library-1", "scan")
        self.db.execute(
            "UPDATE library_jobs SET state='running' WHERE id=?", (newest["id"],)
        )

        self.runtime._recover_active_jobs()

        self.assertEqual(
            self.db.execute(
                "SELECT state FROM library_jobs WHERE id=?", (newest["id"],)
            )[0][0],
            "queued",
        )
        self.assertEqual(
            self.db.execute(
                "SELECT state FROM library_jobs WHERE id=?", (oldest["id"],)
            )[0][0],
            "terminated",
        )

    def test_requeued_job_executes_using_the_same_job_id(self):
        job = self.runtime.enqueue("library-1", "scan")
        self.runtime._recover_active_jobs()
        self.runtime._cancel_events[job["id"]] = threading.Event()

        with patch("app.library.LibraryScanner.scan") as scan:
            self.runtime._execute_job(job["id"], "library-1", "scan")

        scan.assert_called_once_with(
            "library-1", job["id"], unittest.mock.ANY, targets=None
        )

    def test_watcher_reconcile_scopes_move_to_top_level_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.db.execute(
                "UPDATE libraries SET directory=? WHERE id='library-1'", (str(root),)
            )

            self.runtime.request_reconcile(
                "library-1",
                str(root / "Original" / "Season 1" / "Episode.mkv"),
                str(root / "Renamed" / "Season 1" / "Episode.mkv"),
            )

            self.assertEqual(
                self.runtime._reconcile_targets["library-1"],
                {"Original", "Renamed"},
            )
            self.assertIn("library-1", self.runtime._reconcile_due)

    def test_durable_reconcile_with_no_due_targets_is_a_noop(self):
        self.db.execute(
            "CREATE TABLE library_reconcile_targets (library_id TEXT NOT NULL, top_level_root TEXT NOT NULL, debounce_until REAL NOT NULL, event_count INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, PRIMARY KEY (library_id,top_level_root))"
        )
        self.db.execute(
            "INSERT INTO library_reconcile_targets VALUES(?,?,?,?,?,?,?)",
            ("library-1", "Show", time.time() + 60, 1, 1, "before", "before"),
        )
        job = self.runtime.enqueue("library-1", "reconcile")
        self.runtime._cancel_events[job["id"]] = threading.Event()
        with patch("app.library.LibraryScanner.scan") as scan:
            self.runtime._execute_job(job["id"], "library-1", "reconcile")
        scan.assert_not_called()
        self.assertEqual(
            self.db.execute(
                "SELECT state,message FROM library_jobs WHERE id=?", (job["id"],)
            )[0],
            ("completed", "No due watcher targets"),
        )
        self.assertEqual(
            self.db.execute("SELECT top_level_root FROM library_reconcile_targets"),
            [("Show",)],
        )

    def test_durable_reconcile_keeps_newer_revision_after_scan(self):
        self.db.execute(
            "CREATE TABLE library_reconcile_targets (library_id TEXT NOT NULL, top_level_root TEXT NOT NULL, debounce_until REAL NOT NULL, event_count INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, PRIMARY KEY (library_id,top_level_root))"
        )
        self.db.execute(
            "INSERT INTO library_reconcile_targets VALUES(?,?,?,?,?,?,?)",
            ("library-1", "Show", time.time() - 1, 1, 1, "before", "before"),
        )
        job = self.runtime.enqueue("library-1", "reconcile")
        self.runtime._cancel_events[job["id"]] = threading.Event()

        def scan_and_receive_event(*_args, **_kwargs):
            self.db.execute(
                "UPDATE library_reconcile_targets SET revision=2,debounce_until=? WHERE library_id=? AND top_level_root=?",
                (time.time() + 60, "library-1", "Show"),
            )

        with patch(
            "app.library.LibraryScanner.scan", side_effect=scan_and_receive_event
        ) as scan:
            self.runtime._execute_job(job["id"], "library-1", "reconcile")
        scan.assert_called_once()
        self.assertEqual(
            self.db.execute(
                "SELECT revision FROM library_reconcile_targets WHERE library_id='library-1' AND top_level_root='Show'"
            )[0][0],
            2,
        )

    def test_durable_targets_collapse_case_variants_and_keep_latest_spelling(self):
        self.db.execute(
            "CREATE TABLE library_reconcile_targets (library_id TEXT NOT NULL, top_level_root TEXT NOT NULL, debounce_until REAL NOT NULL, event_count INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, PRIMARY KEY (library_id,top_level_root))"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.db.execute(
                "UPDATE libraries SET directory=? WHERE id='library-1'", (str(root),)
            )
            with patch(
                "app.library.os.path.normcase",
                side_effect=lambda value: str(value).lower(),
            ):
                self.runtime.request_reconcile(
                    "library-1", str(root / "Show" / "one.mkv")
                )
                self.runtime.request_reconcile(
                    "library-1", str(root / "show" / "two.mkv")
                )
            self.runtime._flush_reconcile_updates(force=True)
            self.assertEqual(
                self.db.execute(
                    "SELECT top_level_root,event_count,revision FROM library_reconcile_targets"
                ),
                [("show", 2, 2)],
            )

    def test_durable_reconcile_batches_follow_up_events_until_flush(self):
        self.db.execute(
            "CREATE TABLE library_reconcile_targets (library_id TEXT NOT NULL, top_level_root TEXT NOT NULL, debounce_until REAL NOT NULL, event_count INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, PRIMARY KEY (library_id,top_level_root))"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.db.execute(
                "UPDATE libraries SET directory=? WHERE id='library-1'", (str(root),)
            )
            self.runtime.request_reconcile(
                "library-1", str(root / "Show" / "Episode.mkv")
            )
            for index in range(100):
                self.runtime.request_reconcile(
                    "library-1", str(root / "Show" / f"Episode-{index}.mkv")
                )

            self.assertEqual(
                self.db.execute(
                    "SELECT event_count,revision FROM library_reconcile_targets"
                ),
                [(1, 1)],
            )

            self.runtime._flush_reconcile_updates(force=True)

            self.assertEqual(
                self.db.execute(
                    "SELECT event_count,revision FROM library_reconcile_targets"
                ),
                [(101, 101)],
            )

    def test_root_locks_follow_platform_case_semantics(self):
        upper = self.runtime._root_lock("Show", "Show")
        lower = self.runtime._root_lock("Show", "show")
        if os.path.normcase("Show") == os.path.normcase("show"):
            self.assertIs(upper, lower)
        else:
            self.assertIsNot(upper, lower)


if __name__ == "__main__":
    unittest.main()
