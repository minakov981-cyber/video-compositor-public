"""Standalone single-file tools for the Tools tab: compress, extract audio, convert, speed."""
import json
import os
import shutil
import subprocess

from compositor import FFMPEG, FFPROBE

GIF_MAX_SECONDS = 30

TOOL_OPTIONS = {
    "compress":      {"light", "medium", "strong"},
    "extract_audio": {"mp3", "m4a", "wav"},
    "convert":       {"mp4", "mov", "webm", "gif"},
    "speed":         {"0.5", "0.75", "1.25", "1.5", "2"},
}

# Suffix added to the original file name, and the output extension per option
_SUFFIX = {"compress": "compressed", "extract_audio": "audio", "convert": "converted", "speed": "speed"}


def _probe(path):
    """(width, height, has_audio) of a media file."""
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,width,height",
         "-of", "json", path],
        capture_output=True, text=True,
    )
    streams = json.loads(r.stdout or "{}").get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return video.get("width"), video.get("height"), has_audio


def _even(vf=""):
    """libx264/yuv420p need even dimensions; append a safe rounding scale."""
    rounding = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    return f"{vf},{rounding}" if vf else rounding


def _h264(crf):
    return ["-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart"]


def output_extension(tool, option, src_ext):
    if tool == "extract_audio":
        return option
    if tool == "convert":
        return option
    return src_ext if src_ext in ("mp4", "mov") else "mp4"


def output_name(tool, option, original_name):
    stem, ext = os.path.splitext(original_name)
    ext = output_extension(tool, option, ext.lstrip(".").lower())
    label = f"{_SUFFIX[tool]}_{option.replace('.', '_')}x" if tool == "speed" else _SUFFIX[tool]
    return f"{stem}_{label}.{ext}"


def run_tool(tool, option, src, dst):
    """Process src into dst. Raises ValueError for bad input, RuntimeError on FFmpeg failure.

    Returns a dict of extra result info (e.g. {"no_gain": True} when compressing
    would have made the file bigger, in which case dst is a copy of src).
    """
    if option not in TOOL_OPTIONS.get(tool, ()):
        raise ValueError("Unknown tool option")

    w, h, has_audio = _probe(src)
    if tool != "extract_audio" and not w:
        raise ValueError("This file has no video stream")

    cmd = [FFMPEG, "-y", "-i", src]

    if tool == "compress":
        crf = {"light": 23, "medium": 28, "strong": 28}[option]
        vf = ""
        if option == "strong" and min(w, h) > 720:
            # Downscale so the short side is 720px (1080p → 720p)
            vf = "scale=720:-2" if w <= h else "scale=-2:720"
        cmd += ["-map", "0:v:0", "-map", "0:a?", "-vf", _even(vf), *_h264(crf),
                "-c:a", "aac", "-b:a", "128k"]

    elif tool == "extract_audio":
        if not has_audio:
            raise ValueError("This file has no audio track")
        codec = {
            "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
            "m4a": ["-c:a", "aac", "-b:a", "192k"],
            "wav": ["-c:a", "pcm_s16le"],
        }[option]
        cmd += ["-vn", "-map", "0:a:0", *codec]

    elif tool == "convert":
        if option == "gif":
            cmd = [FFMPEG, "-y", "-t", str(GIF_MAX_SECONDS), "-i", src,
                   "-filter_complex",
                   # Longest side 480px keeps vertical GIFs from ballooning in size
                   "fps=12,scale='if(gte(iw,ih),min(480,iw),-2)':'if(gte(iw,ih),-2,min(480,ih))'"
                   ":flags=lanczos,split[a][b];"
                   "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
                   "-loop", "0"]
        elif option == "webm":
            cmd += ["-map", "0:v:0", "-map", "0:a?", "-vf", _even(),
                    "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
                    "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
                    "-pix_fmt", "yuv420p", "-c:a", "libopus", "-b:a", "160k"]
        else:  # mp4 / mov — re-encode to H.264 so it plays everywhere (e.g. iPhone HEVC → H.264)
            cmd += ["-map", "0:v:0", "-map", "0:a?", "-vf", _even(), *_h264(20),
                    "-c:a", "aac", "-b:a", "192k"]

    elif tool == "speed":
        factor = float(option)
        cmd += ["-map", "0:v:0", "-vf", _even(f"setpts=PTS/{factor}"), *_h264(20)]
        if has_audio:
            cmd += ["-map", "0:a:0", "-af", f"atempo={factor}", "-c:a", "aac", "-b:a", "192k"]

    cmd.append(dst)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{r.stderr[-800:]}")

    # Already well-compressed sources can come out bigger — hand back the original then
    if tool == "compress" and os.path.getsize(dst) >= os.path.getsize(src):
        shutil.copyfile(src, dst)
        return {"no_gain": True}
    return {}
