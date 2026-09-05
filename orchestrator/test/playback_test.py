import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.playback import PlaybackManager
from fastapi import HTTPException


class PlaybackTest(unittest.TestCase):
    def test_progressive_playlist_is_ready_only_with_playlist_and_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.assertFalse(PlaybackManager._startup_ready(output))
            (output / "master.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
            self.assertFalse(PlaybackManager._startup_ready(output))
            (output / "segment-000000.ts").write_bytes(b"segment")
            self.assertTrue(PlaybackManager._startup_ready(output))

    def test_completed_playlist_is_finalized_as_vod(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "master.m3u8").write_text(
                "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\nsegment-000000.ts\n",
                encoding="utf-8",
            )
            (output / "segment-000000.ts").write_bytes(b"segment")
            PlaybackManager._finalize_playlist("session-1", output)
            playlist = (output / "master.m3u8").read_text(encoding="utf-8")
            self.assertIn("#EXT-X-PLAYLIST-TYPE:VOD", playlist)
            self.assertIn("#EXT-X-ENDLIST", playlist)
            self.assertNotIn("#EXT-X-PLAYLIST-TYPE:EVENT", playlist)
            self.assertFalse((output / "master.m3u8.finalizing").exists())

    def test_public_playlist_owns_full_mpeg_ts_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            PlaybackManager._write_public_playlist(output, 9.0)
            playlist = (output / "master.m3u8").read_text(encoding="utf-8")
            self.assertIn("#EXT-X-PLAYLIST-TYPE:VOD", playlist)
            self.assertIn("#EXT-X-ENDLIST", playlist)
            self.assertIn("segment-000000.ts", playlist)
            self.assertIn("segment-000001.ts", playlist)
            self.assertIn("segment-000002.ts", playlist)
            self.assertNotIn("init.mp4", playlist)
            self.assertNotIn(".m4s", playlist)

    def test_seek_does_not_change_transcode_session_key(self):
        source = {"id": "source-1", "videoCodec": "hevc", "audioCodec": "aac"}
        profile = {"audioStreamId": 1, "maxStreamingBitrate": 2_000_000}
        self.assertEqual(
            PlaybackManager._transcode_key("u", "e", source, profile, 0.0),
            PlaybackManager._transcode_key("u", "e", source, profile, 120.0),
        )

    def test_segment_names_are_strictly_parsed(self):
        self.assertEqual(PlaybackManager._segment_index("segment-000012.ts"), 12)
        self.assertIsNone(PlaybackManager._segment_index("segment-12.ts.tmp"))
        self.assertIsNone(PlaybackManager._segment_index("../segment-000012.ts"))

    def test_segment_worker_starts_on_the_source_timeline_without_per_segment_cutoff(
        self,
    ):
        manager = object.__new__(PlaybackManager)
        manager._segment_seconds = 4.0
        spec = {
            "source": {
                "streams": [
                    {"index": 0, "codec_type": "video", "codec_name": "hevc"},
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "channels": 2,
                    },
                ],
                "width": 1920,
                "height": 1080,
            },
            "profile": {},
            "mode": "video-transcode",
            "executable": "ffmpeg",
            "path": Path("movie.mkv"),
        }
        command = manager._build_ffmpeg_command(spec, Path("worker"), 25)
        self.assertEqual(command[command.index("-ss") + 1], "100.000")
        self.assertNotIn("-to", command)
        self.assertIn("-f", command)
        self.assertEqual(command[command.index("-f") + 1], "hls")
        self.assertNotIn("init.mp4", " ".join(command))
        self.assertNotIn(".m4s", " ".join(command))

    def test_video_transcode_forces_8_bit_main_profile_h264(self):
        manager = object.__new__(PlaybackManager)
        manager._segment_seconds = 4.0
        spec = {
            "source": {
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "pix_fmt": "yuv420p10le",
                        "profile": "Main 10",
                    },
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "channels": 2,
                    },
                ],
                "width": 1920,
                "height": 1080,
            },
            "profile": {},
            "mode": "video-transcode",
            "executable": "ffmpeg",
            "path": Path("movie.mkv"),
        }

        command = manager._build_ffmpeg_command(spec, Path("worker"), 0)

        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(command[command.index("-profile:v") + 1], "main")

    def test_failed_session_output_returns_structured_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = object.__new__(PlaybackManager)
            manager.db = MagicMock()
            manager._cleanup_expired = MagicMock()
            manager.db.execute.side_effect = [
                [
                    (
                        str(Path(directory)),
                        "failed",
                        "FFMPEG_FAILED",
                        '{"stage":"ffmpeg"}',
                    )
                ],
            ]
            with self.assertRaises(HTTPException) as context:
                manager.session_file("user-1", "session-1", "master.m3u8")
            self.assertEqual(context.exception.status_code, 502)
            self.assertEqual(context.exception.detail["sessionId"], "session-1")
            self.assertEqual(context.exception.detail["errorCode"], "FFMPEG_FAILED")

    def test_playback_mode_decision_matrix(self):
        self.assertEqual(
            PlaybackManager._playback_mode(
                {"container": "mp4", "videoCodec": "h264", "audioCodec": "aac"},
                {
                    "containers": ["mp4"],
                    "videoCodecs": ["h264"],
                    "audioCodecs": ["aac"],
                },
            ),
            "direct",
        )

        self.assertEqual(
            PlaybackManager._playback_mode(
                {"container": "mkv", "videoCodec": "h264", "audioCodec": "aac"},
                {
                    "containers": ["mp4"],
                    "videoCodecs": ["h264"],
                    "audioCodecs": ["aac"],
                },
            ),
            "remux",
        )
        self.assertEqual(
            PlaybackManager._playback_mode(
                {"container": "mp4", "videoCodec": "h264", "audioCodec": "ac3"},
                {
                    "containers": ["mp4"],
                    "videoCodecs": ["h264"],
                    "audioCodecs": ["aac"],
                },
            ),
            "audio-transcode",
        )
        self.assertEqual(
            PlaybackManager._playback_mode(
                {
                    "container": "mp4",
                    "videoCodec": "h264",
                    "audioCodec": "aac",
                    "streams": [
                        {
                            "index": 1,
                            "codec_type": "audio",
                            "codec_name": "aac",
                            "channels": 6,
                        }
                    ],
                },
                {
                    "containers": ["mp4"],
                    "videoCodecs": ["h264"],
                    "audioCodecs": ["aac"],
                    "audioStreamId": 1,
                    "maxAudioChannels": 2,
                },
            ),
            "audio-transcode",
        )
        self.assertEqual(
            PlaybackManager._playback_mode(
                {"container": "mp4", "videoCodec": "hevc", "audioCodec": "aac"},
                {
                    "containers": ["mp4"],
                    "videoCodecs": ["h264"],
                    "audioCodecs": ["aac"],
                },
            ),
            "video-transcode",
        )
        self.assertEqual(
            PlaybackManager._playback_mode(
                {
                    "container": "matroska,webm",
                    "videoCodec": "hevc",
                    "audioCodec": "aac",
                },
                {
                    "containers": ["mkv"],
                    "videoCodecs": ["hevc"],
                    "audioCodecs": ["aac"],
                    "maxAudioChannels": 6,
                },
            ),
            "direct",
        )

    def test_audio_playback_mode_decision_matrix(self):
        for container, codec in (
            ("mp3", "mp3"),
            ("flac", "flac"),
            ("ogg", "opus"),
            ("opus", "opus"),
            ("aac", "aac"),
        ):
            with self.subTest(container=container, codec=codec):
                self.assertEqual(
                    PlaybackManager._playback_mode(
                        {"container": container, "audioCodec": codec},
                        {"containers": [container], "audioCodecs": [codec]},
                    ),
                    "direct",
                )
        self.assertEqual(
            PlaybackManager._playback_mode(
                {"container": "flac", "audioCodec": "flac"},
                {"containers": ["mp3"], "audioCodecs": ["flac"]},
            ),
            "remux",
        )
        self.assertEqual(
            PlaybackManager._playback_mode(
                {"container": "m4a", "audioCodec": "alac"},
                {"containers": ["m4a"], "audioCodecs": ["aac"]},
            ),
            "audio-transcode",
        )

    def test_audio_mime_mapping_does_not_use_video_types(self):
        self.assertEqual(
            PlaybackManager._mime({"container": "mp3", "audioCodec": "mp3"}),
            "audio/mpeg",
        )
        self.assertEqual(
            PlaybackManager._mime({"container": "m4a", "audioCodec": "aac"}),
            "audio/mp4",
        )
        self.assertEqual(
            PlaybackManager._mime({"container": "adts", "audioCodec": "aac"}),
            "audio/aac",
        )
        self.assertEqual(
            PlaybackManager._mime({"container": "matroska", "audioCodec": "flac"}),
            "audio/x-matroska",
        )

    def test_audio_only_ffmpeg_command_disables_video(self):
        manager = object.__new__(PlaybackManager)
        manager._segment_seconds = 4.0
        spec = {
            "source": {
                "container": "m4a",
                "audioCodec": "alac",
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "audio",
                        "codec_name": "alac",
                        "channels": 2,
                    }
                ],
            },
            "profile": {},
            "mode": "audio-transcode",
            "executable": "ffmpeg",
            "path": Path("track.m4a"),
        }

        command = manager._build_ffmpeg_command(spec, Path("worker"), 0)

        self.assertIn("-vn", command)
        self.assertNotIn("0:v:0", command)
        self.assertNotIn("-c:v", command)
        self.assertEqual(command[command.index("-c:a") + 1], "aac")

    def test_audio_remux_preserves_original_stream(self):
        manager = object.__new__(PlaybackManager)
        manager._segment_seconds = 4.0
        spec = {
            "source": {
                "container": "flac",
                "audioCodec": "flac",
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "audio",
                        "codec_name": "flac",
                        "channels": 2,
                    }
                ],
            },
            "profile": {"audioCodecs": ["flac"], "containers": ["mp4"]},
            "mode": "remux",
            "executable": "ffmpeg",
            "path": Path("track.flac"),
        }

        command = manager._build_ffmpeg_command(spec, Path("worker"), 0)

        self.assertIn("-vn", command)
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertNotIn("-c:v", command)

    def test_bitrate_limit_requires_video_transcode(self):
        self.assertEqual(
            PlaybackManager._playback_mode(
                {
                    "container": "mkv",
                    "videoCodec": "h264",
                    "audioCodec": "aac",
                    "bitrate": 8_000_000,
                },
                {
                    "containers": ["mp4"],
                    "videoCodecs": ["h264"],
                    "audioCodecs": ["aac"],
                    "maxStreamingBitrate": 2_000_000,
                },
            ),
            "video-transcode",
        )
        self.assertEqual(
            PlaybackManager._playback_mode(
                {
                    "container": "mkv",
                    "videoCodec": "h264",
                    "audioCodec": "ac3",
                    "bitrate": 8_000_000,
                },
                {
                    "containers": ["mp4"],
                    "videoCodecs": ["h264"],
                    "audioCodecs": ["aac"],
                    "maxStreamingBitrate": 2_000_000,
                },
            ),
            "video-transcode",
        )

    def test_empty_capabilities_mean_no_direct_support(self):
        source = {
            "container": "mp4",
            "videoCodec": "h264",
            "audioCodec": "aac",
        }
        self.assertFalse(
            PlaybackManager._direct(
                source,
                {"containers": [], "videoCodecs": [], "audioCodecs": []},
            )
        )

    def test_omitted_capabilities_use_defaults(self):
        source = {
            "container": "mp4",
            "videoCodec": "H264",
            "audioCodec": "AAC",
        }
        self.assertTrue(PlaybackManager._direct(source, {}))

    def test_matroska_and_bitrate_are_normalized(self):
        source = {
            "container": "matroska,webm",
            "videoCodec": "h264",
            "audioCodec": "aac",
            "bitrate": 4_000_000,
        }
        profile = {
            "containers": ["mkv"],
            "videoCodecs": ["h264"],
            "audioCodecs": ["aac"],
            "maxStreamingBitrate": 2_000_000,
        }
        self.assertFalse(PlaybackManager._direct(source, profile))

    def test_codec_aliases_do_not_force_transcoding(self):
        self.assertEqual(
            PlaybackManager._playback_mode(
                {
                    "container": "matroska",
                    "videoCodec": "h265",
                    "audioCodec": "mp4a",
                },
                {
                    "containers": ["mkv"],
                    "videoCodecs": ["hevc"],
                    "audioCodecs": ["aac"],
                    "maxAudioChannels": 6,
                },
            ),
            "direct",
        )

    def test_sources_reads_media_file_id_and_media_role(self):
        manager = object.__new__(PlaybackManager)
        manager.catalog = MagicMock()
        manager.db = MagicMock()
        manager.db.execute.side_effect = [
            [
                (
                    "source-1",
                    "file-1",
                    "mp4",
                    120.0,
                    1_000_000,
                    1920,
                    1080,
                    "h264",
                    "aac",
                    json.dumps({"streams": []}),
                )
            ],
            [],
        ]

        sources = manager.sources("user-1", "entity-1")

        self.assertEqual(sources[0]["mediaFileId"], "file-1")
        media_query = manager.db.execute.call_args_list[0].args[0]
        self.assertIn(
            "role IN ('media','subtitle','lyrics')",
            manager.db.execute.call_args_list[1].args[0],
        )
        self.assertIn("media_sources", media_query)

    def test_sources_add_descriptor_to_sidecar_title(self):
        manager = object.__new__(PlaybackManager)
        manager.catalog = MagicMock()
        manager.db = MagicMock()
        manager.db.execute.side_effect = [
            [
                (
                    "source-1",
                    "file-1",
                    "mp4",
                    120.0,
                    1_000_000,
                    1920,
                    1080,
                    "h264",
                    "aac",
                    json.dumps({"streams": []}),
                )
            ],
            [
                (
                    "file-1",
                    "5 Centimeters per Second/5 Centimeters per Second.mkv",
                    None,
                    "media",
                ),
                (
                    "file-2",
                    "5 Centimeters per Second/5 Centimeters per Second.AI 音声認識.ja.srt",
                    "ja",
                    "subtitle",
                ),
                (
                    "file-3",
                    "5 Centimeters per Second/5 Centimeters per Second.ja.srt",
                    "ja",
                    "subtitle",
                ),
            ],
        ]

        streams = manager.sources("user-1", "entity-1")[0]["streams"]

        self.assertEqual(
            [stream["tags"]["title"] for stream in streams],
            ["AI 音声認識 - Japanese (日本語)", "Japanese (日本語)"],
        )

    def test_sources_do_not_attach_sidecars_from_another_media_file(self):
        manager = object.__new__(PlaybackManager)
        manager.catalog = MagicMock()
        manager.db = MagicMock()
        manager.db.execute.side_effect = [
            [
                (
                    "source-1",
                    "file-1",
                    "mp4",
                    120.0,
                    1_000_000,
                    1920,
                    1080,
                    "h264",
                    "aac",
                    json.dumps({"streams": []}),
                ),
                (
                    "source-2",
                    "file-2",
                    "mp4",
                    120.0,
                    1_000_000,
                    1920,
                    1080,
                    "h264",
                    "aac",
                    json.dumps({"streams": []}),
                ),
            ],
            [
                ("file-1", "Show/Episode.mkv", None, "media"),
                ("file-2", "Show/Episode.alt.mkv", None, "media"),
                ("subtitle-1", "Show/Episode.en.srt", "en", "subtitle"),
                ("subtitle-2", "Show/Episode.alt.ja.srt", "ja", "subtitle"),
            ],
        ]

        sources = manager.sources("user-1", "entity-1")

        self.assertEqual(
            [stream["fileId"] for stream in sources[0]["streams"]], ["subtitle-1"]
        )
        self.assertEqual(
            [stream["fileId"] for stream in sources[1]["streams"]], ["subtitle-2"]
        )

    @patch("app.playback.issue_ticket", return_value="ticket")
    def test_negotiate_returns_readiness_error_when_no_source(self, _ticket):
        manager = object.__new__(PlaybackManager)
        manager.sources = MagicMock(return_value=[])

        with self.assertRaises(Exception) as context:
            manager.negotiate("user-1", "entity-1", {})

        error = context.exception
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.detail["code"], "MEDIA_NOT_READY")

    def test_source_metadata_uses_the_default_source_without_starting_playback(self):
        manager = object.__new__(PlaybackManager)
        manager.sources = MagicMock(
            return_value=[
                {
                    "id": "source-1",
                    "mediaFileId": "file-1",
                    "container": "mkv",
                    "streams": [
                        {
                            "index": 1,
                            "codec_type": "audio",
                            "tags": {"language": "en"},
                        }
                    ],
                    "path": "C:/private/movie.mkv",
                }
            ]
        )

        source = manager.source_metadata("user-1", "entity-1")

        self.assertEqual(
            source,
            {
                "id": "source-1",
                "streams": [
                    {"index": 1, "codec_type": "audio", "tags": {"language": "en"}}
                ],
            },
        )
        manager.sources.assert_called_once_with("user-1", "entity-1")

    def test_source_metadata_reports_unready_media(self):
        manager = object.__new__(PlaybackManager)
        manager.sources = MagicMock(return_value=[])

        with self.assertRaises(HTTPException) as context:
            manager.source_metadata("user-1", "entity-1")

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(context.exception.detail["code"], "MEDIA_NOT_READY")

    @patch("app.playback.issue_ticket", return_value="renewed-ticket")
    def test_refresh_access_renews_a_direct_source_without_creating_a_session(
        self, ticket_issuer
    ):
        manager = object.__new__(PlaybackManager)
        manager.catalog = MagicMock()
        manager.db = MagicMock()
        manager.db.execute.return_value = [(1,)]

        result = manager.refresh_access(
            "user-1", "entity-1", "source-1", None, "auth-session-1"
        )

        self.assertEqual(result["ticket"], "renewed-ticket")
        self.assertEqual(result["expiresIn"], 15 * 60)
        ticket_issuer.assert_called_once_with(
            "user-1",
            "resource",
            15 * 60,
            entity="entity-1",
            sessionId="auth-session-1",
        )
        manager.catalog.require_entity.assert_called_once_with("user-1", "entity-1")

    @patch("app.playback.issue_ticket", return_value="renewed-ticket")
    def test_refresh_access_validates_the_hls_session_source(self, ticket_issuer):
        manager = object.__new__(PlaybackManager)
        manager.catalog = MagicMock()
        manager.db = MagicMock()
        manager.db.execute.return_value = [("source-1", "ready")]

        result = manager.refresh_access(
            "user-1", "entity-1", "source-1", "session-1", "auth-session-1"
        )

        self.assertEqual(result["ticket"], "renewed-ticket")
        self.assertIn("playback_sessions", manager.db.execute.call_args.args[0])
        ticket_issuer.assert_called_once()

    def test_playback_recovery_timeouts_allow_slow_live_workers(self):
        self.assertEqual(PlaybackManager._startup_timeout_seconds, 30.0)
        self.assertEqual(PlaybackManager._segment_wait_timeout_seconds, 45.0)

    @patch("app.playback.issue_ticket", return_value="ticket")
    def test_negotiate_selects_requested_source(self, _ticket):
        manager = object.__new__(PlaybackManager)
        manager.sources = MagicMock(
            return_value=[
                {
                    "id": "source-1",
                    "mediaFileId": "file-1",
                    "container": "mp4",
                    "videoCodec": "h264",
                    "audioCodec": "aac",
                },
                {
                    "id": "source-2",
                    "mediaFileId": "file-2",
                    "container": "webm",
                    "videoCodec": "vp9",
                    "audioCodec": "opus",
                },
            ]
        )
        manager._transcode = MagicMock(
            return_value={"mode": "remux", "url": "playlist"}
        )

        result = manager.negotiate(
            "user-1",
            "entity-1",
            {
                "sourceId": "source-2",
                "containers": ["mp4"],
                "videoCodecs": ["h264"],
                "audioCodecs": ["aac"],
            },
        )

        self.assertEqual(result["sourceId"], "source-2")
        self.assertEqual(manager._transcode.call_args.args[2]["id"], "source-2")

    @patch("app.playback.issue_ticket", return_value="ticket")
    def test_direct_only_does_not_start_transcoding(self, _ticket):
        manager = object.__new__(PlaybackManager)
        manager.sources = MagicMock(
            return_value=[
                {
                    "id": "source-1",
                    "mediaFileId": "file-1",
                    "container": "mkv",
                    "videoCodec": "hevc",
                    "audioCodec": "ac3",
                }
            ]
        )
        manager._transcode = MagicMock()

        with self.assertRaises(Exception) as context:
            manager.negotiate(
                "user-1",
                "entity-1",
                {
                    "directPlayOnly": True,
                    "containers": ["mp4"],
                    "videoCodecs": ["h264"],
                    "audioCodecs": ["aac"],
                },
            )

        self.assertEqual(context.exception.status_code, 409)
        manager._transcode.assert_not_called()

    @patch("app.playback.issue_ticket", return_value="ticket")
    def test_direct_play_does_not_create_a_session(self, _ticket):
        manager = object.__new__(PlaybackManager)
        manager.sources = MagicMock(
            return_value=[
                {
                    "id": "source-1",
                    "mediaFileId": "file-1",
                    "container": "mp4",
                    "videoCodec": "h264",
                    "audioCodec": "aac",
                }
            ]
        )
        manager._transcode = MagicMock()

        result = manager.negotiate(
            "user-1",
            "entity-1",
            {
                "containers": ["mp4"],
                "videoCodecs": ["h264"],
                "audioCodecs": ["aac"],
                "startPositionSeconds": 42,
            },
        )

        self.assertEqual(result["mode"], "direct")
        self.assertEqual(result["startPositionSeconds"], 42.0)
        self.assertIsNone(result.get("sessionId"))
        manager._transcode.assert_not_called()

    @patch("app.playback.ffmpeg_path", return_value=None)
    def test_forced_transcoding_requires_ffmpeg(self, _ffmpeg):
        manager = object.__new__(PlaybackManager)
        manager.sources = MagicMock(
            return_value=[
                {
                    "id": "source-1",
                    "mediaFileId": "file-1",
                    "container": "mp4",
                    "videoCodec": "h264",
                    "audioCodec": "aac",
                }
            ]
        )

        with self.assertRaises(Exception) as context:
            manager.negotiate("user-1", "entity-1", {"forceTranscoding": True})

        self.assertEqual(context.exception.status_code, 503)

    @patch("app.playback.ffmpeg_path", return_value="ffmpeg")
    def test_reuses_active_matching_transcode_session(self, _ffmpeg):
        manager = object.__new__(PlaybackManager)
        process = MagicMock()
        process.poll.return_value = None
        source = {"id": "source-1", "mediaFileId": "file-1"}
        profile = {"maxStreamingBitrate": 2_000_000}
        session_id = "session-1"
        output = Path(tempfile.mkdtemp())
        (output / "master.m3u8").write_text(
            "#EXTM3U\n#EXT-X-TARGETDURATION:4\nsegment-000000.ts\n",
            encoding="utf-8",
        )
        (output / "segment-000000.ts").write_bytes(b"segment")
        manager.db = MagicMock()
        manager.db.execute.return_value = [(str(output), "ready")]
        key = PlaybackManager._transcode_key("user-1", "entity-1", source, profile)
        previous_processes = PlaybackManager._processes
        previous_users = PlaybackManager._users
        previous_keys = PlaybackManager._session_keys
        try:
            PlaybackManager._processes = {session_id: process}
            PlaybackManager._users = {"user-1": {session_id}}
            PlaybackManager._session_keys = {session_id: key}

            result = manager._transcode("user-1", "entity-1", source, "ticket", profile)

            self.assertEqual(result["sessionId"], session_id)
            self.assertEqual(result["mode"], "video-transcode")
        finally:
            PlaybackManager._processes = previous_processes
            PlaybackManager._users = previous_users
            PlaybackManager._session_keys = previous_keys
