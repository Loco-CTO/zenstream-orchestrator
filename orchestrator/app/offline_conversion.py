import hashlib
import json
import os
import shutil
import threading
import time

from app.ffmpeg_supervisor import FFmpegCancelled, run_ffmpeg
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
            encoders = run_ffmpeg(
                [executable, "-hide_banner", "-encoders"], timeout_seconds=10
            ).decode("utf-8", "replace")
            filters = run_ffmpeg(
                [executable, "-hide_banner", "-filters"], timeout_seconds=10
            ).decode("utf-8", "replace")
            result["audio"] = " aac " in encoders
            result["video"] = " libx264 " in encoders and result["audio"]
            result["toneMapping"] = " zscale " in filters and " tonemap " in filters
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
    return command


def prepare_video(assets, identity, source, request, should_stop):
    cache = assets.queue.cache_directory
    cache.mkdir(parents=True, exist_ok=True)
    temporary = cache / (identity + ".mp4.part")
    final = cache / (identity + ".mp4")
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
                video_command(source, request, temporary),
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
        for track in tracks:
            if track["type"] == "audio":
                track["sourceIndex"] = selected
        return {
            "storageName": final.name,
            "sizeBytes": size,
            "sha256": digest.hexdigest(),
            "mimeType": "video/mp4",
            "durationSeconds": duration,
            "trackManifest": tracks,
        }
    finally:
        temporary.unlink(missing_ok=True)
