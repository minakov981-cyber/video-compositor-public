import functools
import json
import os
import random
import re
import shutil
import subprocess


def _detect_ffmpeg():
    """Return (ffmpeg_bin, ffprobe_bin, drawtext_ok).

    Prefers ffmpeg-full (has libfreetype/drawtext) over the default bottle.
    Falls back to whatever is on PATH.
    """
    candidates = [
        "/opt/homebrew/opt/ffmpeg-full/bin",   # keg-only ffmpeg-full on macOS/ARM
        "/usr/local/opt/ffmpeg-full/bin",       # keg-only on macOS/Intel
    ]
    for prefix in candidates:
        ff = os.path.join(prefix, "ffmpeg")
        fp = os.path.join(prefix, "ffprobe")
        if os.path.isfile(ff):
            result = subprocess.run([ff, "-filters"], capture_output=True, text=True)
            if "drawtext" in result.stdout:
                return ff, fp, True

    # Fall back to whatever ffmpeg is on PATH
    ff = shutil.which("ffmpeg") or "ffmpeg"
    fp = shutil.which("ffprobe") or "ffprobe"
    result = subprocess.run([ff, "-filters"], capture_output=True, text=True)
    return ff, fp, "drawtext" in result.stdout


FFMPEG, FFPROBE, DRAWTEXT_SUPPORTED = _detect_ffmpeg()

OUTPUT_FORMATS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1":  (1080, 1080),
}

# Safe-zone preset y positions (top of text block, reference canvas 1080×1920)
POSITION_Y = {
    "top":          380,
    "upper_center": 650,
    "center":       876,
    "lower_center": 1100,
    "bottom":       1350,
}

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def get_video_duration(filepath):
    cmd = [
        FFPROBE, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        filepath,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and "duration" in stream:
            return float(stream["duration"])

    fmt = data.get("format", {})
    if "duration" in fmt:
        return float(fmt["duration"])

    return 0.0


def parse_duration_range(s):
    s = str(s).strip()
    if "-" in s:
        lo, hi = s.split("-", 1)
        return float(lo), float(hi)
    v = float(s)
    return v, v


def _escape_drawtext(text):
    # FFmpeg drawtext option-level escaping
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\'")
    text = text.replace(":", "\\:")
    return text


def _escape_path_for_filter(path):
    # FFmpeg filter option escaping for file paths
    path = path.replace("\\", "\\\\")
    path = path.replace("'", "\\'")
    path = path.replace(":", "\\:")
    return path


@functools.lru_cache(maxsize=8)
def _resolve_font_path(font_name="Sans"):
    """Return the file path fontconfig resolves for font_name (cached)."""
    try:
        r = subprocess.run(
            ["fc-match", "--format=%{file}", font_name],
            capture_output=True, text=True,
        )
        path = r.stdout.strip()
        if path and os.path.isfile(path):
            return path
    except FileNotFoundError:
        pass
    for fallback in (
        "/Library/Fonts/NotoSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if os.path.isfile(fallback):
            return fallback
    return None


def _resolve_custom_font(font_name):
    """Return the full path if font_name exists in the fonts/ directory."""
    if not font_name:
        return None
    path = os.path.join(FONTS_DIR, font_name)
    return path if os.path.isfile(path) else None


def _measure_text_width(text, font_size, font_path=None):
    """Measure pixel width of text using Pillow with the same font FFmpeg uses.

    Falls back to a character-count estimate if Pillow or the font is unavailable.
    """
    try:
        from PIL import ImageFont
        fp = font_path or _resolve_font_path("Sans")
        if fp:
            font = ImageFont.truetype(fp, font_size)
            try:
                bbox = font.getbbox(text)
                return bbox[2] - bbox[0]
            except AttributeError:
                return font.getsize(text)[0]
    except Exception:
        pass
    # Fallback: ~0.55 × font_size per character for Noto Sans / typical sans-serif
    return int(len(text) * font_size * 0.55)


def _build_drawtext_filters(text_options, canvas_w=1080, canvas_h=1920):
    """Return (filter_list, warning_or_None).

    Box mode: one drawbox covering the entire text block (all lines), sized to
    the longest line.  Width is measured via Pillow; falls back to a
    character-count estimate.  All drawtext lines are drawn on top without
    their own box.

    Shadow mode: drawtext with shadow params, no box.
    None mode:   plain drawtext.

    Y positions are scaled from the 1920-height reference to canvas_h.
    Box x-centering uses canvas_w.
    """
    if not text_options or not text_options.get("text", "").strip():
        return [], None
    if not DRAWTEXT_SUPPORTED:
        return [], (
            "Text overlay was skipped — your FFmpeg build lacks libfreetype/drawtext. "
            f"Using: {FFMPEG}"
        )

    lines = [l for l in text_options["text"].strip().split("\n") if l.strip()]
    if not lines:
        return [], None

    font_size  = int(text_options.get("font_size", 48))
    text_color = text_options.get("color", "white")
    if text_color.startswith("#"):
        text_color = "0x" + text_color[1:]

    # Scale preset y from the 1920-height reference canvas to the actual canvas_h
    ref_y  = POSITION_Y.get(text_options.get("position", "top"), 380)
    base_y = int(ref_y * canvas_h / 1920) + int(text_options.get("offset", 0))

    line_spacing = int(font_size * 1.3)
    style        = text_options.get("style", "box")

    # Resolve custom font (returns None → use FFmpeg default)
    custom_font_path = _resolve_custom_font(text_options.get("font", ""))
    fontfile_opt = (
        f":fontfile='{_escape_path_for_filter(custom_font_path)}'"
        if custom_font_path else ""
    )

    filters = []

    if style == "box":
        box_color   = text_options.get("box_color", "#000000")
        if box_color.startswith("#"):
            box_color = "0x" + box_color[1:]
        box_opacity = float(text_options.get("box_opacity", 60)) / 100.0

        widths = [_measure_text_width(line, font_size, custom_font_path) for line in lines]
        max_w = max(widths)

        BOX_PAD_X = 20
        BOX_PAD_Y = 20
        box_w = max_w + 2 * BOX_PAD_X
        box_x = (canvas_w - max_w) // 2 - BOX_PAD_X   # centered on canvas_w
        box_h = (len(lines) - 1) * line_spacing + font_size + 2 * BOX_PAD_Y
        box_y = base_y - BOX_PAD_Y

        filters.append(
            f"drawbox=x={box_x}:y={box_y}:w={box_w}:h={box_h}"
            f":color={box_color}@{box_opacity:.2f}:t=fill"
        )
        for idx, line in enumerate(lines):
            y_pos = base_y + idx * line_spacing
            filters.append(
                f"drawtext=text='{_escape_drawtext(line.strip())}'"
                f":fontsize={font_size}:fontcolor={text_color}"
                f":x=(w-text_w)/2:y={y_pos}{fontfile_opt}"
            )

    else:
        for idx, line in enumerate(lines):
            y_pos = base_y + idx * line_spacing
            dt = (
                f"drawtext=text='{_escape_drawtext(line.strip())}'"
                f":fontsize={font_size}:fontcolor={text_color}"
                f":x=(w-text_w)/2:y={y_pos}{fontfile_opt}"
            )
            if style == "shadow":
                dt += ":shadowx=2:shadowy=2:shadowcolor=black@0.6"
            filters.append(dt)

    return filters, None


def _build_crop_vf(w, h):
    """Scale-and-crop filter string for -vf (crop-to-fill mode)."""
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def _build_blur_filter_complex(w, h, drawtext_filters=None):
    """filter_complex string for fit-with-blur mode.

    Splits the input into two streams:
    - background: scaled/cropped to w×h then heavily blurred
    - foreground: scaled to fit within w×h (letterboxed), overlaid centred on bg
    Optional drawtext filters are chained after the overlay.
    """
    fc = (
        f"[0:v]split=2[bg_in][fg_in];"
        f"[bg_in]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},boxblur=20:3[bg_blur];"
        f"[fg_in]scale={w}:{h}:force_original_aspect_ratio=decrease[fg_fit];"
        f"[bg_blur][fg_fit]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2"
    )
    if drawtext_filters:
        fc += "," + ",".join(drawtext_filters)
    fc += "[vout]"
    return fc


def _split_middle_clips(clips):
    """Return (numbered_sorted, mixed) from a list of middle clip paths.

    numbered: stem is a pure integer (1.mp4, 14.mp4) — sorted ascending
    mixed:    everything else — order preserved for caller to shuffle
    """
    numbered, mixed = [], []
    for path in clips:
        stem = os.path.splitext(os.path.basename(path))[0]
        if re.fullmatch(r'\d+', stem):
            numbered.append((int(stem), path))
        else:
            mixed.append(path)
    numbered.sort(key=lambda x: x[0])
    return [path for _, path in numbered], mixed


def compose_video(hook_clips, middle_clips, final_clips, audio,
                  duration_range, text_options, output_path, temp_dir,
                  variation=False, music_start=0.0, use_original_duration=False,
                  output_format="9:16", fit_mode="crop"):
    w, h = OUTPUT_FORMATS.get(output_format, (1080, 1920))
    min_dur, max_dur = parse_duration_range(duration_range)

    if variation:
        # Variations: all middle clips shuffled together regardless of filename
        pool = list(middle_clips)
        random.shuffle(pool)
        ordered = hook_clips + pool + final_clips
    else:
        # Initial composition: numbered clips first in ascending order, mixed clips shuffled
        numbered, mixed = _split_middle_clips(middle_clips)
        shuffled_mixed = list(mixed)
        random.shuffle(shuffled_mixed)
        ordered = hook_clips + numbered + shuffled_mixed + final_clips

    if not ordered:
        raise ValueError("No clips provided")

    # Resolve drawtext filters once — scaled to target canvas dimensions
    drawtext_filters, text_warning = _build_drawtext_filters(
        text_options, canvas_w=w, canvas_h=h
    )

    # ── Step 1: process each clip to normalised w×h silent segments ──
    processed = []
    for i, clip_path in enumerate(ordered):
        clip_dur = get_video_duration(clip_path)
        is_final = "final" in os.path.basename(clip_path).lower()

        # "final" clips are always exactly 3 seconds regardless of use_original_duration
        if is_final:
            actual_dur = min(3.0, clip_dur) if clip_dur > 0 else 3.0
            apply_trim = True
        elif use_original_duration:
            actual_dur = clip_dur
            apply_trim = False
        else:
            eff_max = min(max_dur, clip_dur) if clip_dur > 0 else max_dur
            eff_min = min(min_dur, eff_max)
            actual_dur = random.uniform(eff_min, eff_max)
            apply_trim = True

        # Text overlay is skipped on the "final" clip
        active_dt = [] if (is_final or not drawtext_filters) else drawtext_filters

        out = os.path.join(temp_dir, f"clip_{i:03d}.mp4")

        if fit_mode == "blur":
            fc = _build_blur_filter_complex(w, h, active_dt)
            cmd = [FFMPEG, "-y", "-i", clip_path,
                   "-filter_complex", fc,
                   "-map", "[vout]"]
            if apply_trim:
                cmd += ["-t", str(actual_dur)]
            cmd += ["-an", "-c:v", "libx264", "-preset", "fast",
                    "-pix_fmt", "yuv420p", "-r", "30", out]
        else:  # crop (default)
            vf = _build_crop_vf(w, h)
            if active_dt:
                vf += "," + ",".join(active_dt)
            cmd = [FFMPEG, "-y", "-i", clip_path]
            if apply_trim:
                cmd += ["-t", str(actual_dur)]
            cmd += ["-vf", vf, "-an", "-c:v", "libx264", "-preset", "fast",
                    "-pix_fmt", "yuv420p", "-r", "30", out]

        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"FFmpeg failed on clip {os.path.basename(clip_path)}:\n{r.stderr[-800:]}"
            )
        processed.append((out, actual_dur))

    # ── Step 2: build concat list ──
    concat_file = os.path.join(temp_dir, "concat.txt")
    with open(concat_file, "w") as f:
        for path, _ in processed:
            f.write(f"file '{os.path.abspath(path)}'\n")

    total_duration = sum(d for _, d in processed)

    # ── Step 3: concatenate clips, optionally mix in music ──
    # (drawtext was already baked into each clip in Step 1)
    if audio:
        audio_input = []
        if music_start and music_start > 0:
            audio_input = ["-ss", str(music_start)]
        audio_input += ["-i", audio]

        cmd = [
            FFMPEG, "-y",
            "-f", "concat", "-safe", "0", "-i", concat_file,
            *audio_input,
            "-t", str(total_duration),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            output_path,
        ]
    else:
        cmd = [
            FFMPEG, "-y",
            "-f", "concat", "-safe", "0", "-i", concat_file,
            "-t", str(total_duration),
            "-map", "0:v:0",
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-an",
            output_path,
        ]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg concat/mix failed:\n{r.stderr[-800:]}")

    return {
        "total_duration": total_duration,
        "clip_order": [os.path.basename(p) for p, _ in zip(ordered, processed)],
        "warning": text_warning,
    }
