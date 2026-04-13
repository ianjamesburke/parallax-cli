#!/usr/bin/env python3
"""
analyze-audio.py — Waveform visualization + silence report for a time range.

Generates a waveform PNG with silence regions shaded and scene boundaries
overlaid. Designed so the model can read the image and make informed edits
to the manifest — without guessing at raw silencedetect numbers.

Usage:
    python3 analyze-audio.py --input audio.mp3
    python3 analyze-audio.py --input video.mp4 --start 10 --end 30
    python3 analyze-audio.py --input audio.mp3 --min-silence 0.2 --manifest manifest.yaml
    python3 analyze-audio.py --input audio.mp3 --output waveform.png --json silences.json

Flags:
    --input          Audio or video file to analyze
    --start          Start time in seconds (default: 0)
    --end            End time in seconds (default: end of file)
    --min-silence    Minimum silence duration to detect, in seconds (default: 0.15)
    --noise          Silence threshold in dB (default: -40)
    --manifest       Optional manifest.yaml — overlays scene boundaries on waveform
    --output         Output PNG path (default: <input>_waveform.png)
    --json           Optional path to write silence regions as JSON
    --width          Image width in pixels (default: 1920)
    --height         Waveform height in pixels (default: 280)

Requires: Pillow, pyyaml
    pip install Pillow pyyaml
"""

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("ERROR: Pillow is required. Run: pip install Pillow", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# ffprobe helpers
# ---------------------------------------------------------------------------

def get_duration(input_path: str) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", input_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0))
    except Exception as e:
        print(f"ERROR: ffprobe failed on {input_path}: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Waveform generation via ffmpeg showwavespic
# ---------------------------------------------------------------------------

def generate_waveform_image(
    input_path: str,
    start: float,
    duration: float,
    width: int,
    height: int,
) -> Image.Image:
    """Run ffmpeg showwavespic on the time range and return a PIL Image."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(max(0.0, round(start, 6))),
        "-t", str(round(duration, 6)),
        "-i", input_path,
        "-filter_complex",
        f"aformat=channel_layouts=mono,showwavespic=s={width}x{height}:colors=#4fc3f7",
        "-frames:v", "1", "-update", "1",
        tmp_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: ffmpeg waveform generation failed: {e.stderr[-400:] if e.stderr else ''}", file=sys.stderr)
        sys.exit(1)

    try:
        img = Image.open(tmp_path).convert("RGBA")
    except Exception as e:
        print(f"ERROR: Could not open waveform output: {e}", file=sys.stderr)
        sys.exit(1)

    Path(tmp_path).unlink(missing_ok=True)
    return img


# ---------------------------------------------------------------------------
# Silence detection
# ---------------------------------------------------------------------------

def detect_silences(
    input_path: str,
    start: float,
    duration: float,
    min_silence: float,
    noise_db: float,
) -> list[dict]:
    """Return list of {start, end, duration} silence regions (absolute seconds)."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-t", str(duration),
        "-i", input_path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        stderr = result.stderr
    except Exception as e:
        print(f"ERROR: silencedetect failed: {e}", file=sys.stderr)
        return []

    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", stderr)]

    # ffmpeg reports timestamps relative to the segment start when using -ss + -t
    silences = []
    for s, e in zip(starts, ends):
        abs_start = start + s
        abs_end = start + e
        silences.append({
            "start": round(abs_start, 3),
            "end": round(abs_end, 3),
            "duration": round(abs_end - abs_start, 3),
        })

    # Handle trailing silence with no end marker
    if len(starts) > len(ends):
        abs_start = start + starts[-1]
        abs_end = start + duration
        silences.append({
            "start": round(abs_start, 3),
            "end": round(abs_end, 3),
            "duration": round(abs_end - abs_start, 3),
        })

    return silences


# ---------------------------------------------------------------------------
# Scene boundary loading
# ---------------------------------------------------------------------------

def load_scenes(manifest_path: str) -> list[dict]:
    """Load scene timing from a manifest.yaml. Returns list of {index, start_s, end_s, vo_text}."""
    try:
        import yaml
    except ImportError:
        print("WARNING: pyyaml not installed — skipping scene overlays. Run: pip install pyyaml", file=sys.stderr)
        return []

    try:
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
    except Exception as e:
        print(f"ERROR: Could not load manifest {manifest_path}: {e}", file=sys.stderr)
        return []

    scenes = []
    for s in manifest.get("scenes", []):
        if s.get("start_s") is not None and s.get("end_s") is not None:
            scenes.append({
                "index": s["index"],
                "start_s": float(s["start_s"]),
                "end_s": float(s["end_s"]),
                "vo_text": s.get("vo_text", ""),
            })
    return scenes


# ---------------------------------------------------------------------------
# Silence → scene mapping
# ---------------------------------------------------------------------------

def map_silences_to_scenes(silences: list[dict], scenes: list[dict]) -> list[dict]:
    """Annotate each silence with which scene(s) it falls within."""
    for silence in silences:
        sm = silence["start"]
        em = silence["end"]
        overlapping = []
        for sc in scenes:
            # Silence overlaps scene if they intersect
            if sm < sc["end_s"] and em > sc["start_s"]:
                overlapping.append(sc["index"])
        silence["scenes"] = overlapping
        # Classify position within scene
        if overlapping:
            sc = next(s for s in scenes if s["index"] == overlapping[0])
            scene_dur = sc["end_s"] - sc["start_s"]
            rel = (sm - sc["start_s"]) / scene_dur if scene_dur > 0 else 0
            if rel > 0.75:
                silence["position"] = "tail"
            elif rel < 0.25:
                silence["position"] = "head"
            else:
                silence["position"] = "mid"
        else:
            silence["position"] = "between"
    return silences


# ---------------------------------------------------------------------------
# Image composition
# ---------------------------------------------------------------------------

LABEL_HEIGHT = 100   # pixels reserved below waveform for timecodes + scene labels
BG_COLOR = (18, 18, 24, 255)
WAVEFORM_BG = (28, 28, 36, 255)
SILENCE_COLOR = (220, 50, 50, 120)     # semi-transparent red
SILENCE_BORDER = (255, 80, 80, 200)
SCENE_LINE_COLOR = (255, 215, 0, 200)  # gold
TIMECODE_COLOR = (160, 160, 180, 255)
LABEL_COLOR = (220, 220, 240, 255)
SCENE_LABEL_COLOR = (255, 215, 0, 255)


def _load_font(size: int):
    """Load a monospace font, falling back to default."""
    candidates = [
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def compose_image(
    waveform_img: Image.Image,
    silences: list[dict],
    scenes: list[dict],
    start: float,
    duration: float,
    width: int,
    waveform_height: int,
    min_silence: float,
) -> Image.Image:
    """Compose waveform + silence overlays + scene markers into final image."""
    total_height = waveform_height + LABEL_HEIGHT
    canvas = Image.new("RGBA", (width, total_height), BG_COLOR)

    # Paste waveform onto dark waveform background
    wf_bg = Image.new("RGBA", (width, waveform_height), WAVEFORM_BG)
    wf_bg.paste(waveform_img, (0, 0))
    canvas.paste(wf_bg, (0, 0))

    draw = ImageDraw.Draw(canvas, "RGBA")
    font_sm = _load_font(11)
    font_md = _load_font(13)

    end = start + duration

    def t_to_x(t: float) -> int:
        return int((t - start) / duration * width)

    # --- Silence regions ---
    for silence in silences:
        x1 = max(0, t_to_x(silence["start"]))
        x2 = min(width, t_to_x(silence["end"]))
        if x2 <= x1:
            continue
        # Fill
        draw.rectangle([x1, 0, x2, waveform_height], fill=SILENCE_COLOR)
        # Border
        draw.line([(x1, 0), (x1, waveform_height)], fill=SILENCE_BORDER, width=1)
        draw.line([(x2, 0), (x2, waveform_height)], fill=SILENCE_BORDER, width=1)
        # Duration label inside the region if wide enough
        label = f"{silence['duration']:.2f}s"
        try:
            bbox = draw.textbbox((0, 0), label, font=font_sm)
            label_w = bbox[2] - bbox[0]
        except AttributeError:
            label_w = len(label) * 7
        if (x2 - x1) > label_w + 4:
            lx = x1 + (x2 - x1 - label_w) // 2
            draw.text((lx, 4), label, fill=(255, 200, 200, 220), font=font_sm)

    # --- Scene boundaries ---
    for scene in scenes:
        for t in (scene["start_s"], scene["end_s"]):
            if start <= t <= end:
                x = t_to_x(t)
                draw.line([(x, 0), (x, waveform_height)], fill=SCENE_LINE_COLOR, width=1)

        # Scene index label at the bottom of the waveform strip
        if scene["start_s"] >= start and scene["start_s"] <= end:
            x = t_to_x(scene["start_s"])
            draw.text((x + 2, waveform_height - 18), f"S{scene['index']}", fill=SCENE_LABEL_COLOR, font=font_sm)

    # --- Timecode axis ---
    axis_y = waveform_height + 8
    tick_interval = _nice_tick_interval(duration)
    t = math.ceil(start / tick_interval) * tick_interval
    while t <= end:
        x = t_to_x(t)
        draw.line([(x, waveform_height), (x, waveform_height + 5)], fill=TIMECODE_COLOR, width=1)
        label = _fmt_tc(t)
        draw.text((x + 2, axis_y), label, fill=TIMECODE_COLOR, font=font_sm)
        t += tick_interval

    # --- Legend ---
    legend_y = waveform_height + 30
    draw.rectangle([10, legend_y, 22, legend_y + 12], fill=SILENCE_COLOR)
    draw.text((26, legend_y), f"silence ≥ {min_silence}s", fill=LABEL_COLOR, font=font_sm)
    draw.line([(160, legend_y + 6), (172, legend_y + 6)], fill=SCENE_LINE_COLOR, width=2)
    draw.text((176, legend_y), "scene boundary", fill=LABEL_COLOR, font=font_sm)

    # --- Header ---
    header = f"Range: {_fmt_tc(start)} → {_fmt_tc(end)}  ({duration:.1f}s)   silence threshold: {min_silence}s"
    draw.text((10, waveform_height + 56), header, fill=LABEL_COLOR, font=font_md)

    return canvas


def _nice_tick_interval(duration: float) -> float:
    """Pick a human-friendly tick interval for the timecode axis."""
    targets = [1, 2, 5, 10, 15, 30, 60, 120, 300]
    desired_ticks = 20
    for t in targets:
        if duration / t <= desired_ticks:
            return float(t)
    return 60.0


def _fmt_tc(seconds: float) -> str:
    m = int(seconds // 60)
    s = seconds % 60
    if m > 0:
        return f"{m}:{s:05.2f}"
    return f"{s:.2f}s"


# ---------------------------------------------------------------------------
# Report printing
# ---------------------------------------------------------------------------

def print_report(silences: list[dict], scenes: list[dict], start: float, end: float) -> None:
    if not silences:
        print(f"No silences ≥ threshold found in {_fmt_tc(start)}–{_fmt_tc(end)}")
        return

    print(f"\nSilence report  ({_fmt_tc(start)} → {_fmt_tc(end)}):")
    print(f"{'Start':>8}  {'End':>8}  {'Dur':>6}  {'Scene(s)':>10}  {'Position'}")
    print("-" * 56)
    for s in silences:
        sc_str = ",".join(str(i) for i in s.get("scenes", [])) or "—"
        pos = s.get("position", "")
        print(f"{_fmt_tc(s['start']):>8}  {_fmt_tc(s['end']):>8}  {s['duration']:>5.2f}s  {sc_str:>10}  {pos}")

    print(f"\n{len(silences)} silence region(s) found.")
    tail_silences = [s for s in silences if s.get("position") == "tail"]
    if tail_silences:
        print(f"  Tail silences (likely trimmable): {len(tail_silences)}")
        for s in tail_silences:
            sc = s.get("scenes", [])
            print(f"    Scene {sc} ends with {s['duration']:.2f}s silence at {_fmt_tc(s['start'])}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Waveform visualization + silence report for a time range.",
    )
    parser.add_argument("--input", "-i", required=True, help="Audio or video file")
    parser.add_argument("--start", type=float, default=0.0, help="Start time in seconds (default: 0)")
    parser.add_argument("--end", type=float, default=None, help="End time in seconds (default: end of file)")
    parser.add_argument("--min-silence", type=float, default=0.15, help="Min silence duration to detect in seconds (default: 0.15)")
    parser.add_argument("--noise", type=float, default=-40.0, help="Silence threshold in dB (default: -40)")
    parser.add_argument("--manifest", help="manifest.yaml — overlays scene boundaries on waveform")
    parser.add_argument("--output", "-o", help="Output PNG path (default: <input>_waveform.png)")
    parser.add_argument("--json", help="Optional path to write silence regions as JSON")
    parser.add_argument("--width", type=int, default=1920, help="Image width in pixels (default: 1920)")
    parser.add_argument("--height", type=int, default=280, help="Waveform height in pixels (default: 280)")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"ERROR: File not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    total_duration = get_duration(args.input)
    start = args.start
    end = args.end if args.end is not None else total_duration
    end = min(end, total_duration)
    duration = end - start

    if duration <= 0:
        print(f"ERROR: Invalid time range: {start}s → {end}s", file=sys.stderr)
        sys.exit(1)

    # Load scenes if manifest provided
    scenes = load_scenes(args.manifest) if args.manifest else []
    # Filter to scenes that overlap the time range
    scenes = [s for s in scenes if s["end_s"] > start and s["start_s"] < end]

    # Generate waveform
    print(f"Generating waveform for {Path(args.input).name}  [{_fmt_tc(start)} → {_fmt_tc(end)}]...")
    try:
        wf_img = generate_waveform_image(args.input, start, duration, args.width, args.height)
    except SystemExit:
        raise

    # Detect silences
    print(f"Detecting silences (threshold: {args.min_silence}s / {args.noise}dB)...")
    try:
        silences = detect_silences(args.input, start, duration, args.min_silence, args.noise)
    except Exception as e:
        print(f"ERROR: Silence detection failed: {e}", file=sys.stderr)
        sys.exit(1)

    if scenes:
        silences = map_silences_to_scenes(silences, scenes)

    # Compose image
    img = compose_image(wf_img, silences, scenes, start, duration, args.width, args.height, args.min_silence)

    # Save
    output_path = args.output or (Path(args.input).stem + "_waveform.png")
    try:
        img.save(output_path)
    except Exception as e:
        print(f"ERROR: Could not save image to {output_path}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Waveform saved: {output_path}")

    # JSON output
    if args.json:
        try:
            Path(args.json).write_text(json.dumps({"silences": silences, "scenes_in_range": len(scenes)}, indent=2))
            print(f"JSON report:   {args.json}")
        except Exception as e:
            print(f"ERROR: Could not write JSON to {args.json}: {e}", file=sys.stderr)

    # Print report
    print_report(silences, scenes, start, end)


if __name__ == "__main__":
    main()
