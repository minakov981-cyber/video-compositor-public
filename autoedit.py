"""Smart Auto (Create's Auto mode): find scenes in clips, score them, find the beat of a track, plan a cut.

Everything runs on FFmpeg plus plain Python (no numpy), on low-resolution copies of the footage, so a
few minutes of video are analysed in seconds. The planned cut is rendered by compositor.compose_video:
every segment is a hard link to its source file with its own trim, which keeps the Create pipeline
(captions, formats, New Variation) working unchanged.
"""
import array
import math
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from compositor import FFMPEG, get_video_duration, has_audio_stream

SCENE_THRESHOLD = 0.32      # FFmpeg scene score above which a frame starts a new scene
MIN_SCENE = 0.6             # shorter "scenes" are flashes; they are merged into the previous one
LONG_SCENE = 6.0            # one-take footage is split into windows of about this length
SAMPLE_FPS = 4              # frames per second sampled for brightness / sharpness / motion
SAMPLE_W = 96               # sample width in pixels (height keeps the aspect)
DARK_LUMA = 20              # mean brightness (0-255) below which a scene counts as too dark
BLURRY_RATIO = 0.45         # sharpness below this share of the video's median counts as blurry
STATIC_DIFF = 0.8           # mean frame-to-frame change below which a scene counts as static
JUMP_DIFF = 18              # sampled frames this different (mean abs, 0-255) also mark a cut
JUMP_LUMA = 12              # ...or a brightness jump this big (soft cuts FFmpeg's scene score misses)
THUMB_PX = 200              # longest side of a scene preview
PACE_SECONDS = {"fast": 1.0, "medium": 1.8, "slow": 3.0}   # piece per clip without music
PACE_BEATS = {"fast": 2, "medium": 4, "slow": 8}           # piece per clip with music, in beats
FPS = 30                    # compose_video renders at 30 fps; cut points are snapped to frames
MAX_SOURCE_SECONDS = 600    # total footage one Auto Edit may analyse
MAX_SOURCES = 10


# ── Scenes and quality ───────────────────────────────────────────────────────

def _scene_cuts(src):
    """Times (s) where FFmpeg sees a scene change, plus the sampled grey frames for scoring."""
    probe = subprocess.run(
        [FFMPEG, "-v", "error", "-i", src, "-map", "0:v:0", "-frames:v", "1",
         "-vf", f"scale={SAMPLE_W}:-2", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True,
    )
    if probe.returncode != 0 or not probe.stdout:
        raise ValueError("This file has no video stream")
    # One decode: scene detection on a small copy, and a few grey frames per second for scoring
    cmd = [FFMPEG, "-hide_banner", "-i", src, "-filter_complex",
           # eq(n,0) always lets the first frame through: an output with no frames at all fails on FFmpeg 7+
           f"[0:v:0]scale=320:-2,split[a][b];[a]select='gt(scene,{SCENE_THRESHOLD})+eq(n,0)',showinfo[s];"
           f"[b]fps={SAMPLE_FPS},scale={SAMPLE_W}:-2,format=gray[g]",
           "-map", "[s]", "-f", "null", "-", "-map", "[g]", "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg scene detection failed:\n{r.stderr.decode(errors='replace')[-800:]}")
    cuts = sorted({round(float(t), 3) for t in re.findall(rb"pts_time:([0-9.]+)", r.stderr)})
    size = len(probe.stdout)
    frames = [r.stdout[i:i + size] for i in range(0, len(r.stdout) - size + 1, size)]
    return cuts, frames, size // SAMPLE_W


def _frame_stats(frames, w, h):
    """(brightness, sharpness, change from previous frame) for each sampled grey frame."""
    stats, prev = [], None
    for f in frames:
        px = memoryview(f)
        n = len(px)
        luma = sum(px) / n
        # Sharpness: mean absolute difference between horizontal neighbours (more edges = sharper)
        grad, rows = 0, 0
        for y in range(0, h, 2):
            row = px[y * w:(y + 1) * w]
            grad += sum(abs(row[x + 1] - row[x]) for x in range(0, w - 1, 2))
            rows += 1
        sharp = grad / max(1, rows * (w // 2))
        diff = (sum(abs(a - b) for a, b in zip(px[::7], prev[::7])) / len(px[::7])) if prev is not None else None
        stats.append((luma, sharp, diff))
        prev = px
    return stats


def _scenes_for(src_index, src, duration):
    cuts, frames, h = _scene_cuts(src)
    stats = _frame_stats(frames, SAMPLE_W, h) if frames else []
    # Second opinion on cuts from the sampled frames: a big jump in picture or brightness
    for i in range(1, len(stats)):
        if (stats[i][2] or 0) > JUMP_DIFF or abs(stats[i][0] - stats[i - 1][0]) > JUMP_LUMA:
            t = round((i - 0.5) / SAMPLE_FPS, 3)
            if all(abs(t - c) > 0.5 for c in cuts):
                cuts.append(t)
    cuts.sort()
    bounds = [0.0] + [c for c in cuts if MIN_SCENE / 2 < c < duration - MIN_SCENE / 2] + [duration]
    scenes = []
    for a, b in zip(bounds, bounds[1:]):
        if scenes and b - a < MIN_SCENE:
            scenes[-1]["end"] = b          # a flash: part of the previous scene
            continue
        scenes.append({"start": a, "end": b})
    # Split long single takes into windows so they give several moments to choose from
    split = []
    for s in scenes:
        length = s["end"] - s["start"]
        parts = max(1, int(length // LONG_SCENE))
        step = length / parts
        for i in range(parts):
            split.append({"start": s["start"] + i * step, "end": s["start"] + (i + 1) * step})

    for s in split:
        idx = [i for i in range(len(stats)) if s["start"] <= i / SAMPLE_FPS < s["end"]] or \
              ([min(len(stats) - 1, int(s["start"] * SAMPLE_FPS))] if stats else [])
        lumas = [stats[i][0] for i in idx]
        sharps = [stats[i][1] for i in idx]
        diffs = [stats[i][2] for i in idx if stats[i][2] is not None]
        s.update(source=src_index,
                 luma=sum(lumas) / len(lumas) if lumas else 128,
                 sharp=sum(sharps) / len(sharps) if sharps else 0,
                 motion=sum(diffs) / len(diffs) if diffs else 0)
    return split


def _thumb(src, t, dst):
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", src, "-map", "0:v:0", "-frames:v", "1",
                        "-vf", f"scale='if(gte(iw,ih),{THUMB_PX},-2)':'if(gte(iw,ih),-2,{THUMB_PX})'",
                        "-q:v", "4", dst], capture_output=True)
    return r.returncode == 0 and os.path.isfile(dst)


def analyze(sources, music, out_dir=None, prefix="", thumbs=True, max_seconds=MAX_SOURCE_SECONDS):
    """Scenes of every source with quality flags and previews, and the beat of the music (if any).

    sources: list of video paths. Previews are written to out_dir as "<prefix>_scene_###.jpg".
    Returns a JSON-able dict; scene times are seconds in their source file.
    """
    durations = [get_video_duration(p) for p in sources]
    if any(d <= 0 for d in durations):
        raise ValueError("One of the files has no video stream")
    if sum(durations) > max_seconds + 1:
        raise ValueError(f"Auto can use up to {max_seconds // 60} minutes of footage at once. "
                         "Use fewer or shorter clips, or switch to Manual trim.")

    with ThreadPoolExecutor(min(4, len(sources))) as pool:
        per_source = list(pool.map(lambda a: _scenes_for(a[0], a[1], a[2]),
                                   [(i, p, d) for i, (p, d) in enumerate(zip(sources, durations))]))
    scenes = [s for group in per_source for s in group]

    # Blur is judged against the other scenes of the same video: footage differs a lot in detail
    medians = {}
    for src_index in range(len(sources)):
        vals = sorted(s["sharp"] for s in scenes if s["source"] == src_index)
        medians[src_index] = vals[len(vals) // 2] if vals else 0
    for i, s in enumerate(scenes):
        flags = []
        if s["luma"] < DARK_LUMA:
            flags.append("dark")
        median_sharp = medians[s["source"]]
        if median_sharp > 0 and s["sharp"] < BLURRY_RATIO * median_sharp:
            flags.append("blurry")
        if s["motion"] < STATIC_DIFF:
            flags.append("static")
        s["id"] = i
        s["flags"] = flags
        s["skip"] = "dark" in flags or "blurry" in flags   # static shots are often fine (a product, a view)
        s["start"], s["end"] = round(s["start"], 3), round(s["end"], 3)
    if scenes and all(s["skip"] for s in scenes):
        for s in scenes:      # e.g. all night footage: keep the flags as a hint but use everything
            s["skip"] = False

    def preview(s):
        name = f"{prefix}_scene_{s['id']:03d}.jpg"
        ok = _thumb(sources[s["source"]], (s["start"] + s["end"]) / 2, os.path.join(out_dir, name))
        s["thumb"] = name if ok else None
    if thumbs and out_dir:
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(preview, scenes))
    for s in scenes:
        median_sharp = medians[s["source"]] or 1
        # Sharper (relative to its own video) and moving shots first; flagged ones last
        s["score"] = round(min(2.0, s["sharp"] / median_sharp) + (0.0 if "static" in s["flags"] else 0.3)
                           - (1.0 if s["skip"] else 0.0), 3)
        for k in ("luma", "sharp", "motion"):
            s.pop(k, None)

    result = {"scenes": scenes, "durations": [round(d, 3) for d in durations],
              "has_audio": [has_audio_stream(p) for p in sources]}
    if music:
        beat = detect_beat(music)
        result.update(music_duration=round(get_video_duration(music) or _audio_duration(music), 3), **beat)
    return result


# ── Beat ─────────────────────────────────────────────────────────────────────

BEAT_SR = 11025       # analysis sample rate
HOP = 128             # samples per onset-envelope step (~11.6 ms)
ENERGY_WIN = 512      # samples per energy window (~46 ms)
MAX_BEAT_AUDIO = 60   # seconds of music analysed for the tempo


def _audio_duration(path):
    r = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"], capture_output=True, text=True)
    m = re.findall(r"time=(\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        return 0.0
    h, mi, s = m[-1]
    return int(h) * 3600 + int(mi) * 60 + float(s)


def _onset_envelope(path):
    """Onset strength per HOP: how much the short-term energy rises, mean removed."""
    r = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-t", str(MAX_BEAT_AUDIO), "-map", "0:a:0", "-ac", "1",
                        "-ar", str(BEAT_SR), "-f", "s16le", "-"], capture_output=True)
    if r.returncode != 0 or len(r.stdout) < BEAT_SR * 2 * 4:
        return None
    samples = array.array("h")
    samples.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    # Energy over a 46 ms window (several periods of even a 55 Hz bass, so steady tones don't wobble),
    # stepped every HOP for timing precision
    energy = [math.log1p(math.sqrt(sum(v * v for v in samples[i:i + ENERGY_WIN:2]) / (ENERGY_WIN // 2)))
              for i in range(0, len(samples) - ENERGY_WIN, HOP)]
    onset = [0.0] + [max(0.0, b - a) for a, b in zip(energy, energy[1:])]
    mean = sum(onset) / len(onset)
    return [o - mean for o in onset]


def detect_beat(path):
    """{"bpm", "beat_period", "first_beat"} of a track, or bpm None if no steady beat was found.

    Coarse tempo from the onset envelope's autocorrelation (70-180 BPM, nudged towards ~118 BPM to
    avoid half/double tempo), then a fine search over sub-step periods and phases for the beat grid
    that lands on the most onset strength, so cuts stay on the beat over a whole video.
    """
    none = {"bpm": None, "beat_period": None, "first_beat": 0.0}
    onset = _onset_envelope(path)
    if not onset:
        return none
    hop_s = HOP / BEAT_SR
    n = len(onset)

    def ac(lag):
        return sum(onset[i] * onset[i - lag] for i in range(lag, n))
    lo, hi = int(60 / 180 / hop_s), int(math.ceil(60 / 70 / hop_s))
    weighted = {lag: ac(lag) * math.exp(-0.5 * ((60 / (lag * hop_s) - 118) / 45) ** 2) for lag in range(lo, hi + 1)}
    coarse = max(weighted, key=weighted.get)
    if weighted[coarse] <= 0:
        return none

    # Fine search; peak[i] tolerates a step of jitter around each predicted beat
    peak = [max(onset[max(0, i - 1):i + 2]) for i in range(n)]
    best, best_p, best_phi = -1e18, float(coarse), 0.0
    p = coarse - 1.5
    while p <= coarse + 1.5:
        phi = 0.0
        while phi < p:
            total, x = 0.0, phi
            while x < n - 1:
                total += peak[int(x + 0.5)]
                x += p
            if total > best:
                best, best_p, best_phi = total, p, phi
            phi += 0.5
        p += 0.05
    if best <= 0:
        return none
    period = best_p * hop_s
    # The energy window sees a hit up to a window early; shift the grid back by half a window
    first = (best_phi * hop_s + ENERGY_WIN / 2 / BEAT_SR) % period
    return {"bpm": round(60 / period, 1), "beat_period": round(period, 5), "first_beat": round(first, 3)}


# ── Planning the cut ─────────────────────────────────────────────────────────

def plan_cut(scenes, durations, pace, beat_period=None, overlap=0.0):
    """One piece from each clip, in clip order: [{source, start, duration, scene}].

    With a beat period each piece lasts PACE_BEATS[pace] beats (fewer whole beats if the clip is too
    short), and cut points are snapped to frames from absolute beat times, so every cut lands on a beat
    and they don't drift. Without music a piece lasts PACE_SECONDS[pace]. The piece comes from the
    clip's best scene that is long enough (sharp, moving, not flagged), centred in it. `overlap`
    (crossfade length) is added to each piece so transitions don't eat into it.
    """
    by_source = {}
    for sc in scenes:
        by_source.setdefault(sc["source"], []).append(sc)

    plan, beats_so_far, secs_so_far = [], 0, 0.0
    for src in range(len(durations)):
        options = by_source.get(src) or [{"id": -1, "start": 0.0, "end": durations[src], "score": 0}]
        if beat_period:
            want = PACE_BEATS[pace]
            fit = max(1, min(want, int(durations[src] / beat_period + 1e-6)))
            start_mark = round(beats_so_far * beat_period * FPS) / FPS
            beats_so_far += fit
            length = round(beats_so_far * beat_period * FPS) / FPS - start_mark
        else:
            length = min(PACE_SECONDS[pace], durations[src])
            start_mark = round(secs_so_far * FPS) / FPS
            secs_so_far += length
            length = round(secs_so_far * FPS) / FPS - start_mark
        length = min(length, durations[src])
        roomy = [o for o in options if o["end"] - o["start"] >= length - 1e-3]
        best = max(roomy or options, key=lambda o: (o.get("score", 0), o["end"] - o["start"]))
        centre = (best["start"] + best["end"]) / 2
        start = max(0.0, min(centre - length / 2, durations[src] - length))
        plan.append({"source": src, "start": round(start, 3), "duration": round(length + overlap, 4),
                     "scene": best["id"]})
    return plan
