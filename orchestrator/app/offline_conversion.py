import hashlib
import json
import os
import shutil
import threading
import time

from app.ffmpeg_supervisor import FFmpegCancelled, FFmpegError, run_ffmpeg
from app.offline_asset_queue import prepared_limit
from app.playback import ffmpeg_path, ffprobe_path
from fastapi import HTTPException

_capability_lock = threading.Lock()
_capabilities = {}
_capability_time = 0.0


def conversion_capabilities():
    global _capabilities, _capability_time
    with _capability_lock:
        if time.monotonic() - _capability_time < 300:
            return dict(_capabilities)
        executable = ffmpeg_path()
        result = {"video": False, "audio": False, "toneMapping": False}
        if executable and ffprobe_path():
            try:
                encoders = run_ffmpeg(
                    [executable, "-hide_banner", "-encoders"], timeout_seconds=10
                ).decode("utf-8", "replace")
                filters = run_ffmpeg(
                    [executable, "-hide_banner", "-filters"], timeout_seconds=10
                ).decode("utf-8", "replace")
                result["audio"] = " aac " in encoders
                result["video"] = " libx264 " in encoders and result["audio"]
                result["toneMapping"] = " zscale " in filters and " tonemap " in filters
            except (OSError, FFmpegError):
                # Original downloads remain available when conversion tools fail.
                pass
        _capabilities = result
        _capability_time = time.monotonic()
        return dict(result)


def probe_file(path):
    executable = ffprobe_path()
    if not executable:
        raise HTTPException(409, "FFprobe is unavailable.")
    return json.loads(
        run_ffmpeg(
            [
                executable,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            timeout_seconds=30,
        )
    )


def is_hdr(source):
    return any(
        stream.get("color_transfer") in {"smpte2084", "arib-std-b67"}
        for stream in source["probe"].get("streams", [])
        if stream.get("codec_type") == "video"
    )


def audio_index(source, requested):
    tracks = [
        stream
        for stream in source["probe"].get("streams", [])
        if stream.get("codec_type") == "audio"
    ]
    if requested is not None:
        if not any(stream.get("index") == requested for stream in tracks):
            raise HTTPException(400, "The selected audio track is unavailable.")
        return requested
    if not tracks:
        return None
    return next(
        (
            stream["index"]
            for stream in tracks
            if stream.get("disposition", {}).get("default")
        ),
        tracks[0]["index"],
    )


def audio_track_tags(source, selected):
    stream = next(
        (
            item
            for item in source["probe"].get("streams", [])
            if item.get("index") == selected
        ),
        {},
    )
    tags = {str(key).lower(): value for key, value in stream.get("tags", {}).items()}
    if not source.get("videoCodec"):
        for key, value in source["probe"].get("format", {}).get("tags", {}).items():
            tags.setdefault(str(key).lower(), value)
    title = tags.get("title")
    if not title and tags.get("handler_name") not in {
        None,
        "SoundHandler",
        "AudioHandler",
    }:
        title = tags.get("handler_name")
    values = {"language": tags.get("language"), "title": title}
    return {
        key: str(value)
        for key, value in values.items()
        if value is not None and 0 < len(str(value)) <= 128
    }


def audio_metadata_args(source, selected):
    tags = audio_track_tags(source, selected)
    result = []
    if "language" in tags:
        result.extend(["-metadata:s:a:0", f"language={tags['language']}"])
    if "title" in tags:
        result.extend(["-metadata:s:a:0", f"handler_name={tags['title']}"])
    return result


def video_command(source, request, target):
    profile = request["profile"]
    height, bitrate = (720, 2500000) if profile == "video-720p" else (1080, 5000000)
    width = 1280 if height == 720 else 1920
    capabilities = conversion_capabilities()
    if not capabilities["video"] or not source.get("videoCodec"):
        raise HTTPException(409, "Portable video conversion is unavailable.")
    filters = []
    if is_hdr(source):
        if not capabilities["toneMapping"]:
            raise HTTPException(
                409, "HDR tone mapping is unavailable; download the original."
            )
        filters.extend(
            [
                "zscale=t=linear:npl=100",
                "format=gbrpf32le",
                "tonemap=tonemap=hable:desat=0",
                "zscale=p=bt709:t=bt709:m=bt709:r=limited",
            ]
        )
    filters.append(
        f"scale=w='min({width},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    filters.append("format=yuv420p")
    videos = [
        stream
        for stream in source["probe"].get("streams", [])
        if stream.get("codec_type") == "video"
        and not stream.get("disposition", {}).get("attached_pic")
    ]
    if not videos:
        raise HTTPException(409, "Video stream is unavailable.")
    selected = audio_index(source, request.get("audioStreamId"))
    command = [
        ffmpeg_path(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source["path"]),
        "-map",
        f"0:{videos[0]['index']}",
    ]
    if selected is not None:
        command.extend(["-map", f"0:{selected}"])
    command.extend(
        [
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-sn",
            "-vf",
            ",".join(filters),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-b:v",
            str(bitrate),
            "-maxrate",
            str(int(bitrate * 1.2)),
            "-bufsize",
            str(bitrate * 2),
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ac",
            "2",
            "-threads",
            "2",
            "-filter_threads",
            "2",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(target),
        ]
    )
    if selected is not None:
        command[-1:-1] = audio_metadata_args(source, selected)
    return command


def audio_command(source, request, target):
    if not conversion_capabilities()["audio"]:
        raise HTTPException(409, "Compact audio conversion is unavailable.")
    selected = audio_index(source, request.get("audioStreamId"))
    if selected is None:
        raise HTTPException(409, "Audio stream is unavailable.")
    track = next(
        stream
        for stream in source["probe"]["streams"]
        if stream.get("index") == selected
    )
    rate = int(track.get("sample_rate") or 48000)
    if rate not in {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000}:
        rate = 48000
    bitrate = "128k" if request["profile"] == "audio-128k" else "256k"
    command = [
        ffmpeg_path(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source["path"]),
        "-map",
        f"0:{selected}",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-vn",
        "-sn",
        "-c:a",
        "aac",
        "-b:a",
        bitrate,
        "-ac",
        "2",
        "-ar",
        str(rate),
        "-threads",
        "2",
    ]
    if int(track.get("channels") or 2) <= 2:
        tags = source["probe"].get("format", {}).get("tags", {})
        for key, value in tags.items():
            if (
                key.lower()
                in {
                    "replaygain_track_gain",
                    "replaygain_track_peak",
                    "replaygain_album_gain",
                    "replaygain_album_peak",
                }
                and len(str(value)) <= 64
            ):
                command.extend(["-metadata", f"{key.lower()}={value}"])
    command.extend(audio_metadata_args(source, selected))
    command.extend(["-movflags", "+faststart", "-f", "mp4", str(target)])
    return command


def prepare_audio(assets, identity, source, request, should_stop):
    result = prepare_video(
        assets,
        identity,
        source,
        request,
        should_stop,
        command_builder=audio_command,
        suffix=".m4a",
        mime_type="audio/mp4",
    )
    selected = audio_index(source, request.get("audioStreamId"))
    track = next(
        stream
        for stream in source["probe"]["streams"]
        if stream.get("index") == selected
    )
    if int(track.get("channels") or 2) <= 2:
        result["replayGain"] = {
            key.lower(): str(value)
            for key, value in source["probe"].get("format", {}).get("tags", {}).items()
            if key.lower()
            in {
                "replaygain_track_gain",
                "replaygain_track_peak",
                "replaygain_album_gain",
                "replaygain_album_peak",
            }
            and len(str(value)) <= 64
        }
    return result


def prepare_video(
    assets,
    identity,
    source,
    request,
    should_stop,
    *,
    command_builder=video_command,
    suffix=".mp4",
    mime_type="video/mp4",
):
    cache = assets.queue.cache_directory
    cache.mkdir(parents=True, exist_ok=True)
    temporary = cache / (identity + suffix + ".part")
    final = cache / (identity + suffix)
    limit = prepared_limit()
    rows = assets.db.read_execute(
        "SELECT COALESCE(SUM(json_extract(result_json,'$.sizeBytes')),0) FROM offline_assets WHERE result_json IS NOT NULL AND json_extract(result_json,'$.storageName') IS NOT NULL"
    )
    available = limit - int(rows[0][0] or 0)
    if available <= 0 or shutil.disk_usage(cache).free < 1073741824:
        raise HTTPException(507, "Prepared media storage is full.")

    def terminate():
        if temporary.exists() and temporary.stat().st_size > available:
            raise HTTPException(507, "Prepared media storage limit reached.")
        if shutil.disk_usage(cache).free < 1073741824:
            raise HTTPException(507, "Prepared media storage is full.")
        return should_stop()

    try:
        try:
            run_ffmpeg(
                command_builder(source, request, temporary),
                should_terminate=terminate,
                timeout_seconds=21600,
            )
        except FFmpegCancelled as error:
            raise InterruptedError("Conversion interrupted") from error
        probe = probe_file(temporary)
        duration = float(probe.get("format", {}).get("duration") or 0)
        expected = float(source.get("durationSeconds") or 0)
        if duration <= 0 or (
            expected and abs(duration - expected) > max(2, expected * 0.01)
        ):
            raise HTTPException(409, "Prepared media duration is invalid.")
        size = temporary.stat().st_size
        if size <= 0 or size > available:
            raise HTTPException(507, "Prepared media storage limit reached.")
        digest = hashlib.sha256()
        with temporary.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if should_stop():
                    raise InterruptedError("Verification interrupted")
                digest.update(chunk)
        stat = source["path"].stat()
        revision = hashlib.sha256(
            json.dumps((stat.st_size, stat.st_mtime_ns, stat.st_ino)).encode()
        ).hexdigest()
        if revision != request["sourceRevision"]:
            raise HTTPException(409, "Source changed during conversion.")
        with temporary.open("rb+") as stream:
            os.fsync(stream.fileno())
        temporary.replace(final)
        tracks = assets.tracks({"probe": probe})
        selected = audio_index(source, request.get("audioStreamId"))
        selected_tags = audio_track_tags(source, selected)
        for track in tracks:
            if track["type"] == "audio":
                track["sourceIndex"] = selected
                if "language" in selected_tags:
                    track["language"] = selected_tags["language"]
                if "title" in selected_tags:
                    track["displayTitle"] = selected_tags["title"]
        return {
            "storageName": final.name,
            "sizeBytes": size,
            "sha256": digest.hexdigest(),
            "mimeType": mime_type,
            "durationSeconds": duration,
            "trackManifest": tracks,
        }
    finally:
        temporary.unlink(missing_ok=True)
