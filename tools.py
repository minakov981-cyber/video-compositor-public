"""Standalone single-file tools for the Tools tab: compress, extract audio, convert, speed, screenshots."""
import json
import os
import shutil
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from compositor import FFMPEG, FFPROBE, get_video_duration

GIF_MAX_SECONDS = 30

TOOL_OPTIONS = {
    "compress":      {"light", "medium", "strong"},
    "extract_audio": {"mp3", "m4a", "wav"},
    "convert":       {"mp4", "mov", "webm", "gif"},
    "speed":         {"0.5", "0.75", "1.25", "1.5", "2"},
    "frames":        {"first", "last", "pick", "multi"},
}

FRAMES_MIN, FRAMES_MAX = 2, 100
FRAME_THUMBS = 12      # small previews shown under the Screenshots tool
FRAME_THUMB_PX = 240   # longest side of a preview
FRAME_WORKERS = 4      # FFmpeg grabs running at once for Multiple

# Suffix added to the original file name, and the output extension per option
_SUFFIX = {"compress": "compressed", "extract_audio": "audio", "convert": "converted", "speed": "speed",
           "frames": "frames"}
_FRAME_SUFFIX = {"first": "first_frame", "last": "last_frame", "pick": "frame", "multi": "frames"}


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
    if tool == "frames":
        return "zip" if option == "multi" else "jpg"
    if tool == "extract_audio":
        return option
    if tool == "convert":
        return option
    return src_ext if src_ext in ("mp4", "mov") else "mp4"


def output_name(tool, option, original_name):
    stem, ext = os.path.splitext(original_name)
    ext = output_extension(tool, option, ext.lstrip(".").lower())
    if tool == "frames":
        label = _FRAME_SUFFIX[option]
    elif tool == "speed":
        label = f"{_SUFFIX[tool]}_{option.replace('.', '_')}x"
    else:
        label = _SUFFIX[tool]
    return f"{stem}_{label}.{ext}"


def frame_params(option, form):
    """Validated Screenshots settings from the request form; raises ValueError for bad values.

    pick: "time" (seconds). multi: "count" (2-100) and an optional "start"/"end" range in seconds.
    """
    def seconds(name, default=None):
        raw = form.get(name, "")
        if raw in ("", None):
            if default is None:
                raise ValueError("Choose a moment on the timeline first")
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError("Invalid time on the timeline")
        if value < 0 or value != value:
            raise ValueError("Invalid time on the timeline")
        return value

    if option == "pick":
        return {"time": seconds("time")}
    if option == "multi":
        try:
            count = int(form.get("count", ""))
        except (TypeError, ValueError):
            raise ValueError(f"Choose between {FRAMES_MIN} and {FRAMES_MAX} frames")
        if not FRAMES_MIN <= count <= FRAMES_MAX:
            raise ValueError(f"Choose between {FRAMES_MIN} and {FRAMES_MAX} frames")
        start, end = seconds("start", 0.0), seconds("end", -1.0)
        if end >= 0 and end <= start:
            raise ValueError("The end of the range must be after its start")
        return {"count": count, "start": start, "end": end}
    return {}


def frame_times(count, start, end):
    """count moments spread evenly from start to end, both ends included (2 = first and last)."""
    step = (end - start) / (count - 1)
    return [round(start + i * step, 3) for i in range(count)]


def _fmt_time(t):
    m, s = divmod(t, 60)
    return f"{int(m):02d}m{s:05.2f}s"


def _grab_frame(src, t, dst, duration):
    """Save the frame at t seconds as a high-quality JPG. Moments at the very end use the last frame."""
    if t < duration - 0.05:
        r = subprocess.run([FFMPEG, "-y", "-ss", f"{t:.3f}", "-i", src, "-map", "0:v:0",
                            "-frames:v", "1", "-q:v", "2", dst], capture_output=True, text=True)
        if r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0:
            return
    # Seeking past the last decodable frame gives nothing: decode the last second and keep the final frame
    r = subprocess.run([FFMPEG, "-y", "-sseof", "-1", "-i", src, "-map", "0:v:0",
                        "-update", "1", "-q:v", "2", dst], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(dst):
        raise RuntimeError(f"FFmpeg failed:\n{r.stderr[-800:]}")


def _thumb(src_jpg, dst_jpg):
    with Image.open(src_jpg) as im:
        im.thumbnail((FRAME_THUMB_PX, FRAME_THUMB_PX))
        im.convert("RGB").save(dst_jpg, "JPEG", quality=80)


def _run_frames(option, params, src, dst, thumb_prefix):
    """Screenshots tool: one JPG (first / last / picked moment) or a ZIP of evenly spaced JPGs."""
    duration = get_video_duration(src)
    if duration <= 0:
        raise ValueError("This file has no video stream")
    # Names inside the ZIP follow the download name, without the "<job id>_" prefix of the stored file
    stem = os.path.splitext(os.path.basename(dst))[0]
    if stem.startswith(os.path.basename(thumb_prefix) + "_"):
        stem = stem[len(os.path.basename(thumb_prefix)) + 1:]

    if option == "first":
        times = [0.0]
    elif option == "last":
        times = [duration]
    elif option == "pick":
        times = [min(params["time"], duration)]
    else:
        start = min(params["start"], duration)
        end   = duration if params["end"] < 0 else min(params["end"], duration)
        if end - start < 0.05:
            raise ValueError("The selected range is too short")
        times = frame_times(params["count"], start, end)

    work = dst + "_parts"
    os.makedirs(work, exist_ok=True)
    try:
        frames = [(os.path.join(work, f"{i:03d}.jpg"), t) for i, t in enumerate(times, 1)]
        with ThreadPoolExecutor(FRAME_WORKERS) as pool:
            list(pool.map(lambda f: _grab_frame(src, f[1], f[0], duration), frames))

        # Previews for the page: up to FRAME_THUMBS, spread over all frames
        picks = sorted({round(k * (len(frames) - 1) / max(1, FRAME_THUMBS - 1)) for k in range(FRAME_THUMBS)})
        thumbs = []
        for k in picks[:FRAME_THUMBS]:
            path, t = frames[k]
            name = f"{thumb_prefix}_thumb_{k + 1:03d}.jpg"
            _thumb(path, os.path.join(os.path.dirname(dst), name))
            thumbs.append({"file": name, "time": t})

        if option == "multi":
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED) as z:  # JPGs are already compressed
                for i, (path, t) in enumerate(frames, 1):
                    z.write(path, f"{stem}_{i:03d}_{_fmt_time(t)}.jpg")
        else:
            shutil.move(frames[0][0], dst)
        return {"frames": len(frames), "thumbs": thumbs, "times": [t for _, t in frames]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_tool(tool, option, src, dst, params=None, thumb_prefix=None):
    """Process src into dst. Raises ValueError for bad input, RuntimeError on FFmpeg failure.

    Returns a dict of extra result info (e.g. {"no_gain": True} when compressing
    would have made the file bigger, in which case dst is a copy of src).
    """
    if option not in TOOL_OPTIONS.get(tool, ()):
        raise ValueError("Unknown tool option")
    if tool == "frames":
        return _run_frames(option, params or {}, src, dst, thumb_prefix or os.path.splitext(dst)[0])

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
